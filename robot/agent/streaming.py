"""Streaming helper — runs the agent and streams text chunks via a callback."""

import asyncio
import inspect
import json
import logging
import os
import re as _re
import shutil
import time
from typing import Callable

_UNSAFE_HTML = _re.compile(r'[<>&\x00-\x1f]')


def _sanitize_meta_str(s: str) -> str:
    """Remove HTML/script injection characters from LLM-generated metadata."""
    return _UNSAFE_HTML.sub('', s)

from langchain_core.messages import AIMessage, HumanMessage

from robot.agent.robot_agent import RobotAgentState
from robot.memory.memory_manager import MemoryManager
from robot.rate_limiter import check_ws_create_limit

logger = logging.getLogger(__name__)

# Prefix markers for game messages from iOS
_GAME_MODIFY_PREFIX = "[game_modify]"
_GAME_CREATE_PREFIX = "[game_create]"

# Prefix markers for widget messages from iOS
_WIDGET_MODIFY_PREFIX = "[widget_modify]"
_WIDGET_CREATE_PREFIX = "[widget_create]"


async def stream_agent_response(
    user_input: str,
    agent,
    state: RobotAgentState,
    memory: MemoryManager,
    on_text_chunk: Callable[[str], None],
    cancel_check: Callable[[], bool] | None = None,
    save_memory: bool = True,
    attachments: list | None = None,
) -> str:
    """Stream the agent response, calling on_text_chunk for each text piece.

    Args:
        cancel_check: If provided, called before each chunk. Returns True
            to stop streaming early (saves LLM compute).
        save_memory: If False, caller is responsible for saving the turn
            to memory (useful when Turn may be cancelled).
        attachments: Optional list of image/file attachments from the client.

    Returns the full response text.
    """
    # Set per-request context for tools
    from robot.agent.tools import set_request_context
    from robot.agent.tools._context import _current_cancel_check
    set_request_context(
        state.get("owner_id", ""),
        state.get("user_id", ""),
        "admin",
        state.get("user_tz", ""),
        state.get("user_lang", "en"),
        state.get("device_model", ""),
    )
    # 注入 cancel_check 让 fast-path 工具（create_game / modify_game）能在
    # 关键 checkpoint 检查并清理（防 WS 断 / 用户取消时残留草稿 + 文件）。
    _current_cancel_check.set(cancel_check)
    logger.info("[Stream] context set: owner=%s user=%s",
                state.get("owner_id", ""), state.get("user_id", ""))

    # ── Game fast path: prefix detected → call game tool directly, skip LLM agent ──
    if user_input.startswith(_GAME_MODIFY_PREFIX):
        from robot.agent.tools.game_tools import (
            _is_safe_ai_game_id,
            _send_game_modification_failed,
        )
        try:
            payload = json.loads(user_input[len(_GAME_MODIFY_PREFIX):])
            game_id = payload.get("game_id", "")
            instruction = payload.get("instruction", "")
        except (json.JSONDecodeError, TypeError):
            logger.warning("[Stream] failed to parse game_modify payload, rejecting")
            await _send_game_modification_failed("", "invalid_payload")
            return ""
        # P0 强校验：路径穿越防御（详见 game_tools._GAME_ID_PATTERN 注释）。
        # 工具内部还有一道 defense-in-depth，这里早 reject 可以省一次无意义的
        # set_request_context / ws send_to_user roundtrip。
        if not _is_safe_ai_game_id(game_id):
            logger.warning("[Stream] game_modify rejected: invalid game_id=%r", game_id)
            await _send_game_modification_failed(game_id, "invalid_game_id")
            return ""
        logger.info("[Stream] game_modify fast path: game_id=%s instruction=%r", game_id, instruction[:100])
        result = await _handle_game_direct("modify", game_id, instruction, on_text_chunk)
        if save_memory and result:
            await memory.save_turn(user_input, result)
        return result

    if user_input.startswith(_GAME_CREATE_PREFIX):
        _uid = state.get("user_id", "")
        if not await check_ws_create_limit(_uid, "game_create", limit=30, window=3600):
            from robot.i18n import t as _t
            lang = state.get("user_lang", "en")
            msg = _t(lang, "RATE_LIMIT_GAME_CREATE")
            # on_text_chunk may be async (websocket handler passes coroutine
            # function). Type hint says sync but caller is heterogeneous.
            _r = on_text_chunk(msg)
            if asyncio.iscoroutine(_r):
                await _r
            return msg
        raw = user_input[len(_GAME_CREATE_PREFIX):]
        # Parse JSON format (with input_mode, template_game_id) or fall back to plain text
        input_mode = "touch"
        template_game_id = ""
        try:
            payload = json.loads(raw)
            raw = payload.get("text", raw)
            input_mode = payload.get("input_mode", "touch")
            template_game_id = payload.get("template_game_id", "")
        except (json.JSONDecodeError, TypeError):
            pass
        logger.info("[Stream] game_create fast path: %r input_mode=%s template=%s",
                    raw[:100], input_mode, template_game_id or "auto")
        result = await _handle_game_direct("create", "", raw, on_text_chunk, input_mode=input_mode, template_game_id=template_game_id)
        if save_memory and result:
            await memory.save_turn(user_input, result)
        return result

    # ── Widget fast path: prefix detected → call widget tool directly, skip LLM agent ──
    if user_input.startswith(_WIDGET_MODIFY_PREFIX):
        try:
            payload = json.loads(user_input[len(_WIDGET_MODIFY_PREFIX):])
            widget_id = payload.get("widget_id", "")
            instruction = payload.get("instruction", "")
            logger.info("[Stream] widget_modify fast path: widget_id=%s instruction=%r",
                        widget_id, instruction[:100])
            result = await _handle_widget_direct("modify", widget_id, instruction, on_text_chunk)
            if save_memory and result:
                await memory.save_turn(user_input, result)
            return result
        except (json.JSONDecodeError, TypeError):
            logger.warning("[Stream] failed to parse widget_modify payload, rejecting")
            return ""

    if user_input.startswith(_WIDGET_CREATE_PREFIX):
        _uid = state.get("user_id", "")
        if not await check_ws_create_limit(_uid, "widget_create", limit=30, window=3600):
            from robot.i18n import t as _t
            lang = state.get("user_lang", "en")
            msg = _t(lang, "RATE_LIMIT_WIDGET_CREATE")
            _r = on_text_chunk(msg)
            if asyncio.iscoroutine(_r):
                await _r
            return msg
        raw = user_input[len(_WIDGET_CREATE_PREFIX):]
        template_widget_id = ""
        try:
            payload = json.loads(raw)
            raw = payload.get("text", raw)
            template_widget_id = payload.get("template_widget_id", "")
        except (json.JSONDecodeError, TypeError):
            pass
        logger.info("[Stream] widget_create fast path: %r template=%s",
                    raw[:100], template_widget_id or "auto")
        result = await _handle_widget_direct("create", "", raw, on_text_chunk, template_widget_id=template_widget_id)
        if save_memory and result:
            await memory.save_turn(user_input, result)
        return result

    # ── Image pre-processing (mirrors QwenStreamer pattern in AIChatServer) ──
    from aichat.tools.vision_tool import (
        analyze_images_auto,
        clear_attachments,
        set_attachments,
    )
    from aichat.demo.agent import get_sticky_cache

    user_id = state.get("user_id", "")
    sticky_cache = get_sticky_cache()
    raw_attachments: list[dict] = list(attachments or [])
    has_new_images = any(a.get("kind") == "image" for a in raw_attachments)
    current_images: list[dict] = []

    # Load history into state
    history = await memory.load_history_as_messages()
    logger.info("[Stream] loaded %d history messages, user_input=%r attachments=%d",
                len(history), user_input[:100] if user_input else "", len(raw_attachments))

    if has_new_images:
        # Auto-analyze new images via VLM, inject analysis into history as context
        analysis = await analyze_images_auto(raw_attachments)
        if analysis:
            from robot.i18n import t as _t
            lang = state.get("user_lang", "en")
            history = history + [AIMessage(content=_t(lang, "image_content", analysis=analysis))]
            logger.info("[Stream] auto image analysis injected, len=%d", len(analysis))
        sticky_cache.save(user_id, raw_attachments)
        current_images = [a for a in raw_attachments if a.get("kind") == "image" and a.get("data")]
    else:
        sticky = sticky_cache.get(user_id)
        if sticky:
            current_images = sticky
            sticky_cache.incr_inject(user_id)
            logger.info("[Stream] injected %d sticky images for user=%s", len(sticky), user_id)

    if current_images:
        set_attachments(current_images)

    state["messages"] = history + [HumanMessage(content=user_input)]

    # 调用这些工具后不需要语音/文字回复
    SILENT_TOOLS = {"take_photo"}

    # 一旦下列「昂贵 / 强副作用」tool 进入调用流，就不再 honor speech.stop
    # 触发的 cancel_check —— 它们耗时几十秒、烧 LLM 钱、产生用户可见物（推送 +
    # 文件落盘 + DB 行）。半截砍掉等于扔了所有算力且 UI 卡在最后一条 progress。
    # 其他便宜的只读 tool（calendar / weather / search / photo …）保留可取消，
    # 让用户的 "stop + 补一段" 语义照常工作。
    UNCANCELLABLE_TOOLS = {
        "create_game", "modify_game",
        "create_widget", "modify_widget",
    }

    full_response = ""
    start_time = time.time()
    first_token_time = None
    tool_calls_seen = []
    suppress_text = False   # 检测到静默工具后置 True，不再 emit 文本

    try:
        async for chunk in agent.astream(state, stream_mode="messages"):
            # Cancel check —— 按 cancel reason 分支处理，区分两种来源：
            #   * "speech_stopped"  → 用户没说话了 / 离开摄像头 / ASR 超时。
            #     **不该砍 agent**：tool 流转继续，progress / ready 事件正常推。
            #   * "new_turn_created" → 用户真说了第二句要打断 → 像以前一样砍，
            #     让外层拼 A+B 重跑。但若已经在跑「昂贵 tool」（耗时几十秒、烧
            #     LLM 钱、有副作用），保留兜底 —— 半截砍掉等于扔了所有算力。
            #   * 其他 / bool 兼容路径 → 视为取消（保留旧 cancel_check=lambda: True
            #     的语义不破，让 test_create_game_cancel_cleanup 等老测试照常过）。
            if cancel_check:
                reason = cancel_check()
                if reason:
                    if reason == "speech_stopped":
                        # 忽略：tool 继续；只在第一次见到时记一行 log。
                        pass
                    else:
                        has_uncancellable = any(
                            tc.get("name") in UNCANCELLABLE_TOOLS for tc in tool_calls_seen
                        )
                        if not has_uncancellable:
                            logger.info(
                                "[Stream] cancelled by cancel_check (reason=%s tools=%s)",
                                reason,
                                [tc.get("name") for tc in tool_calls_seen] or "none",
                            )
                            break

            # chunk is (message_chunk, metadata)
            if isinstance(chunk, tuple) and len(chunk) == 2:
                message_chunk, metadata = chunk
                # Log tool calls & detect silent tools
                if isinstance(message_chunk, AIMessage) and hasattr(message_chunk, "tool_calls") and message_chunk.tool_calls:
                    for tc in message_chunk.tool_calls:
                        tool_calls_seen.append(tc)
                        logger.info("[Stream] tool_call: name=%s args=%s", tc.get("name"), tc.get("args"))
                        if tc.get("name") in SILENT_TOOLS:
                            suppress_text = True
                            logger.info("[Stream] silent tool detected (%s), suppressing text output", tc.get("name"))
                if isinstance(message_chunk, AIMessage) and message_chunk.content:
                    if first_token_time is None:
                        first_token_time = time.time()
                    text = message_chunk.content
                    full_response += text
                    if not suppress_text:
                        try:
                            result = on_text_chunk(text)
                            if inspect.isawaitable(result):
                                await result
                        except Exception as e:
                            logger.error("stream callback error: %s", e)
                # Log tool results
                if not isinstance(message_chunk, AIMessage) and hasattr(message_chunk, "content"):
                    msg_type = type(message_chunk).__name__
                    logger.info("[Stream] %s content=%s", msg_type, str(message_chunk.content)[:200])
    except Exception as e:
        logger.error("Agent streaming error: %s", e, exc_info=True)
        from robot.i18n import t as _t
        lang = state.get("user_lang", "en")
        full_response = _t(lang, "process_error", e=e)
        result = on_text_chunk(full_response)
        if inspect.isawaitable(result):
            await result
    finally:
        if current_images:
            clear_attachments()
        _current_cancel_check.set(None)  # M-9: 防 ContextVar 泄漏到下个请求

    # 静默工具调用时返回空字符串，避免 ai.text.done 把文本发到客户端
    output = "" if suppress_text else full_response

    # Save turn to memory (only if save_memory=True and response is non-empty)
    if save_memory and output:
        await memory.save_turn(user_input, output)

    elapsed = time.time() - start_time
    ttft = (first_token_time - start_time) if first_token_time else None
    logger.info(
        "[Stream] done: ttft=%.3fs total=%.3fs len=%d tool_calls=%d tools=%s suppressed=%s",
        ttft or 0,
        elapsed,
        len(full_response),
        len(tool_calls_seen),
        [tc.get("name") for tc in tool_calls_seen] if tool_calls_seen else "none",
        suppress_text,
    )

    return output


async def _emit(on_text_chunk: Callable, text: str) -> None:
    """Emit a text chunk, handling both sync and async callbacks."""
    result = on_text_chunk(text)
    if inspect.isawaitable(result):
        await result


def _load_official_templates(input_mode: str = "") -> list[dict]:
    """Load official curated templates from ``games/_templates/_registry.json``.

    Returns a list of registry entries, each like::

        {
          "id": "shooter",
          "title_i18n": {"en": "Top-down Shooter", "zh-Hans": "..."},
          "description_i18n": {"en": "...", "zh-Hans": "..."},
          "tags": ["shooter", "action"],
          "input_modes": ["touch", "body_pose"],
        }

    Returns ``[]`` when:

    * 注册表文件还没建立（registry 体系正在 ramp，没问题）
    * 注册表 JSON 解析失败（防御性，记一条 warning 后降级到空 list）

    候选源切到这里之后：候选池全是平台 curated、自带 bundled session 的种子
    工程，没有任何用户私有源代码暴露风险，``create_game`` 直接 copytree 即可。
    """
    from robot.agent.tools.game_tools import GAMES_DIR
    registry = GAMES_DIR / "_templates" / "_registry.json"
    if not registry.is_file():
        return []
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[Stream] _templates/_registry.json parse failed: %s", e)
        return []
    templates = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(templates, list):
        return []
    return templates


async def _extract_game_meta(description: str, input_mode: str = "touch") -> tuple[dict[str, str], dict[str, str], str, str]:
    """Use LLM to generate bilingual game meta and best template.

    Returns (title_i18n, description_i18n, icon_emoji, template_game_id).
    template_game_id is empty if no suitable template is found.
    """
    from robot.config import settings
    from openai import AsyncOpenAI
    from robot.agent.tools._context import get_user_lang

    # 注意：fallback 不再用 description[:10] —— 那会把"一个俯视角太空射击游"
    # 这种用户描述截断后当 title 显示，看着特别像没解析。改为通用占位符
    # （数字时间戳保证唯一），等用户进发布页改成更想要的名字。
    short_id = str(int(time.time()))[-4:]
    fallback_title_zh = f"新游戏-{short_id}"
    fallback_title_en = f"New Game {short_id}"
    fallback_description = description[:100].strip() or fallback_title_zh
    if not settings.dashscope_api_key:
        return (
            {"en": fallback_title_en, "zh-Hans": fallback_title_zh},
            {"en": fallback_description, "zh-Hans": fallback_description},
            "🎮",
            "",
        )

    # 候选源：官方 curated 模板注册表。registry 还没建好（_load_official_templates
    # 返空 list）→ LLM prompt 退化到不带模板字段那一支 → 最终 template_id="" →
    # create_game 自动降级到空脚手架 _template。整条链对「无可用官方模板」状态
    # graceful degrade，不需要任何 env var gate。
    official_templates = _load_official_templates(input_mode=input_mode)
    templates_list_text = ""
    user_lang = get_user_lang()
    if official_templates:
        lines = []
        for i, t in enumerate(official_templates, 1):
            tid = t.get("id", "")
            ti18n = t.get("title_i18n", {}) or {}
            di18n = t.get("description_i18n", {}) or {}
            ttitle = ti18n.get(user_lang, ti18n.get("en", ""))
            tdesc = (di18n.get(user_lang, di18n.get("en", "")) or "")[:140]
            tmodes = ",".join(t.get("input_modes", []) or [])
            lines.append(f"{i}. {tid} | {ttitle} | {tdesc} | {tmodes}")
        templates_list_text = "\n".join(lines)

    try:
        client = AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.openai_base_url,
        )

        if templates_list_text:
            system_content = (
                "You are a creative game naming expert. "
                "Given a game description, output EXACTLY a JSON object with six fields:\n"
                '{"title_en": "...", "title_zh": "...", "description_en": "...", "description_zh": "...", "icon": "...", "template_id": "..."}\n\n'
                "- title_en: a short, catchy English game title, 1-3 words. No punctuation, no quotes.\n"
                "- title_zh: a short, catchy Simplified Chinese game title, 2-5 Chinese characters. No punctuation, no quotes.\n"
                "- description_en: one concise English sentence describing the gameplay.\n"
                "- description_zh: one concise Simplified Chinese sentence describing the gameplay.\n"
                "- icon: a single emoji that best represents the game's theme.\n"
                "- template_id: id of the most similar official template to start from. "
                "Match priority:\n"
                f"  1. Gameplay similar AND input_mode includes {input_mode!r}\n"
                "  2. Gameplay similar but different input_mode\n"
                "  3. If no template fits, set template_id to empty string (a blank scaffold will be used)\n\n"
                "Available templates (id | title | description | input_modes):\n"
                + templates_list_text + "\n\n"
                "Output ONLY the JSON object, no explanation."
            )
            max_tokens = 400
        else:
            system_content = (
                "You are a creative game naming expert. "
                "Given a game description, output EXACTLY a JSON object with five fields:\n"
                '{"title_en": "...", "title_zh": "...", "description_en": "...", "description_zh": "...", "icon": "..."}\n\n'
                "- title_en: a short, catchy English game title, 1-3 words. No punctuation, no quotes.\n"
                "- title_zh: a short, catchy Simplified Chinese game title, 2-5 Chinese characters. No punctuation, no quotes.\n"
                "- description_en: one concise English sentence describing the gameplay.\n"
                "- description_zh: one concise Simplified Chinese sentence describing the gameplay.\n"
                "- icon: a single emoji that best represents the game's theme.\n\n"
                "Output ONLY the JSON object, no explanation."
            )
            max_tokens = 300

        resp = await client.chat.completions.create(
            model=settings.default_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": description},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        raw = resp.choices[0].message.content.strip()

        # 容忍 LLM 在 JSON 外包了 markdown 围栏（```json ... ``` / ``` ... ```）。
        # 旧 max_tokens=80 时还见过被截断的 JSON，留个兜底裁出第一个 `{...}`：
        # 找第一个 `{` 到最后一个 `}` 之间的子串再 parse；解析失败再 raise。
        if raw.startswith("```"):
            # 去掉首行 ```json / ``` 和尾行 ```
            first_nl = raw.find("\n")
            if first_nl != -1:
                raw = raw[first_nl + 1:]
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3].rstrip()
        # 截出 {...}
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last > first:
            raw = raw[first:last + 1]

        # Parse JSON response
        parsed = json.loads(raw)
        title_en = _sanitize_meta_str(str(parsed.get("title_en", "")).strip().strip('"\'""'''))[:40]
        title_zh = _sanitize_meta_str(str(parsed.get("title_zh", "")).strip().strip('"\'""'''))[:15]
        desc_en = _sanitize_meta_str(str(parsed.get("description_en", "")).strip())[:100]
        desc_zh = _sanitize_meta_str(str(parsed.get("description_zh", "")).strip())[:100]
        icon = _sanitize_meta_str(str(parsed.get("icon", "🎮")).strip())
        template_id = str(parsed.get("template_id", "")).strip()

        if not icon or len(icon) > 4:
            icon = "🎮"
        title_en = title_en or fallback_title_en
        title_zh = title_zh or fallback_title_zh
        desc_en = desc_en or fallback_description
        desc_zh = desc_zh or fallback_description

        # Validate template_id exists in our official template registry
        valid_ids = {t.get("id", "") for t in official_templates}
        if template_id and template_id not in valid_ids:
            logger.warning(
                "[Stream] LLM returned invalid template_id=%r (not in registry), "
                "falling back to blank _template", template_id,
            )
            template_id = ""

        if template_id:
            logger.info("[Stream] template match: %s for %r", template_id, description[:60])

        return {"en": title_en, "zh-Hans": title_zh}, {"en": desc_en, "zh-Hans": desc_zh}, icon, template_id
    except Exception as e:
        # 把 raw 也 log 出来方便定位（截断、围栏、奇怪 unicode 等）
        raw_preview = ""
        try:
            raw_preview = (raw or "")[:300]
        except NameError:
            pass
        logger.warning(
            "[Stream] _extract_game_meta failed: %s, fallback. raw=%r",
            e, raw_preview,
        )
        return (
            {"en": fallback_title_en, "zh-Hans": fallback_title_zh},
            {"en": fallback_description, "zh-Hans": fallback_description},
            "🎮",
            "",
        )


async def _try_fastpath_create(instruction: str, input_mode: str) -> str | None:
    """Thin delegate → game_tools._try_fastpath (logic centralized there).

    Kept so the iOS [game_create] route (and its tests) keep this call site,
    while the actual fast-path logic lives once in game_tools."""
    from robot.agent.tools.game_tools import _try_fastpath

    return await _try_fastpath(instruction, input_mode)


async def _handle_game_direct(
    action: str,
    game_id: str,
    instruction: str,
    on_text_chunk: Callable,
    input_mode: str = "touch",
    template_game_id: str = "",
) -> str:
    """Directly call game tools, bypassing the LLM agent.

    Called when iOS sends [game_create] or [game_modify] prefixed messages,
    where the intent is already determined by the UI context.
    """
    from robot.agent.tools.game_tools import create_game, modify_game
    from robot.agent.tools._context import get_user_lang

    t0 = time.time()

    try:
        if action == "create":
            # 测试 mock 路径：环境变量 ROBOT_GAME_GEN_MOCK 非空 → 不调 Claude Code SDK，
            # 直接拷一个现有游戏作为生成结果。值可以是：
            #   "1" / "true" → 自动选 games/ 下第一个 ai_* 游戏
            #   game_id     → 使用指定游戏作为模板
            # 用于测试创作 → 发布全链路，跳过几十秒到几分钟的 AI 生成时间。
            mock_flag = os.environ.get("ROBOT_GAME_GEN_MOCK", "").strip()
            if mock_flag:
                logger.info("[Stream] 🚧 MOCK game_create active (ROBOT_GAME_GEN_MOCK=%r)", mock_flag)
                result = await _mock_create_game(
                    instruction=instruction,
                    input_mode=input_mode,
                    mock_source=mock_flag,
                )
                logger.info("[Stream] mock game_create done: %.1fs result=%r", time.time() - t0, result[:100])
                await _emit(on_text_chunk, result)
                return result

            # ─── Fast Path（feature flag 灰度） ────────────────────────────
            # 命中 archetype + 高置信度 → 跳过 Claude Code SDK，秒级出游戏。
            # 任何环节失败完全无感 fallback 到下面的 slow path。
            from robot.config import settings as _settings
            if _settings.fastpath_enabled and not template_game_id:
                fp_result = await _try_fastpath_create(instruction, input_mode)
                if fp_result is not None:
                    logger.info("[Stream] fast path hit: game_id=%s in %.1fs",
                                fp_result, time.time() - t0)
                    await _emit(on_text_chunk, fp_result)
                    return fp_result
            # ─── Slow path（现有 SDK 链路） ────────────────────────────────
            title_i18n, description_i18n, icon, auto_template_id = await _extract_game_meta(instruction, input_mode=input_mode)
            title = title_i18n["en"] if get_user_lang().startswith("en") else title_i18n["zh-Hans"]
            # User-selected template takes priority over LLM auto-match
            final_template_id = template_game_id or auto_template_id
            logger.info("[Stream] game_create direct: title=%r icon=%s desc_len=%d input_mode=%s template=%s",
                        title, icon, len(instruction), input_mode, final_template_id or "none")
            # We already tried fast path above (pre-block) → tell create_game to
            # skip its internal attempt, avoiding a redundant router call on miss.
            from robot.agent.tools import game_tools as _gt
            _fp_tok = _gt._skip_fastpath.set(True)
            try:
                result = await create_game.ainvoke({"title": title, "title_i18n": title_i18n, "description": instruction, "description_i18n": description_i18n, "input_mode": input_mode, "icon": icon, "template_game_id": final_template_id})
            finally:
                _gt._skip_fastpath.reset(_fp_tok)
        else:
            # MOCK modify：直接返回成功，不真改文件。
            mock_flag = os.environ.get("ROBOT_GAME_GEN_MOCK", "").strip()
            if mock_flag:
                logger.info("[Stream] 🚧 MOCK game_modify active (no-op): game_id=%s", game_id)
                result = f"Mock modify success: game_id={game_id} (no actual changes)"
                await _emit(on_text_chunk, result)
                return result
            logger.info("[Stream] game_modify direct: game_id=%s instruction=%r", game_id, instruction[:100])
            result = await modify_game.ainvoke({"game_id": game_id, "instruction": instruction})

        logger.info("[Stream] game direct done: %.1fs result=%r", time.time() - t0, result[:100])
        await _emit(on_text_chunk, result)
        return result

    except Exception as e:
        logger.error("[Stream] game direct error: %s", e, exc_info=True)
        from robot.i18n import t as _t
        from robot.agent.tools._context import get_user_lang as _get_user_lang
        # modify 路径异常必须推 game_modification_failed，否则 iOS UI 永久 stuck
        # （create 路径已有 `game_creation_failed` 闭环，但工具内部抛异常时
        # 这里是兜底网；create 时 game_id 为空，跳过事件，让 create 自己的
        # cleanup 路径处理）。
        if action == "modify" and game_id:
            try:
                from robot.agent.tools.game_tools import _send_game_modification_failed
                await _send_game_modification_failed(game_id, "internal_error")
            except Exception as e2:
                logger.warning("[Stream] also failed to notify modify failure: %s", e2)
        error_msg = _t(_get_user_lang(), "GAME_OPERATION_ERROR", e=e)
        await _emit(on_text_chunk, error_msg)
        return error_msg


# ── Widget direct path ───────────────────────────────────────────────────


def _load_existing_widgets(owner_id: str) -> list[dict]:
    """Return user-owned + seed widget metas, both eligible as creation templates."""
    from robot.agent.tools.widget_tools import WIDGETS_DIR, SEEDS_DIR
    widgets: list[dict] = []
    # User-owned widgets share the WIDGETS_DIR namespace; we filter by owner_id in meta.
    if WIDGETS_DIR.is_dir():
        for d in sorted(WIDGETS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name == "seeds":
                continue
            meta_file = d / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if owner_id and str(meta.get("owner_id", "")) != str(owner_id):
                    continue
                widgets.append(meta)
            except Exception:
                pass
    if SEEDS_DIR.is_dir():
        for d in sorted(SEEDS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            meta_file = d / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                meta.setdefault("widget_id", d.name)
                meta["is_seed"] = True
                widgets.append(meta)
            except Exception:
                pass
    return widgets


async def _extract_widget_meta(description: str, owner_id: str = "") -> tuple[dict[str, str], dict[str, str], str, str]:
    """LLM-derived widget title / description / icon / best-match template id.

    Mirrors _extract_game_meta but over widgets/seeds + user widgets pool.
    Returns (title_i18n, description_i18n, icon, template_widget_id).
    """
    from robot.config import settings
    from openai import AsyncOpenAI

    fallback_title = description[:10].strip() or "AI Widget"
    fallback_description = description[:100].strip() or fallback_title
    if not settings.dashscope_api_key:
        fallback_i18n = {"en": fallback_title, "zh-Hans": fallback_title}
        return fallback_i18n, {"en": fallback_description, "zh-Hans": fallback_description}, "🧩", ""

    existing = _load_existing_widgets(owner_id)
    list_text = ""
    if existing:
        lines = []
        for i, w in enumerate(existing, 1):
            wid = w.get("widget_id", "")
            wtitle = w.get("title", "")
            wdesc = (w.get("description") or "")[:100]
            sizes = ",".join(w.get("supported_sizes", []) or [])
            lines.append(f"{i}. {wid} | {wtitle} | {wdesc} | sizes={sizes}")
        list_text = "\n".join(lines)

    try:
        client = AsyncOpenAI(api_key=settings.dashscope_api_key, base_url=settings.openai_base_url)
        if list_text:
            system_content = (
                "You are a creative widget naming expert for a tiny H5 dashboard widget. "
                "Given a widget description, output EXACTLY a JSON object with six fields:\n"
                '{"title_en": "...", "title_zh": "...", "description_en": "...", "description_zh": "...", "icon": "...", "template_id": "..."}\n\n'
                "- title_en: 1-3 English words, no punctuation, no quotes.\n"
                "- title_zh: 2-5 Simplified Chinese characters, no punctuation, no quotes.\n"
                "- description_en: one concise English sentence describing the widget.\n"
                "- description_zh: one concise Simplified Chinese sentence describing the widget.\n"
                "- icon: a single emoji that best represents the widget's theme.\n"
                "- template_id: the widget_id of the most similar existing widget to use as template, or empty if none is similar.\n\n"
                "Existing widgets:\n" + list_text + "\n\n"
                "Output ONLY the JSON object."
            )
            max_tokens = 120
        else:
            system_content = (
                "You are a creative widget naming expert. "
                "Given a widget description, output EXACTLY a JSON object with five fields:\n"
                '{"title_en": "...", "title_zh": "...", "description_en": "...", "description_zh": "...", "icon": "..."}\n\n'
                "- title_en: 1-3 English words.\n- title_zh: 2-5 Simplified Chinese characters.\n"
                "- description_en, description_zh: one concise sentence each.\n"
                "- icon: a single emoji.\n\n"
                "Output ONLY the JSON object."
            )
            max_tokens = 80

        resp = await client.chat.completions.create(
            model=settings.default_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": description},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        raw = resp.choices[0].message.content.strip()
        parsed = json.loads(raw)
        title_en = str(parsed.get("title_en", "")).strip().strip('"\'""''')[:40] or fallback_title
        title_zh = str(parsed.get("title_zh", "")).strip().strip('"\'""''')[:15] or fallback_title
        desc_en = str(parsed.get("description_en", "")).strip()[:100] or fallback_description
        desc_zh = str(parsed.get("description_zh", "")).strip()[:100] or fallback_description
        icon = str(parsed.get("icon", "🧩")).strip()
        if not icon or len(icon) > 4:
            icon = "🧩"
        template_id = str(parsed.get("template_id", "")).strip()
        valid_ids = {w.get("widget_id", "") for w in existing}
        if template_id and template_id not in valid_ids:
            logger.warning("[Stream] LLM returned invalid widget template_id=%r, ignoring", template_id)
            template_id = ""
        if template_id:
            logger.info("[Stream] widget template match: %s for %r", template_id, description[:60])
        return {"en": title_en, "zh-Hans": title_zh}, {"en": desc_en, "zh-Hans": desc_zh}, icon, template_id
    except Exception as e:
        logger.warning("[Stream] _extract_widget_meta failed: %s, fallback", e)
        return {"en": fallback_title, "zh-Hans": fallback_title}, {"en": fallback_description, "zh-Hans": fallback_description}, "🧩", ""


async def _handle_widget_direct(
    action: str,
    widget_id: str,
    instruction: str,
    on_text_chunk: Callable,
    template_widget_id: str = "",
) -> str:
    """Directly call widget tools, bypassing the LLM agent."""
    from robot.agent.tools.widget_tools import create_widget, modify_widget
    from robot.agent.tools._context import _current_owner_id, get_user_lang

    t0 = time.time()
    try:
        if action == "create":
            owner_id = _current_owner_id.get("")
            title_i18n, description_i18n, icon, auto_template_id = await _extract_widget_meta(
                instruction, owner_id=owner_id,
            )
            title = title_i18n["en"] if get_user_lang().startswith("en") else title_i18n["zh-Hans"]
            final_template_id = template_widget_id or auto_template_id
            logger.info("[Stream] widget_create direct: title=%r icon=%s template=%s",
                        title, icon, final_template_id or "none")
            result = await create_widget.ainvoke({
                "title": title,
                "title_i18n": title_i18n,
                "description": instruction,
                "description_i18n": description_i18n,
                "icon": icon,
                "template_widget_id": final_template_id,
            })
        else:
            logger.info("[Stream] widget_modify direct: widget_id=%s instruction=%r",
                        widget_id, instruction[:100])
            result = await modify_widget.ainvoke({"widget_id": widget_id, "instruction": instruction})

        logger.info("[Stream] widget direct done: %.1fs result=%r",
                    time.time() - t0, result[:100])
        await _emit(on_text_chunk, result)
        return result

    except Exception as e:
        logger.error("[Stream] widget direct error: %s", e, exc_info=True)
        from robot.i18n import t as _t
        from robot.agent.tools._context import get_user_lang as _get_user_lang
        error_msg = _t(_get_user_lang(), "WIDGET_OPERATION_ERROR", e=e)
        await _emit(on_text_chunk, error_msg)
        return error_msg


# ────────────────────────────────────────────────────────────────────────
# MOCK game generation — for E2E test of create→ready→publish flow
# without paying for / waiting on Claude Code SDK.
# ────────────────────────────────────────────────────────────────────────


async def _mock_create_game(instruction: str, input_mode: str, mock_source: str) -> str:
    """Skip Claude Code SDK 直接拷贝一个现有游戏作为生成结果。

    `mock_source`：
      "1" / "true" / "auto" → 自动选 games/ 下第一个 ai_* 游戏
      具体 game_id          → 用该游戏作为模板（必须在 games/ 下）

    流程：
      1. 找模板源目录
      2. 生成新 game_id，shutil.copytree 复制
      3. 清掉源 meta.json / .godot 缓存
      4. 写新 meta.json（标题取用户描述前 30 字）
      5. 重打 zip
      6. upsert_game_row 入库（visibility='private'）
      7. 推 game_ready WebSocket 事件
      8. 返回成功文案

    全程 0 LLM 调用，几百 ms 完成。
    """
    import json as _json
    from robot.agent.tools.game_tools import (
        GAMES_DIR, _generate_game_id, _coerce_i18n_map,
    )
    from robot.agent.tools import _context as ctx

    # 1. 选模板源
    flag_lower = mock_source.lower()
    if flag_lower in ("1", "true", "yes", "auto"):
        ai_dirs = sorted([
            p for p in GAMES_DIR.iterdir()
            if p.is_dir() and p.name.startswith("ai_") and (p / "scenes" / "main.gd").exists()
        ])
        if not ai_dirs:
            return "Mock create failed: no ai_* games found in games/ to copy from"
        source = ai_dirs[0]
    else:
        source = GAMES_DIR / mock_source
        if not source.is_dir() or not (source / "scenes" / "main.gd").exists():
            return f"Mock create failed: source game {mock_source!r} not found / invalid"

    logger.info("[MockGame] source=%s", source.name)

    # 2. mock 路径直接用硬编码 meta —— 不调 LLM，防止 mock 模式意外消耗额度。
    title_i18n = None
    description_i18n = None
    icon = "🎮"
    title = (instruction or "Mock Game").strip()[:15] or "Mock Game"

    game_id = _generate_game_id(title)
    game_dir = GAMES_DIR / game_id
    shutil.copytree(source, game_dir)
    logger.info("[MockGame] copied → %s", game_dir.name)

    # 3. 清掉源 meta.json + Godot 缓存
    for cleanup in [game_dir / "meta.json", game_dir / ".godot"]:
        if cleanup.exists():
            if cleanup.is_dir():
                shutil.rmtree(cleanup, ignore_errors=True)
            else:
                cleanup.unlink()

    # 3.5. 预置 canonical assets（与 create_game 真实路径一致）—— 这样
    # iOS 直替路径有标准文件名可命中。
    try:
        from robot.agent.tools.game_tools import _ensure_canonical_assets
        _ensure_canonical_assets(game_dir)
    except Exception as e:
        logger.warning("[MockGame] canonical assets failed: %s", e)

    # 注：去掉 game_dir_ready 推送（与真 AI 路径一致）。iOS 改成等 game_ready
    # 后再上传到 background.png，那时 DB 行已存在，上传不会 404；同时避免
    # 早期 DB 写入造成的草稿污染问题。

    # 4. 写新 meta.json
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    sid = None  # mock 不调 Claude Code SDK，没有 session id
    title_map = _coerce_i18n_map(title_i18n, title)
    desc = (instruction or "")[:200]
    desc_map = _coerce_i18n_map(description_i18n, desc)
    # Mock 没真实 AI 输出 → 这里捏一个示例 replaceable_assets，让 iOS view tab
    # image panel 有内容可列。背景永远 background.png（host 预创）。
    meta = {
        "game_id": game_id,
        "title": title,
        "title_i18n": title_map,
        "icon": icon,
        "description": desc,
        "description_i18n": desc_map,
        "input_mode": input_mode,
        "created_at": int(time.time()),
        "modified_at": int(time.time()),
        "owner_id": owner_id,
        "session_id": sid,
        "template_from": source.name,
        "replaceable_assets": [
            {"filename": "background.png", "role": "background",
             "label_en": "Background", "label_zh": "背景"},
        ],
        "history": [{"role": "user", "content": desc}],
    }
    (game_dir / "meta.json").write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # 取消检查（与真 AI 路径一致）—— 用户取消 / WS 断在 mock 完成前都
    # 应该清干净，不留草稿。
    cancel_check = ctx._current_cancel_check.get()
    if cancel_check and cancel_check():
        logger.warning("[MockGame] cancelled before zip/DB — cleaning up %s", game_id)
        from robot.games._session_paths import wipe_game_artifacts
        wipe_game_artifacts(game_dir)
        return f"Game creation cancelled by user: {game_id}"

    # 5. 重打 zip
    zip_path = GAMES_DIR / f"{game_id}.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))
    logger.info("[MockGame] zipped %d KB", zip_path.stat().st_size // 1024)

    # 6. DB 入库
    try:
        from robot.games.service import upsert_game_row
        await upsert_game_row(
            game_id=game_id,
            owner_id=str(owner_id or ""),
            title=title,
            title_i18n=title_map,
            description=desc,
            description_i18n=desc_map,
            icon=icon,
            input_mode=input_mode,
            template_from=source.name,
            session_id=sid,
            visibility="private",
            kind="ai",
        )
    except Exception as e:
        logger.warning("[MockGame] upsert_game_row failed (non-fatal): %s", e)

    # 7. 推 game_ready
    if ctx._ws_manager:
        await ctx._ws_manager.send_to_user(
            owner_id, user_id,
            {
                "type": "robot_command",
                "data": {
                    "action": "game_ready",
                    "game_id": game_id,
                    "title": title,
                    "title_i18n": title_map,
                    "description_i18n": desc_map,
                    "icon": icon,
                    "input_mode": input_mode,
                },
            },
        )
        logger.info("[MockGame] ✓ game_ready sent | game_id=%s", game_id)

    return f"Mock game created: {title} (game_id={game_id}, source={source.name})"
