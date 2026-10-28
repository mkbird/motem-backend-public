"""Robot Agent — LangGraph StateGraph + ToolNode."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Sequence, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph, add_messages
from langgraph.prebuilt import ToolNode

from robot.agent.tools import all_tools, set_dependencies
from robot.config import settings

logger = logging.getLogger(__name__)

# ── State ──


class RobotAgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    owner_id: str
    user_id: str
    device_id: str
    display_name: str
    user_lang: str
    game_id: str
    device_model: str


# ── Model cache ──

_model_cache: dict[str, object] = {}


def _get_model():
    cache_key = f"{settings.openai_base_url}:{settings.default_model}"
    if cache_key not in _model_cache:
        logger.info("[Agent] initializing model: %s at %s", settings.default_model, settings.openai_base_url)
        _model_cache[cache_key] = init_chat_model(
            f"openai:{settings.default_model}",
            api_key=settings.dashscope_api_key,
            base_url=settings.openai_base_url,
            temperature=0.7,
        )
    return _model_cache[cache_key]


# ── System prompt ──

_prompt_template: str | None = None


def _load_prompt_template() -> str:
    """Load prompt template from file (always re-read in debug/dev mode)."""
    global _prompt_template
    path = Path(__file__).parent / "prompts" / "system_prompt_robot.txt"
    _prompt_template = path.read_text(encoding="utf-8")
    return _prompt_template


def _build_system_prompt(state: RobotAgentState) -> str:
    template = _load_prompt_template()
    lang = state.get("user_lang", "en")
    lang_instruction = "Reply in English." if lang.startswith("en") else "用中文回复。"

    game_id = state.get("game_id", "")
    game_context = f"\n- Current active game ID: {game_id} (use this for modify_game)" if game_id else ""

    return template.format(
        member_display_name=state.get("display_name", ""),
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M %A"),
        lang_instruction=lang_instruction,
        game_context=game_context,
    )


# ── Graph nodes ──


def call_model(state: RobotAgentState):
    model = _get_model()
    model_with_tools = model.bind_tools(all_tools)

    system_prompt = _build_system_prompt(state)
    messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
    logger.info("[Agent] call_model: %d messages, last user msg=%s",
                len(messages),
                next((m.content[:80] for m in reversed(messages) if isinstance(m, HumanMessage)), "N/A"))

    response = model_with_tools.invoke(messages)

    if hasattr(response, "tool_calls") and response.tool_calls:
        logger.info("[Agent] LLM wants tool calls: %s", [(tc["name"], tc.get("args", {}).keys()) for tc in response.tool_calls])
    else:
        logger.info("[Agent] LLM text response, len=%d, text=%r", len(response.content) if response.content else 0, (response.content or "")[:200])

    return {"messages": [response]}


def should_continue(state: RobotAgentState) -> Literal["continue", "end"]:
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        logger.info("[Agent] should_continue → tools: %s", [tc["name"] for tc in last_message.tool_calls])
        return "continue"
    logger.info("[Agent] should_continue → end")
    return "end"


# ── Build graph ──


def create_robot_agent(db_session_factory, ws_manager):
    """Create and compile the robot agent graph."""
    set_dependencies(db_session_factory, ws_manager)

    tool_node = ToolNode(all_tools)
    workflow = StateGraph(RobotAgentState)

    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges(
        "agent", should_continue, {"continue": "tools", "end": END}
    )
    workflow.add_edge("tools", "agent")

    return workflow.compile()
