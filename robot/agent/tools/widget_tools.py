"""Widget creation tools — create_widget / modify_widget via Claude Agent SDK.

Mirrors the structure of game_tools.py but targets H5 widgets rendered in
WKWebView on iOS. The runtime contract is documented in
`widgets/_template/CLAUDE.md` and is the source of truth for what AI is
allowed to generate.
"""

import asyncio
import hashlib
import json
import logging
import os as _os
import re as _re
import shutil
import time
from pathlib import Path

# Per-message inactivity timeout（秒）：两个 token 之间超过此值 abort。
_WIDGET_INACTIVITY_TIMEOUT = float(
    _os.environ.get("ROBOT_WIDGET_AGENT_INACTIVITY_TIMEOUT", "300")  # 5 min
)
# 总 wall-clock 预算（秒）：防 extended thinking 绕过 inactivity timeout。
_WIDGET_WALL_CLOCK_BUDGET = float(
    _os.environ.get("ROBOT_WIDGET_AGENT_WALL_CLOCK_TIMEOUT", "600")  # 10 min
)

from langchain_core.tools import tool

from robot.agent.tools import _context as ctx
from robot.agent.tools import _sdk_patch  # noqa: F401  apply MessageParseError tolerance
from robot.config import settings
from robot.i18n import t

logger = logging.getLogger(__name__)


def _is_mock_creation_mode() -> bool:
    """When ``ROBOT_MOCK_CREATION`` is truthy in the environment, skip the
    Claude Code SDK call and produce a minimal scaffold from the template (or
    no-op modify) so the rest of the pipeline (zip, DB upsert, ``widget_ready``
    push) can be smoke-tested end-to-end without spending real LLM tokens.

    Reads ``os.environ`` directly so that ``monkeypatch.setenv`` in tests and
    runtime ``.env`` changes both take effect without reloading the singleton.
    """
    raw = _os.environ.get("ROBOT_MOCK_CREATION", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}

# Directories
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WIDGETS_DIR = _BACKEND_ROOT / "widgets"
TEMPLATE_DIR = WIDGETS_DIR / "_template"
SEEDS_DIR = WIDGETS_DIR / "seeds"

# Max length for widget description stored in meta.json
_MAX_DESCRIPTION_LEN = 100

# File extensions that are considered "widget source" — used for manifest
# diffing and text-based packaging. Binary fonts under assets/fonts/ are
# packaged in the zip but not treated as text for diff.
_WIDGET_TEXT_EXTS = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".md"}

# System prompt for Claude Agent SDK when generating widgets.
_WIDGET_SYSTEM_PROMPT = """\
You are an expert front-end developer building tiny self-contained H5 widgets
for the Motem iOS dashboard. Each widget is rendered inside a sandboxed
WKWebView at one of four sizes (small, medium, large, xlarge).

## Project structure (cwd = widget folder, use ONLY relative paths)

- `index.html` — DOM skeleton. MODIFY THIS.
- `app.js` — runtime logic. MODIFY THIS.
- `meta.json` — title, description, supported_sizes, required_data. MODIFY THIS.
- `assets/base.css` — DS design tokens. DO NOT MODIFY.
- `assets/fonts/VT323-Regular.ttf` — pixel font, registered by base.css. DO NOT MODIFY.
- `CLAUDE.md` — your style/runtime guide. READ FIRST. DO NOT MODIFY.

You may add extra files under `assets/` (CSS, JS, SVG). NEVER reference external URLs for fonts/CSS/JS — only HTTPS data fetches via `fetch()` are allowed.

CRITICAL: All Read/Write/Edit paths MUST be relative (e.g. `app.js`, `assets/extra.css`). Absolute paths or `..` are forbidden.

## Visual rules

Your widget MUST follow the existing design tokens. The base.css already defines:

```css
:root {
  --ds-fg: #9B9AA1;
  --ds-bg: #16151B;
  --ds-pixel-font: 'VT323', 'Courier New', monospace;
  --ds-radius: 16px;
}
```

- Background MUST be transparent. The host SwiftUI page paints the page bg.
- Text color MUST be `var(--ds-fg)` (with optional opacity for dim labels).
- Font MUST be `var(--ds-pixel-font)`. Do NOT @import Google Fonts. Do NOT hardcode `#fff`/`#000`/`Arial`.
- Use UPPERCASE + wide letter-spacing for labels (matches existing widgets).
- Avoid heavy box-shadows; the host overlays its own CRT pixel grid.

## Host runtime API (already injected at document-start)

```js
window.motem = {
  locale: "en" | "zh-Hans",
  size:   "small" | "medium" | "large" | "xlarge",
  params: { /* user-set widget params, optional */ },
  apiBase: "http://...",       // backend base URL
  authBearer: "<jwt>",         // user's access token, used by fetchData
  fetchData: async fn(channel) // returns payload or null, see "Backend channels"
  onTimeTick: fn(cb),          // cb(Date) every 1s
}
```

Do NOT use `setInterval` for clock updates — use `onTimeTick`. Setting your own timer prevents the host from suspending offscreen widgets.

## Backend channels (PREFERRED data source)

For weather / todos / pomodoro / calendar etc, ALWAYS pull from the backend
via `fetchData(channel)`. Do NOT rely on `window.motem.data.weather`
or `onDataChange` (deprecated; only kept for legacy widgets).

Available channels:
- `weather` → `{temp, feels_like, humidity, description, icon, city}`
- `todos`   → `{count, pending, items: [{title, done}]}`
- `pomodoro`→ `{state, remaining_s}`

```js
async function loadWeather() {
  const w = await window.motem.fetchData('weather');
  if (!w) {
    // backend hasn't cached this channel yet, or fetch errored — show
    // a friendly placeholder, never blank.
    document.getElementById('temp').textContent = '--';
    return;
  }
  document.getElementById('temp').textContent = Math.round(w.temp) + '°';
  document.getElementById('city').textContent = w.city || '';
}
loadWeather();
// Refresh every 5 minutes for weather; per channel cadence varies.
setInterval(loadWeather, 300000);
```

`fetchData` already adds Authorization + Accept-Language headers, an 8s
timeout, and try/catch. It returns the payload directly (the JSON
wrapper `{channel, payload, updated_at}` is unwrapped for you), or null on
any error.

## External network (custom data, not in backend channels)

For widgets that need data NOT covered by backend channels (e.g., NBA score,
news headlines), use `fetch()` over HTTPS directly:

- HTTPS only. Plain http:// is blocked by ATS.
- Wrap every fetch in try/catch and add `signal: AbortSignal.timeout(8000)`.
- On failure render a friendly placeholder. NEVER show a blank screen.

## i18n

All player-facing strings MUST be `{en, "zh-Hans"}` dicts; branch on `window.motem.locale`. Internal logs / variables / comments stay English.

## Forbidden

- `window.open`, `eval`, `new Function`, dynamic `import()`
- `localStorage`, `sessionStorage`, `indexedDB`
- `<iframe>` loading remote pages
- camera, microphone, geolocation, notifications, vibrate, clipboard
- third-party fonts via `@import` / Google Fonts

## Final response format (after edits, separated by `---`)

```
---
description_i18n: {"en": "one English sentence describing the widget", "zh-Hans": "一句简体中文描述当前小组件"}
supported_sizes: ["medium", "large"]
```

The host parses this to update meta.json and the size picker. Output a complete final response in this format and nothing else after the separator.
"""


# ── helpers ───────────────────────────────────────────────────────────────


def _tr(key: str, **kwargs: object) -> str:
    return t(ctx.get_user_lang(), key, **kwargs)


def _generate_widget_id(title: str) -> str:
    ts = int(time.time())
    h = hashlib.md5(f"{title}{ts}".encode()).hexdigest()[:6]
    return f"aw_{ts}_{h}"


def _coerce_i18n_map(value: dict | None, fallback: str) -> dict[str, str]:
    if not isinstance(value, dict):
        value = {}
    en = str(value.get("en") or fallback).strip()[:_MAX_DESCRIPTION_LEN]
    zh = str(value.get("zh-Hans") or value.get("zh") or fallback).strip()[:_MAX_DESCRIPTION_LEN]
    return {
        "en": en or fallback[:_MAX_DESCRIPTION_LEN],
        "zh-Hans": zh or fallback[:_MAX_DESCRIPTION_LEN],
    }


def _coerce_sizes(value: object) -> list[str]:
    """Validate supported_sizes; default to ['medium','large'] when invalid."""
    allowed = {"small", "medium", "large", "xlarge"}
    if isinstance(value, list):
        cleaned = [s for s in value if isinstance(s, str) and s in allowed]
        if cleaned:
            # Preserve order, dedupe
            seen, out = set(), []
            for s in cleaned:
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            return out
    return ["medium", "large"]


def _extract_description_i18n_from_result(result: str) -> dict[str, str] | None:
    for line in result.splitlines():
        line = line.strip()
        if not line.lower().startswith("description_i18n:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        if isinstance(parsed, dict):
            en = str(parsed.get("en", "")).strip()
            zh = str(parsed.get("zh-Hans", parsed.get("zh", ""))).strip()
            if en and zh:
                return {"en": en[:_MAX_DESCRIPTION_LEN], "zh-Hans": zh[:_MAX_DESCRIPTION_LEN]}
    return None


def _extract_supported_sizes_from_result(result: str) -> list[str] | None:
    for line in result.splitlines():
        line = line.strip()
        if not line.lower().startswith("supported_sizes:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        sizes = _coerce_sizes(parsed)
        if sizes:
            return sizes
    return None


async def _send_widget_progress(message: str) -> None:
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    if ctx._ws_manager:
        try:
            await ctx._ws_manager.send_to_user(owner_id, user_id, {
                "type": "robot_command",
                "data": {"action": "widget_progress", "message": message},
            })
        except Exception as e:
            logger.error("[WidgetTool] progress send failed: %s", e)
    else:
        logger.warning("[WidgetTool] progress skipped (no ws_manager): %s", message)


async def _send_widget_progress_key(key: str, **kwargs: object) -> None:
    await _send_widget_progress(_tr(key, **kwargs))


def _compute_file_manifest(widget_dir: Path) -> dict[str, str]:
    """Return {relative_path: content} for editable text files in the widget directory."""
    manifest: dict[str, str] = {}
    for f in widget_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _WIDGET_TEXT_EXTS:
            continue
        # Skip CLAUDE.md — it is documentation for AI and should not ship to the client.
        if f.name == "CLAUDE.md":
            continue
        rel = str(f.relative_to(widget_dir))
        try:
            manifest[rel] = f.read_text(encoding="utf-8")
        except Exception:
            pass
    return manifest


def _compute_diffs(old_manifest: dict, new_manifest: dict) -> list[dict]:
    diffs = []
    all_files = set(old_manifest.keys()) | set(new_manifest.keys())
    for path in sorted(all_files):
        old_content = old_manifest.get(path)
        new_content = new_manifest.get(path)
        if old_content is None and new_content is not None:
            diffs.append({"op": "add", "path": path, "content": new_content})
        elif old_content is not None and new_content is None:
            diffs.append({"op": "delete", "path": path})
        elif old_content != new_content:
            diffs.append({"op": "modify", "path": path, "content": new_content})
    return diffs


async def _run_widget_code_agent(
    cwd: str,
    prompt: str,
    max_turns: int = 12,
    session_id: str = "",
) -> tuple[str, str, int]:
    """Run Claude Code SDK to generate/modify widget code.

    Returns (result_text, session_id, files_written_count).
    """
    import os

    # Token 模式自动分发，详见 game_tools._run_claude_code_sdk 注释。
    # OAuth `sk-ant-oat01-*` 必须走 CLAUDE_CODE_OAUTH_TOKEN，否则 401。
    auth_token = (settings.anthropic_auth_token or "").strip()
    is_oauth = auth_token.startswith("sk-ant-oat01-")
    if auth_token and is_oauth:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = auth_token
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ["ANTHROPIC_API_KEY"] = ""
    elif auth_token:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = auth_token
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ["ANTHROPIC_API_KEY"] = ""
    elif settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if settings.anthropic_base_url:
        os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    else:
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    try:
        from claude_code_sdk import (
            query, ClaudeCodeOptions,
            AssistantMessage, UserMessage, SystemMessage, ResultMessage,
            TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )
    except ImportError:
        logger.error("[WidgetTool] claude-code-sdk not installed")
        return _tr("CLAUDE_SDK_NOT_INSTALLED"), "", 0

    # widget 用独立 model 配置；空时让 SDK 用默认值。
    chosen_model = settings.widget_code_model.strip() or None

    # Per-widget session storage + sliding-window compaction —— 完全复用 game 侧
    # 的实现（函数名虽叫 `per_game_*` 但内部 cwd-参数化，对任意目录都成立；
    # 后续 C1 重构会把它挪到 `robot/_artifact_session_paths.py`）。
    # 设计与代价见 docs/widget-agent-optimization-plan.md §六 + game_tools.py:848-913。
    from robot.games._session_paths import (
        per_game_config_dir as _per_widget_config_dir,
        per_game_session_jsonl as _per_widget_session_jsonl,
        global_session_jsonl as _global_session_jsonl,
        compact_session as _compact_session,
        count_prompt_boundaries as _count_prompt_boundaries,
        flush_active_to_archive as _flush_active_to_archive,
    )
    per_widget_dir = _per_widget_config_dir(cwd)

    def _make_options(resume_session: str) -> ClaudeCodeOptions:
        opts = ClaudeCodeOptions(
            system_prompt=_WIDGET_SYSTEM_PROMPT,
            cwd=cwd,
            model=chosen_model,
            allowed_tools=["Write", "Edit", "Read", "Glob", "Grep"],
            mcp_servers=None,
            max_turns=max_turns,
            permission_mode="acceptEdits",
            # 把 session jsonl 放到 <widget_dir>.claude/ —— remix 走 shutil.copytree
            # 自动带上历史，删 widget 时 sibling 跟着删干净，不留孤儿。
            env={"CLAUDE_CONFIG_DIR": str(per_widget_dir)},
            # Token diet — game_tools 同款（见 docs/modify-game-cost-optimization.md）:
            #   * `tools` schema 白名单：SDK 默认会注入 ~82 个 tool schema
            #     (Bash/Agent/TeamCreate/Monitor/Cron/Worktree/...) 共 ~126KB
            #     给模型，与 allowed_tools(permission 层) 是两件事。这里只发
            #     5 个 widget 真正用到的 ~15KB。
            #   * `bare` 关掉 CLAUDE.md auto-discovery / hooks / LSP / skills /
            #     keychain。否则 SDK 启动时会把 ~/.claude/CLAUDE.md + 仓库内
            #     CLAUDE.md + OMC skill 目录 + project-memory 全部塞进
            #     messages[0]，单次实测 ~55KB。
            extra_args={
                "tools": "Write,Edit,Read,Glob,Grep",
                "bare": None,
            },
        )
        if resume_session:
            opts.resume = resume_session
        return opts

    # Legacy migration: 旧 widget session 落在 ~/.claude/projects/<slug>/<sid>.jsonl
    # （per-widget CLAUDE_CONFIG_DIR 之前的版本）。如果 per-widget 路径空但
    # global 有，迁移一次。失败也无所谓 —— 下面 retry-without-resume 路径兜底。
    if session_id:
        pw_jsonl = _per_widget_session_jsonl(cwd, session_id)
        gl_jsonl = _global_session_jsonl(cwd, session_id)
        if not pw_jsonl.is_file() and gl_jsonl.is_file():
            try:
                pw_jsonl.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gl_jsonl, pw_jsonl)
                logger.info(
                    "[WidgetTool] migrated legacy session %s… (global → per-widget)",
                    session_id[:12],
                )
            except OSError as e:
                logger.warning(
                    "[WidgetTool] legacy session migration failed for %s… (%s); "
                    "will fall back to retry-without-resume",
                    session_id[:12], e,
                )

    # Sliding-window compaction —— 阈值比 game 小一档（widget 单文件 + diff 小，
    # 累积更慢；50KB 触发可能永远不到）。命中后把最旧 prompt 切到 archive。
    # 失败则 cliff-fallback 起 fresh sid，靠当前 prompt + 代码现状续接。
    WIDGET_SESSION_TRIM_TRIGGER_BYTES = 30_000   # ~50K tokens input ceiling
    WIDGET_SESSION_TRIM_TARGET_BYTES = 15_000    # ~25K tokens floor
    WIDGET_SESSION_MIN_PROMPTS_KEPT = 1

    if session_id:
        pw_jsonl = _per_widget_session_jsonl(cwd, session_id)
        if pw_jsonl.is_file() and pw_jsonl.stat().st_size > WIDGET_SESSION_TRIM_TRIGGER_BYTES:
            old_kb = pw_jsonl.stat().st_size // 1024
            # 边界探测（与 game_tools 同款修复，见 fix(agent): skip compact when
            # promptId boundaries insufficient...）：compact_session 需要
            # ≥ MIN+1 个 promptId 才能切。SDK 每次 query() 只产生 1 个
            # promptId，所以**首次** modify 时 jsonl 必然只有 create 留下的 1
            # 个 promptId → 旧代码 compact 必失败 → cliff fallback → fresh
            # sid → 下次又是 1 个 promptId → 永远跑不到累积 ≥ 2 promptId 那一
            # 刻。修法：边界不够时**跳过 compact 但保留 session_id**，让本轮
            # SDK 把新 prompt 追加进同一份 active jsonl。
            n_prompts = _count_prompt_boundaries(cwd, session_id)
            if n_prompts <= WIDGET_SESSION_MIN_PROMPTS_KEPT:
                logger.info(
                    "[WidgetTool] session %s… size %dKB but only %d promptId(s) "
                    "(need >%d); skipping compact, letting jsonl accumulate "
                    "boundaries (resume continues)",
                    session_id[:12], old_kb, n_prompts,
                    WIDGET_SESSION_MIN_PROMPTS_KEPT,
                )
            else:
                ok = _compact_session(
                    cwd, session_id,
                    target_bytes=WIDGET_SESSION_TRIM_TARGET_BYTES,
                    min_prompts_kept=WIDGET_SESSION_MIN_PROMPTS_KEPT,
                )
                if ok:
                    new_kb = pw_jsonl.stat().st_size // 1024
                    logger.info(
                        "[WidgetTool] compacted session %s…  %dKB → %dKB (head archived)",
                        session_id[:12], old_kb, new_kb,
                    )
                else:
                    # 边界够但 compact 仍 False —— IO / malformed jsonl / 找不
                    # 到满足 target 的切点。真失败才走 cliff fallback。
                    logger.warning(
                        "[WidgetTool] session %s… size %dKB with %d promptIds "
                        "but compact failed (likely IO / malformed jsonl); "
                        "flushing to archive + cliff-fallback to fresh",
                        session_id[:12], old_kb, n_prompts,
                    )
                    _flush_active_to_archive(cwd, session_id)
                    try:
                        pw_jsonl.unlink()
                    except OSError as e:
                        logger.warning(
                            "[WidgetTool] could not unlink orphan active %s (%s)",
                            pw_jsonl, e,
                        )
                    session_id = ""

    options = _make_options(session_id)
    if session_id:
        logger.info("[WidgetTool] resuming session %s", session_id[:12])

    result_text = ""
    out_session_id = ""
    turn = 0
    tool_calls_total = 0
    files_written: list[str] = []
    progress_sent_design = False
    t0 = time.time()

    logger.info("[WidgetTool] ═══ START | model=%s cwd=%s prompt_len=%d ═══",
                chosen_model or "default", cwd, len(prompt))

    try:
        # 与 game_tools 同款：显式 iter + per-message inactivity timeout +
        # wall-clock 总预算，防 extended thinking 死循环烧额度。
        agen = query(prompt=prompt, options=options).__aiter__()
        t_last = t0
        while True:
            try:
                message = await asyncio.wait_for(
                    agen.__anext__(), timeout=_WIDGET_INACTIVITY_TIMEOUT,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                gap = time.time() - t_last
                logger.warning(
                    "[WidgetTool] ⚠ inactivity timeout at turn %d (no message for %.1fs); aborting",
                    turn, gap,
                )
                await _send_widget_progress_key("WIDGET_PROGRESS_TIMEOUT")
                break
            now = time.time()
            t_last = now
            if now - t0 > _WIDGET_WALL_CLOCK_BUDGET:
                logger.warning(
                    "[WidgetTool] ⚠ wall-clock budget %.0fs exceeded at turn %d; aborting",
                    _WIDGET_WALL_CLOCK_BUDGET, turn,
                )
                await _send_widget_progress_key("WIDGET_PROGRESS_TIMEOUT")
                break
            elapsed = now - t0

            if isinstance(message, SystemMessage):
                if message.subtype == "api_retry":
                    logger.warning("[WidgetTool] +%.1fs API_RETRY attempt=%s",
                                   elapsed, message.data.get("attempt"))

            elif isinstance(message, AssistantMessage):
                turn += 1
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        logger.info("[WidgetTool] +%.1fs [turn %d] 💭 thinking (%d chars)",
                                    elapsed, turn, len(block.thinking or ""))
                        if not progress_sent_design:
                            progress_sent_design = True
                            await _send_widget_progress_key("WIDGET_PROGRESS_ANALYZING")

                    elif isinstance(block, TextBlock) and block.text.strip():
                        preview = block.text[:120].replace("\n", " ")
                        logger.info("[WidgetTool] +%.1fs [turn %d] 💬 %s",
                                    elapsed, turn, preview)
                        result_text += block.text

                    elif isinstance(block, ToolUseBlock):
                        tool_calls_total += 1
                        tool_input = block.input or {}
                        if block.name in ("Write", "Edit"):
                            fpath = tool_input.get("file_path", tool_input.get("path", "?"))
                            files_written.append(fpath)
                            logger.info("[WidgetTool] +%.1fs [turn %d] ✏️  %s → %s",
                                        elapsed, turn, block.name, fpath)
                            await _send_widget_progress_key("WIDGET_PROGRESS_WRITING", path=fpath)
                        elif block.name == "Read":
                            fpath = tool_input.get("file_path", "?")
                            logger.info("[WidgetTool] +%.1fs [turn %d] 📖 Read → %s",
                                        elapsed, turn, fpath)

            elif isinstance(message, UserMessage):
                blocks = message.content if isinstance(message.content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        status = "❌ ERROR" if block.is_error else "✅"
                        preview = str(block.content)[:100].replace("\n", " ") if block.content else ""
                        logger.info("[WidgetTool] +%.1fs [turn %d] %s tool result: %s",
                                    elapsed, turn, status, preview)

            elif isinstance(message, ResultMessage):
                if message.session_id:
                    out_session_id = message.session_id
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "?"
                logger.info(
                    "[WidgetTool] ═══ DONE | turns=%d tool_calls=%d files=%d elapsed=%.1fs cost=%s ═══",
                    turn, tool_calls_total, len(set(files_written)), elapsed, cost,
                )
                n = len(set(files_written))
                if n > 0:
                    await _send_widget_progress_key("WIDGET_PROGRESS_CODE_DONE", count=n)
                else:
                    await _send_widget_progress_key("WIDGET_PROGRESS_ANALYSIS_DONE")

    except Exception as e:
        if session_id and turn == 0 and tool_calls_total == 0:
            logger.warning("[WidgetTool] resume %s… failed (%s); retrying without resume",
                           session_id[:12], str(e)[:100])
            return await _run_widget_code_agent(cwd, prompt, max_turns=max_turns, session_id="")
        logger.error("[WidgetTool] ✗ error after +%.1fs: %s", time.time() - t0, e, exc_info=True)
        return _tr("WIDGET_CODE_GENERATION_ERROR", e=e), out_session_id, len(set(files_written))

    if session_id and turn == 0 and tool_calls_total == 0:
        logger.warning("[WidgetTool] resume %s… produced no output; retrying without resume",
                       session_id[:12])
        return await _run_widget_code_agent(cwd, prompt, max_turns=max_turns, session_id="")

    if files_written:
        logger.info("[WidgetTool] files written: %s", ", ".join(dict.fromkeys(files_written)))
    return result_text, out_session_id, len(set(files_written))


# ── public tools ──────────────────────────────────────────────────────────


@tool
async def create_widget(
    title: str,
    description: str,
    icon: str = "🧩",
    template_widget_id: str = "",
    title_i18n: dict | None = None,
    description_i18n: dict | None = None,
) -> str:
    """Create a brand-new H5 widget when the user asks to make or build a widget.

    **Idempotent / always-call**: Every "do a widget / 做个组件" utterance is a
    fresh request for a NEW widget. Even if the conversation history shows you
    already called `create_widget` successfully earlier, you MUST call this
    tool again — DO NOT skip with text like "you already have a widget /
    已经创建了". Each successful call yields a different `widget_id`.

    Args:
        title: Widget name, e.g. "Lakers Score" or "Countdown Timer".
        description: Detailed widget description (gameplay/data needs/visuals).
        icon: Emoji icon representing the widget theme.
        template_widget_id: Optional existing widget ID to use as a template.
            Empty means use the default `_template`.
        title_i18n: Optional bilingual title map with "en" and "zh-Hans".
        description_i18n: Optional bilingual description map with "en" and "zh-Hans".
    """
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    t0 = time.time()
    logger.info("[WidgetTool] ▶ create_widget | title=%r owner=%s", title, owner_id)

    if not TEMPLATE_DIR.is_dir():
        logger.error("[WidgetTool] template dir missing: %s", TEMPLATE_DIR)
        return _tr("WIDGET_TEMPLATE_MISSING")

    widget_id = _generate_widget_id(title)
    widget_dir = WIDGETS_DIR / widget_id

    # Resolve template source: user-selected widget > seed > _template fallback.
    source_dir = TEMPLATE_DIR
    template_label = "_template"
    if template_widget_id and not _re.match(r'^[a-zA-Z0-9_-]{1,64}$', template_widget_id):
        logger.warning("[WidgetTool] template_widget_id rejected (invalid format): %r", template_widget_id)
        template_widget_id = ""
    if template_widget_id:
        candidate = WIDGETS_DIR / template_widget_id
        seed_candidate = SEEDS_DIR / template_widget_id
        if candidate.is_dir() and (candidate / "index.html").exists():
            source_dir = candidate
            template_label = template_widget_id
            logger.info("[WidgetTool] using user widget template: %s", template_widget_id)
        elif seed_candidate.is_dir() and (seed_candidate / "index.html").exists():
            source_dir = seed_candidate
            template_label = f"seed/{template_widget_id}"
            logger.info("[WidgetTool] using seed template: %s", template_widget_id)
        else:
            logger.warning("[WidgetTool] template %s invalid, falling back to _template",
                           template_widget_id)

    await _send_widget_progress_key("WIDGET_PROGRESS_PREPARING_TEMPLATE")
    shutil.copytree(source_dir, widget_dir)

    # Strip the source widget's meta.json so AI starts fresh.
    if template_label != "_template":
        meta_to_clear = widget_dir / "meta.json"
        if meta_to_clear.exists():
            meta_to_clear.unlink()

    logger.info("[WidgetTool] [1/3] widget dir created: %s (template=%s)",
                widget_dir, template_label)

    if template_label != "_template":
        prompt = f"""Create a new widget named "{title}" based on an existing similar widget.

User's widget description:
{description}

The template widget's code is already in the project directory. Read CLAUDE.md and index.html first.

## Task
1. Read index.html and app.js to understand the template.
2. Modify the code to implement the requested new widget. Prefer Edit for local changes.
3. Update meta.json with the new title, description (en + zh-Hans) and supported_sizes.
4. If extra files are needed, create them under assets/.

Important: all Read/Write/Edit paths must be relative (e.g. `app.js`). Absolute paths are forbidden.
Do not modify assets/base.css or assets/fonts/.

The widget must work at every size listed in meta.json["supported_sizes"]; adapt with @media or by reading window.motem.size.

Final response: exactly the description_i18n + supported_sizes block defined in CLAUDE.md."""
    else:
        prompt = f"""Create a new widget named "{title}".

User's widget description:
{description}

## Project layout (already copied from the template)
- index.html — DOM skeleton, modify with Write or Edit
- app.js — runtime logic, modify with Write
- meta.json — title/description/supported_sizes, update with Write
- assets/base.css — design tokens, READ-ONLY
- CLAUDE.md — runtime contract, READ FIRST

Important: all Read/Write/Edit paths must be relative (e.g. `app.js`). Absolute paths are forbidden.

## Task
1. Read CLAUDE.md and index.html before any edit.
2. Implement the widget using the host runtime API documented in CLAUDE.md.
3. Update meta.json with title, description (en + zh-Hans) and supported_sizes.
4. Use var(--ds-fg) and var(--ds-pixel-font) for all visuals.
5. If the widget needs to fetch external data, use HTTPS fetch() with try/catch and an 8s AbortSignal.timeout.

The widget must work at every size listed in meta.json["supported_sizes"]; adapt with @media or by reading window.motem.size.

Final response: exactly the description_i18n + supported_sizes block defined in CLAUDE.md."""

    if _is_mock_creation_mode():
        # Mock mode — skip the SDK; touch app.js with a marker so the
        # `template_unchanged` failure check below does NOT fire (otherwise
        # mock-created widgets would always be rejected).
        app_path = widget_dir / "app.js"
        if app_path.exists():
            app_path.write_text(
                app_path.read_text(encoding="utf-8")
                + f"\n// [MOCK] {title} (ROBOT_MOCK_CREATION=1)\n",
                encoding="utf-8",
            )
        result = "[MOCK] empty scaffold from template (ROBOT_MOCK_CREATION=1)"
        sid = ""
        file_count = 1
        logger.info(
            "[WidgetTool] [2/3] MOCK MODE — skipped CodeAgent, marked app.js")
    else:
        logger.info("[WidgetTool] [2/3] launching CodeAgent for %r ...", title)
        await _send_widget_progress_key("WIDGET_PROGRESS_GENERATING_CODE")
        result, sid, file_count = await _run_widget_code_agent(str(widget_dir), prompt)
        logger.info("[WidgetTool] [2/3] CodeAgent done | elapsed=%.1fs result_len=%d files=%d",
                    time.time() - t0, len(result), file_count)

    # Failure detection — three signals collapsed to "incomplete":
    #
    #   (a) AI 一个文件都没写 (file_count == 0)：上游模型 API 错误（如本次
    #       OpenRouter 402 credits 不够 / qwen 不支持 max_tokens=32000）/ SDK
    #       parse error 直接退出 / 用户客户端取消 turn 都会落到这里。
    #       不依赖 source template 内容，最鲁棒。
    #   (b) 与 source template 完全一致：AI 跑了但没改文件（罕见 — 比如它写
    #       了文件然后又删除了）。对比 **source_dir** 而非 hardcode TEMPLATE_DIR
    #       —— 走 user template / seed template 路径时 source 不是 _template，
    #       旧代码硬对比 _template 永远 mismatch，让本次这种 fail 路径漏网。
    #   (c) index.html 直接缺失：file_count > 0 但都不是 index.html。
    #
    # 任一命中：rmtree widget_dir、不进 zip / DB upsert / widget_ready，给客户端
    # 返回 INCOMPLETE 错误（fast path streaming layer 会把 result 推回给 iOS toast）。
    index_path = widget_dir / "index.html"
    app_path = widget_dir / "app.js"
    meta_path = widget_dir / "meta.json"

    def _bytes_equal(a: Path, b: Path) -> bool:
        return a.exists() and b.exists() and a.read_bytes() == b.read_bytes()

    incomplete_reason: str | None = None
    if file_count == 0:
        incomplete_reason = "no_files_written"
    elif (
        # Three core files all byte-identical to source → AI literally did nothing.
        # 旧逻辑只比 index.html + app.js，忽略 meta.json 让"router 选到完美
        # template，AI 只改 title/description/supported_sizes"的合法 clone
        # 场景被误判失败。把 meta.json 也加入：三者**都**与 source 一致才算
        # template_unchanged。
        _bytes_equal(index_path, source_dir / "index.html")
        and _bytes_equal(app_path, source_dir / "app.js")
        and _bytes_equal(meta_path, source_dir / "meta.json")
    ):
        incomplete_reason = "template_unchanged"
    elif not index_path.exists():
        incomplete_reason = "index_missing"

    if incomplete_reason is not None:
        logger.error(
            "[WidgetTool] generation FAILED reason=%s — cleaning up widget_dir %s",
            incomplete_reason, widget_id,
        )
        shutil.rmtree(widget_dir, ignore_errors=True)
        if ctx._ws_manager:
            try:
                await ctx._ws_manager.send_to_user(owner_id, user_id, {
                    "type": "robot_command",
                    "data": {"action": "widget_creation_failed", "reason": incomplete_reason},
                })
            except Exception as _e:
                logger.warning("[WidgetTool] widget_creation_failed notify failed: %s", _e)
        # `result` 通常已经携带 upstream error message（line 1 含 "API Error:..."），
        # 直接透传给 iOS；否则返一个通用 incomplete 文案。
        if result and "error" in result.lower():
            return result
        return _tr("WIDGET_CREATE_INCOMPLETE", title=title, widget_id=widget_id)
    if not app_path.exists():
        logger.warning("[WidgetTool] app.js missing — using source template fallback")
        if (source_dir / "app.js").exists():
            shutil.copy2(source_dir / "app.js", app_path)
        else:
            shutil.copy2(TEMPLATE_DIR / "app.js", app_path)

    # Persist canonical meta FIRST. AI may have written a partial meta.json
    # during generation (only title/description/sizes); the iOS client
    # WidgetMeta struct requires widget_id and icon as non-optional. If we
    # zip BEFORE this overwrite, the zip ships AI's partial version, iOS
    # JSONDecoder fails silently in loadMeta, aiWidgets dict stays empty,
    # MY WIDGETS section never appears. Order matters.
    title_map = _coerce_i18n_map(title_i18n, title)
    desc_from_result = _extract_description_i18n_from_result(result)
    description_map = _coerce_i18n_map(desc_from_result or description_i18n, description)
    sizes_from_result = _extract_supported_sizes_from_result(result)
    sizes = sizes_from_result or _coerce_sizes(None)

    meta = {
        "widget_id": widget_id,
        "title": title_map.get(ctx.get_user_lang(), title_map["en"]),
        "title_i18n": title_map,
        "icon": icon,
        "description": description_map.get(ctx.get_user_lang(), description_map["en"]),
        "description_i18n": description_map,
        "supported_sizes": sizes,
        "required_data": [],
        "required_permissions": [],
        "created_at": int(time.time()),
        "modified_at": int(time.time()),
        "owner_id": owner_id,
        "session_id": sid,
        "template_from": template_widget_id if template_label != "_template" else "",
        "history": [
            {"role": "user", "content": description[:200]},
            {"role": "assistant", "content": result[:200]},
        ],
    }
    (widget_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # Pack zip AFTER canonical meta is on disk so iOS gets the complete file.
    await _send_widget_progress_key("WIDGET_PROGRESS_PACKING")
    zip_path = WIDGETS_DIR / f"{widget_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(widget_dir))
    zip_size_kb = zip_path.stat().st_size // 1024
    logger.info("[WidgetTool] [3/3] zip created: %s (%d KB)", zip_path.name, zip_size_kb)

    # Persist DB row (best-effort; schema is created lazily by alembic).
    try:
        from robot.widgets.service import upsert_widget_row
        await upsert_widget_row(
            widget_id=widget_id,
            owner_id=str(owner_id or ""),
            title_i18n=title_map,
            description_i18n=description_map,
            supported_sizes=sizes,
            base_template_id=template_widget_id or "",
            visibility="private",
            kind="ai",
        )
    except Exception as e:
        logger.warning("[WidgetTool] upsert_widget_row failed (non-fatal): %s", e)

    # Notify iOS.
    if ctx._ws_manager:
        await ctx._ws_manager.send_to_user(owner_id, user_id, {
            "type": "robot_command",
            "data": {
                "action": "widget_ready",
                "widget_id": widget_id,
                "title": meta["title"],
                "title_i18n": title_map,
                "description_i18n": description_map,
                "icon": icon,
                "supported_sizes": sizes,
            },
        })
        logger.info("[WidgetTool] ✓ widget_ready sent | widget_id=%s total=%.1fs",
                    widget_id, time.time() - t0)
    else:
        logger.warning("[WidgetTool] ws_manager not available, widget_ready NOT sent")

    return _tr("WIDGET_CREATE_DONE", title=title, widget_id=widget_id)


@tool
async def modify_widget(widget_id: str, instruction: str) -> str:
    """Modify an existing widget when the user asks for a widget change.

    **Always-call**: Every "改一下 / 把 X 改成 Y" utterance is a fresh modify
    request — DO NOT short-circuit with text like "I already changed that".
    Always call the tool again with the new instruction.

    Args:
        widget_id: ID of the widget to modify.
        instruction: Modification request describing what to change.
    """
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    t0 = time.time()
    logger.info("[WidgetTool] ▶ modify_widget | widget_id=%s instruction=%r",
                widget_id, instruction[:100])

    widget_dir = WIDGETS_DIR / widget_id
    if not widget_dir.is_dir():
        logger.error("[WidgetTool] widget dir not found: %s", widget_dir)
        return _tr("WIDGET_NOT_FOUND_DETAIL", widget_id=widget_id)

    old_manifest = _compute_file_manifest(widget_dir)
    logger.info("[WidgetTool] [1/4] snapshot: %d files", len(old_manifest))

    # Pull modification history + previous session id from meta for context.
    meta_path = widget_dir / "meta.json"
    meta: dict = {}
    history: list[dict] = []
    prev_session_id = ""
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            history = meta.get("history", [])
            prev_session_id = meta.get("session_id", "")
        except Exception:
            pass

    history_text = ""
    if history:
        lines = []
        for h in history:
            prefix = "User" if h["role"] == "user" else "AI"
            lines.append(f"- {prefix}: {h['content']}")
        history_text = f"\n\n## Modification history\n" + "\n".join(lines)

    prompt = f"""Modify this existing Motem widget.

Modification request:
{instruction}
{history_text}

## Steps
1. Read CLAUDE.md, index.html and app.js to understand the current widget.
2. Apply the requested change without breaking existing features.
3. Prefer Edit for local changes; use Write only when most of a file must change.
4. If the change affects which sizes the widget supports, update meta.json["supported_sizes"].

Important: all Read/Write/Edit paths must be relative (e.g. `app.js`). Absolute paths are forbidden.

Final response: exactly the description_i18n + supported_sizes block defined in CLAUDE.md."""

    if _is_mock_creation_mode():
        # Mock mode — append a comment to app.js so the diff computer emits
        # exactly one modify diff for iOS to round-trip.
        app_path = widget_dir / "app.js"
        if app_path.exists():
            app_path.write_text(
                app_path.read_text(encoding="utf-8")
                + f"\n// [MOCK modify @ {int(time.time())}] {instruction[:80]}\n",
                encoding="utf-8",
            )
            file_count = 1
        else:
            file_count = 0
        result = "[MOCK] no-op modify (ROBOT_MOCK_CREATION=1)"
        sid = prev_session_id or ""
        logger.info(
            "[WidgetTool] [2/4] MOCK MODE — appended mock comment to app.js")
    else:
        logger.info("[WidgetTool] [2/4] launching CodeAgent for modify (resume=%s) ...",
                    prev_session_id[:12] + "..." if prev_session_id else "none")
        await _send_widget_progress_key("WIDGET_PROGRESS_MODIFYING")
        result, sid, file_count = await _run_widget_code_agent(str(widget_dir), prompt, max_turns=10, session_id=prev_session_id)
        logger.info("[WidgetTool] [2/4] CodeAgent done | elapsed=%.1fs result_len=%d",
                    time.time() - t0, len(result))

    # Update canonical meta on disk BEFORE computing diffs / zipping. AI may
    # have stomped meta.json during edit (stripped widget_id/icon). If we
    # diff first, iOS's widget_patch receives AI's partial meta, applyDiffs
    # writes the bad version to local disk, WidgetMeta decode fails, the
    # widget effectively "disappears" from MY WIDGETS. Same trap as
    # create_widget but harder to spot because the file does end up updated
    # later (it's just too late — the diff already shipped).
    try:
        history.append({"role": "user", "content": instruction[:200]})
        history.append({"role": "assistant", "content": result[:200]})
        if len(history) > 6:
            history = history[-6:]
        meta["history"] = history
        meta["modified_at"] = int(time.time())
        if sid:
            meta["session_id"] = sid
        new_desc = _extract_description_i18n_from_result(result)
        if new_desc:
            meta["description_i18n"] = new_desc
            meta["description"] = new_desc.get(ctx.get_user_lang(), new_desc["en"])
        new_sizes = _extract_supported_sizes_from_result(result)
        if new_sizes:
            meta["supported_sizes"] = new_sizes
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("[WidgetTool] failed to update meta.json: %s", e)

    new_manifest = _compute_file_manifest(widget_dir)
    diffs = _compute_diffs(old_manifest, new_manifest)
    logger.info("[WidgetTool] [3/4] diffs: %d changes | %s",
                len(diffs), ", ".join(f"{d['op']}:{d['path']}" for d in diffs))

    if not diffs:
        logger.warning("[WidgetTool] no diffs detected after modify (file_count=%d)", file_count)
        if file_count == 0 and ctx._ws_manager:
            try:
                await ctx._ws_manager.send_to_user(owner_id, user_id, {
                    "type": "robot_command",
                    "data": {"action": "widget_creation_failed", "reason": "no_files_written"},
                })
            except Exception as _e:
                logger.warning("[WidgetTool] widget_creation_failed notify failed: %s", _e)
        return _tr("WIDGET_NO_CHANGES")

    # Repack zip AFTER canonical meta is rewritten.
    await _send_widget_progress_key("WIDGET_PROGRESS_PACKING")
    zip_path = WIDGETS_DIR / f"{widget_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(widget_dir))

    if ctx._ws_manager:
        await ctx._ws_manager.send_to_user(owner_id, user_id, {
            "type": "robot_command",
            "data": {
                "action": "widget_patch",
                "widget_id": widget_id,
                "diffs": diffs,
            },
        })
        logger.info("[WidgetTool] ✓ widget_patch sent | diffs=%d total=%.1fs",
                    len(diffs), time.time() - t0)

    # Refresh DB row (best-effort).
    try:
        from robot.widgets.service import upsert_widget_row
        # modify_widget 不传 visibility — service.upsert_widget_row 在 update 路径上
        # 不会改 visibility，保持原值。这正是"作者修改自己组件不算新建"的语义。
        await upsert_widget_row(
            widget_id=widget_id,
            owner_id=str(owner_id or ""),
            title_i18n=meta.get("title_i18n", {"en": meta.get("title", widget_id), "zh-Hans": meta.get("title", widget_id)}),
            description_i18n=meta.get("description_i18n", {"en": "", "zh-Hans": ""}),
            supported_sizes=meta.get("supported_sizes", ["medium", "large"]),
            base_template_id=meta.get("template_from", ""),
        )
    except Exception as e:
        logger.warning("[WidgetTool] upsert_widget_row failed (non-fatal): %s", e)

    changed_files = [d["path"] for d in diffs]
    return _tr("WIDGET_MODIFY_DONE", count=len(diffs), files=", ".join(changed_files))
