"""Slim, game-focused agent for the bot.

A LangGraph agent bound to ONLY create_game / modify_game (plus plain chat) —
not the full robot agent's ~15 tools (calendar / weather / device / camera /
widget…), which are irrelevant in Discord and would add ~15-20K tokens/turn.

The agent itself decides create vs modify vs chat from the conversation, using
the per-user 'active game' injected into the system prompt. This replaces the
bot's earlier hand-rolled intent parser.

Per-user isolation: conversation memory (Redis robot:chat:{id}) and active game
are keyed by the Discord user id, which is passed as ``user_id``. Game DB
ownership uses a separate ``owner_id`` (the bot's backend account). ContextVars
set per call are isolated per asyncio task, so concurrent users don't clash.
"""

from __future__ import annotations

import logging
import re

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from robot.agent.robot_agent import RobotAgentState, _get_model
from robot.agent.tools import set_request_context
from robot.agent.tools.game_tools import create_game, modify_game
from robot.bot import active_game
from robot.bot.service import build_play_url
from robot.config import settings
from robot.memory.redis_client import ChatMemoryClient

logger = logging.getLogger(__name__)

_GAME_ID_RE = re.compile(r"ai_\d+_[a-f0-9]{6}")


@tool
async def create_browser_game(description: str, title: str = "") -> str:
    """Create a brand-new camera-controlled browser game from the user's
    description. Use when the user asks for a NEW game. `description` is the
    user's full request; `title` is an optional short name."""
    # create_game itself now does fast-path-first → SDK fallback (one place,
    # all callers benefit). We just default input_mode to body_pose (web games
    # are camera-controlled).
    return await create_game.ainvoke({
        "title": title or description[:40] or "AI Game",
        "description": description,
        "input_mode": "body_pose",
    })


_GAME_TOOLS = [create_browser_game, modify_game]

_SYSTEM_PROMPT = """You are motem, a friendly assistant in a chat app that turns \
text into playable browser games (camera/visual control, shareable links).

Decide what to do for each user message:
- The user describes a NEW game they want → call create_browser_game with the \
user's full request as `description` (and a short `title` if obvious).
- The user wants to tweak/change/fix the game they are currently working on \
(e.g. "more bananas", "enemies too fast", "add sound") → call \
modify_game(game_id=<the current game id>, instruction=<the user's request \
verbatim>). Only use modify when there IS a current game.
- Greetings, questions, "how do I play", small talk → just reply briefly, do \
NOT call a tool.

After a tool runs, reply with a short, friendly confirmation (the playable link \
is attached automatically — you do NOT need to include any URL).{game_context}

{lang_instruction}"""


def _build_prompt(state: RobotAgentState) -> str:
    lang = state.get("user_lang", "en")
    lang_instruction = "Reply in English." if lang.startswith("en") else "用中文回复。"
    gid = state.get("game_id", "")
    game_context = (
        f"\n\nThe user's CURRENT game id is {gid} — use it for modify_game."
        if gid else
        "\n\nThe user has no current game yet; the next game request is a create."
    )
    return _SYSTEM_PROMPT.format(game_context=game_context, lang_instruction=lang_instruction)


async def _call_model(state: RobotAgentState):
    model = _get_model().bind_tools(_GAME_TOOLS)
    messages = [SystemMessage(content=_build_prompt(state))] + list(state["messages"])
    response = await model.ainvoke(messages)  # async → never blocks the loop
    if getattr(response, "tool_calls", None):
        logger.info("[bot-agent] tool: %s", [tc["name"] for tc in response.tool_calls])
    return {"messages": [response]}


def _should_continue(state: RobotAgentState):
    last = state["messages"][-1]
    return "continue" if getattr(last, "tool_calls", None) else "end"


def build_game_agent():
    """Compile the slim game agent graph (build once at bot startup)."""
    wf = StateGraph(RobotAgentState)
    wf.add_node("agent", _call_model)
    wf.add_node("tools", ToolNode(_GAME_TOOLS))
    wf.set_entry_point("agent")
    wf.add_conditional_edges("agent", _should_continue, {"continue": "tools", "end": END})
    wf.add_edge("tools", "agent")
    return wf.compile()


def _history_to_messages(turns: list[dict]) -> list[BaseMessage]:
    msgs: list[BaseMessage] = []
    for t in turns:
        if t.get("human"):
            msgs.append(HumanMessage(content=t["human"]))
        if t.get("ai"):
            msgs.append(AIMessage(content=t["ai"]))
    return msgs


async def run_game_agent(
    agent,
    *,
    requester_key: str,
    owner_id: str,
    text: str,
    user_lang: str = "en",
    display_name: str = "",
):
    """Run one turn for a chat message. Returns ``(reply_text, game_id|None)``.

    ``requester_key`` = the Discord user id (memory + active-game key).
    ``owner_id`` = the bot's backend account (game ownership). Caller is
    responsible for any concurrency lock / gating.
    """
    mem = ChatMemoryClient(requester_key)
    history = _history_to_messages(await mem.get_recent(limit=settings.chat_history_max_rounds))
    active = await active_game.get_active(requester_key)

    state: RobotAgentState = {
        "messages": history + [HumanMessage(content=text)],
        "owner_id": owner_id,
        "user_id": requester_key,
        "device_id": "",
        "display_name": display_name,
        "user_lang": user_lang,
        "game_id": active or "",
        "device_model": "",
    }
    # ContextVars (isolated per asyncio task) so create_game/modify_game see the
    # right identity even under concurrent users.
    set_request_context(owner_id, requester_key, "admin", "", user_lang, "")

    reply_parts: list[str] = []
    touched_game_id: str | None = None  # game CREATED or MODIFIED this turn (None = pure chat)
    async for chunk in agent.astream(state, stream_mode="messages"):
        if not (isinstance(chunk, tuple) and len(chunk) == 2):
            continue
        msg, _meta = chunk
        if isinstance(msg, AIMessage):
            if msg.content:
                reply_parts.append(msg.content if isinstance(msg.content, str) else str(msg.content))
            # modify_game's result string carries no game_id → take it from the call args.
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc.get("name") == "modify_game":
                    gid = (tc.get("args") or {}).get("game_id")
                    if gid and _GAME_ID_RE.fullmatch(str(gid)):
                        touched_game_id = str(gid)
        elif isinstance(msg, ToolMessage) and msg.content:
            # create_game's result string contains the new game_id.
            m = _GAME_ID_RE.search(str(msg.content))
            if m:
                touched_game_id = m.group(0)

    reply = "".join(reply_parts).strip()
    if touched_game_id:
        await active_game.set_active(requester_key, touched_game_id)
    await mem.save_turn(text, reply or "(no reply)")
    return reply, touched_game_id
