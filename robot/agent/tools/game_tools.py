"""Game creation tools — create_game / modify_game via Claude Agent SDK."""

import asyncio
import hashlib
import json
import logging
import re as _re
import shutil
import time
from pathlib import Path

from langchain_core.tools import tool

from robot.agent.tools import _context as ctx
from robot.agent.tools import _sdk_patch as _sdk_patch  # noqa: F401  -- side-effect import
from robot.config import settings
from robot.i18n import t

# `_sdk_patch` 在 import 时 auto-apply，让 claude_code_sdk 的 parse_message 容忍
# `Unknown message type: rate_limit_event` 之类的新事件（OAuth 订阅模式服务端
# 推送的 rate-limit 提示，旧 SDK 不认识就抛 MessageParseError 中断整个 query
# stream，导致 AI 生成中途挂掉）。必须在调 `claude_code_sdk.query()` 之前 import。

logger = logging.getLogger(__name__)

# Directories
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GAMES_DIR = _BACKEND_ROOT / "games"
TEMPLATE_DIR = GAMES_DIR / "_template"   # skeleton hook mode (since Phase 5)
# 方案 C 拆分：硬件接口契约层在 `_template/`，两条路径都共享；slow path 专属
# 资源（CLAUDE.md 给 Claude Code SDK 的写代码指南、未来的 slow path 特有 hint）
# 叠加在 sibling `_template_slowpath/`，只在 slow path create 流程末尾拷一遍，
# 避免污染 fast path 生成的游戏 zip。fast path 的叠加层在 fastpath/compiler.py。
# 路径相对 TEMPLATE_DIR 推导 —— 测试 monkeypatch TEMPLATE_DIR 到 tmp 目录时
# addon 自动跟着失效，避免生产 addon 污染测试 fixture。
AUDIO_LIBRARY_DIR = _BACKEND_ROOT / "robot" / "games" / "assets" / "audio"
_AUDIO_REF_RE = None  # lazy-compiled in _harvest_audio
_BGM_TRACK_RE = None  # lazy-compiled in _harvest_audio

# Single source of truth for the cross-platform game rules. Both the
# backend SDK (here) and macOS Motem Studio's local terminal claude
# (via the template-bundle endpoint in `robot/games/router.py`) consume
# the same file content so rule changes propagate to both targets with
# one edit.
_SLOWPATH_CLAUDE_MD_PATH = GAMES_DIR / "_template_slowpath" / "CLAUDE.md"
_SLOWPATH_CLAUDE_MD_TEMPLATE: str | None = None


def _render_slowpath_claude_md() -> str:
    """Return `_template_slowpath/CLAUDE.md` with the current audio
    catalogue substituted into the `{audio_section}` literal placeholder.

    Uses `str.replace` (not `.format`) because the CLAUDE.md body contains
    GDScript snippets with curly braces (`_tr("key", {params})` etc.) that
    `.format` would choke on.
    """
    global _SLOWPATH_CLAUDE_MD_TEMPLATE
    if _SLOWPATH_CLAUDE_MD_TEMPLATE is None:
        try:
            _SLOWPATH_CLAUDE_MD_TEMPLATE = _SLOWPATH_CLAUDE_MD_PATH.read_text(
                encoding="utf-8"
            )
        except OSError:
            _SLOWPATH_CLAUDE_MD_TEMPLATE = ""
    if not _SLOWPATH_CLAUDE_MD_TEMPLATE:
        return ""
    return _SLOWPATH_CLAUDE_MD_TEMPLATE.replace(
        "{audio_section}", _build_audio_section()
    )

# Max length for game description stored in meta.json
_MAX_DESCRIPTION_LEN = 100

# game_id 强校验正则：必须与 `_generate_game_id` 产出严格对齐
# (`ai_<unix_ts>_<md5[:6]>`)。fast-path / modify_game / 外部入口都依赖这条
# 规则做路径穿越防御。
# Why: 三方 review 共识 P0 — 之前 `streaming.py` 的 [game_modify] 把 user-
# controlled `game_id` 直接拼到 `GAMES_DIR / game_id`，Python `pathlib` 在
# 右操作数是绝对路径时返回右操作数本身（不是子目录），加上 `meta.json` 缺
# 失时 ownership 静默放行 → 任意登录用户可让后端 AI agent 以服务进程身份
# 读写主机任意目录（等价 RCE）。
_GAME_ID_PATTERN = _re.compile(r"^ai_\d+_[a-f0-9]{6}$")


def _is_safe_ai_game_id(game_id: str) -> bool:
    """是否是合法的 AI 生成游戏 ID（防路径穿越 / 模板污染）。"""
    return isinstance(game_id, str) and bool(_GAME_ID_PATTERN.match(game_id))

# 最长 inactivity 间隔（秒）：Claude Code SDK 拉新消息超过这个时长就 abort。
# 设 600s（10 分钟）：复杂游戏 + extended thinking 偶尔会超过 5 分钟仍在正常
# 推进，给 OpenRouter / Sonnet 一些缓冲。超过 10 分钟通常是真的 hang（之前
# 实测有 21 分钟单次 API call 烧 $1.32 + 0 文件输出的灾难案例）。本数值通过
# 环境变量 ROBOT_CODE_AGENT_INACTIVITY_TIMEOUT 可调（秒）。
import os as _os
_INACTIVITY_TIMEOUT_SECONDS = float(
    _os.environ.get("ROBOT_CODE_AGENT_INACTIVITY_TIMEOUT", "600")
)


def _is_mock_creation_mode() -> bool:
    """When ``ROBOT_MOCK_CREATION`` is truthy in the environment, skip the
    Claude Code SDK call and produce a minimal scaffold from the template (or
    no-op modify) so the rest of the pipeline (zip, DB upsert, ``game_ready``
    push) can be smoke-tested end-to-end without spending real LLM tokens.

    Reads ``os.environ`` directly so that ``monkeypatch.setenv`` in tests and
    runtime ``.env`` changes both take effect without reloading the singleton.
    """
    raw = _os.environ.get("ROBOT_MOCK_CREATION", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}
# 总 wall-clock 上限（秒）：inactivity timeout 防不住 extended thinking 持续 emit
# 心跳 token 的场景，需要一个绝对时间预算。默认 900s（15 分钟）。
_WALL_CLOCK_BUDGET_SECONDS = float(
    _os.environ.get("ROBOT_CODE_AGENT_WALL_CLOCK_TIMEOUT", "900")
)

# System prompt for Claude Agent SDK when generating Godot games
_GODOT_SYSTEM_PROMPT_BASE = """\
You are an expert Godot 4 game developer. You write GDScript and .tscn scene files.

## Runtime environment
- Platform: iOS (iPhone/iPad), embedded inside a SwiftUI app via SwiftGodotKit
- Renderer: Forward Mobile (renderer/rendering_method="mobile" in project.godot)
- Screen: landscape, {screen_resolution} logical pixels
- NO keyboard, NO mouse — input depends on the control mode (see below)
- For tap-to-start / tap-to-restart: connect a full-screen Area2D or Button to input_event
- **UI overlay safe zones**: The host app draws overlay buttons on top of the game at the safe-area edges:
  - Top-left: exit button (X icon), occupies roughly 50x50px starting from the safe area edge
  - Top-right: modify button (pencil icon), occupies roughly 50x50px starting from the safe area edge
  Do NOT place any game UI (score, lives, HUD, buttons) within 130px horizontally and 10px vertically of the top-left or top-right corners. Keep HUD elements away from these corners — use the top-center or offset inward.

## Project structure (cwd = game project root, use relative paths only):
- project.godot — project config (DO NOT MODIFY)
- scenes/main.tscn — main scene (MODIFY THIS)
- scenes/main.gd — main script (MODIFY THIS)
- scripts/input_bridge.gd — body pose input bridge (DO NOT MODIFY, but you can use its API)
- scripts/game_manager.gd — game lifecycle signals (DO NOT MODIFY, but you can use it)
- scripts/i18n.gd — localization runtime (DO NOT MODIFY, but call it via the template `_tr(key, params)` helper)
- scripts/i18n_strings.gd — game-specific localized strings (MODIFY THIS)
- assets/ — image assets directory.
    - `assets/background.png` — full-screen background. **REQUIRED in every game**, host pre-creates a 1280x720 placeholder PNG; use a `Sprite2D` (or TextureRect) loading `res://assets/background.png` covering the entire viewport. **CRITICAL**: set `bg.z_index = -100` (or any negative value) BEFORE `add_child(bg)` — Godot 2D paints the parent Node2D's `_draw()` FIRST, then paints child Sprite2D nodes ON TOP. Without negative `z_index` the background PNG will cover all `_draw()` content (player, enemies, bullets, HUD) — screen becomes a solid placeholder color. `move_child(bg, 0)` does NOT fix this; only `z_index` does.
    - For all OTHER on-screen visual elements (player avatar, enemies, bullets, blocks, tiles, notes, obstacles, items, etc.) — pick descriptive filenames matching your game's theme (e.g. `spaceship.png` `alien.png` `laser.png` for a shooter; `wall.png` `exit.png` for a maze; `block.png` `spike.png` for a platformer). You MUST:
        1. Create the PNG file yourself (a simple solid-color rectangle written via a tiny pre-build helper script is fine — but make sure the file actually exists at `assets/<name>.png`).
        2. Load it via `Sprite2D` + `load("res://assets/<name>.png")`.
        3. Register it in the meta.json output (see "Output format" below).
    - Game element images MUST live in `assets/`. Reserve _draw()/draw_rect/etc. for HUD chrome only (score numbers, lives, debug overlays).
  Additional user-uploaded files may also live here (modify prompts list them under "## User-uploaded assets"); prefer `load("res://assets/<filename>")` for those too.

**CRITICAL**: All file operations (Read/Write/Edit) MUST use relative paths from cwd (e.g. `scenes/main.gd`). NEVER use absolute paths. NEVER access files outside the cwd directory.

{input_section}

{audio_section}

## Rules
- Write ONLY GDScript (.gd files) — do NOT use C#, no .cs files
- Use Godot 4.6 GDScript syntax ONLY — no Godot 3 syntax
- All scripts must start with `extends Node2D` or appropriate base class
- All rendering must be 2D (Node2D, Sprite2D, CharacterBody2D, Area2D, etc.)
- For visual game elements (background, player, enemies, bullets, items): MUST use Sprite2D nodes loading the canonical `res://assets/<role>.png` files (see Project structure). Reserve draw_rect/draw_circle/draw_line/_draw() for HUD elements (score bar, score text background, debug visuals) — never for the main gameplay sprites
- When using draw_rect() for filled rectangles, do NOT pass the width parameter (it has no effect and prints warnings). Use: `draw_rect(rect, color)` for filled, `draw_rect(rect, color, false, width)` for outlines only
- draw_colored_polygon() second arg must be a single Color, NOT PackedColorArray — wrong type crashes the entire script silently
- Do NOT define inner classes (`class Foo:` inside the script) — they crash in iOS embedded mode. Use Dictionary instead
- For text rendering: a CJK font is pre-configured via the project theme. See CLAUDE.md for font rules.
- Player-facing UI text MUST be localized:
  - Put every player-facing string in scripts/i18n_strings.gd.
  - Every key must contain both "en" and "zh-Hans" values.
  - Use the template `_tr("key", params_dict)` helper for draw_string(), Label text, Button text, start/retry hints, score, lives, game over, and all other game UI. Pass interpolation values as a Dictionary, e.g. `_tr("score", dict_with_score_key)`. Do NOT call `I18n.tr(...)` directly — use `_tr(...)`, which delegates to `I18n.translate(...)` and avoids colliding with Godot's built-in `Object.tr()`.
  - Do NOT hardcode player-facing Chinese or English UI text directly in scenes/main.gd.
  - Internal code, variable names, node names, comments, and logs should remain English.
- Include a complete game loop: start screen (tap to start), gameplay, game over (tap to restart)
- NEVER use keyboard input (Input.is_key_pressed, KEY_*, etc.) — iOS has no keyboard
- NEVER use Input.get_axis or Input.is_action_pressed for movement
- NEVER split a single function call across multiple lines inside a lambda (e.g. filter/map) — keep lambdas on one line
- NEVER use := for variables assigned from Dictionary values or Variant — always use explicit type annotation: `var x: float = dict["key"]`
- Use := ONLY when the right-hand side has an unambiguous concrete type (e.g. `var v := Vector2(0, 0)`)
- Write clean, commented code
- You may create additional .gd and .tscn files under scenes/ or scripts/
- NEVER use TodoWrite or any todo list tool — just write files directly

{mcp_section}"""


# MCP section 单独拎出来，按 use_mcp 条件注入。modify 路径 use_mcp=False，
# 这段 ~550 bytes / ~137 tok 完全不需要（modify 不调 create_scene / add_node）。
_GODOT_MCP_SECTION = """\
## Godot MCP Tools (available)

You have Godot MCP tools for scene manipulation:
- `create_scene`: Create a new scene file with a specified root node type (guarantees correct .tscn format)
- `add_node`: Add child nodes to an existing scene with customizable properties

**Recommended workflow**:
1. Write main.gd with the Write tool (game logic)
2. Use `create_scene` + `add_node` to build main.tscn if needed (more reliable than writing .tscn text directly)
Do NOT use run_project/get_debug_output/stop_project — validation is handled externally.
"""

# ─────────────────────────────────────────────────────────────────────────────
#  SKELETON MODE system prompt — used when ROBOT_USE_HOOK_SKELETON=1.
#  AI fills in 3 required hooks + optionally overrides 5 default ones, instead
#  of writing the whole 28K-char main.gd. Skeleton already enforces all the
#  conventional bugs (z_index, null check, .import, draw_colored_polygon...).
# ─────────────────────────────────────────────────────────────────────────────
_GODOT_SKELETON_SYSTEM_PROMPT = """\
You are an expert Godot 4 game developer writing GDScript for an
iOS-embedded (SwiftGodotKit) mobile game.

This game uses **{input_mode}** controls. The project root is your cwd;
use relative paths only (no absolute paths, no files outside cwd).
DO NOT rewrite scenes/main.gd from scratch — the skeleton there already
handles the outer shell; only fill the hooks in the AI IMPLEMENTATION
ZONE at the bottom of the file.

The full game architecture, hook list, runtime constraints, asset
structure, coding rules, and the audio catalogue are in the project
CLAUDE.md (appended below this prompt under "Game-specific rules" by
the caller). Treat it as the authoritative spec.
{mcp_section}"""


# Input documentation for touch-only mode
_TOUCH_INPUT_DOCS = """\
## Input mode: TOUCH ONLY
This game uses touch screen input only. Do NOT use InputBridge or body pose data.

### Touch input pattern
```gdscript
var touch_x := 0.0  # -1 ~ 1
var touch_active := false
var touch_drag_delta := Vector2.ZERO
var last_touch_pos := Vector2.ZERO

func _input(event):
    if event is InputEventScreenTouch:
        if event.pressed:
            touch_active = true
            last_touch_pos = event.position
        else:
            touch_active = false
            touch_x = 0.0
            touch_drag_delta = Vector2.ZERO
    elif event is InputEventScreenDrag:
        touch_x = clamp((event.position.x / get_viewport_rect().size.x) * 2.0 - 1.0, -1.0, 1.0)
        touch_drag_delta = event.position - last_touch_pos
        last_touch_pos = event.position
```
Use `touch_x` for horizontal movement, `touch_drag_delta` for direct position control.
Use InputEventScreenTouch for tap detection (start game, restart, etc.)."""

# Input documentation for body pose (visual control) mode
_BODY_POSE_INPUT_DOCS = """\
## Input mode: BODY POSE (visual control via camera)
This game uses body pose / face tracking input via the device camera.
The host app sends tracking data at ~30fps to the InputBridge autoload singleton.
Do NOT use touch input for gameplay (touch is only used for tap-to-start / tap-to-restart).

### InputBridge API
```gdscript
var bridge: Node

func _ready():
    bridge = get_node("/root/InputBridge")
```

### Available properties
| Property | Type | Range | Description |
|----------|------|-------|-------------|
| `bridge.body_lean_x` | float | -1.0 ~ 1.0 | Body lean left(−) / right(+) |
| `bridge.body_lean_y` | float | -1.0 ~ 1.0 | Body lean forward(−) / back(+) |
| `bridge.is_ducking` | bool | | Quick crouch / duck |
| `bridge.is_hands_up` | bool | | Both hands raised above shoulders |
| `bridge.is_reaching_out` | bool | | One hand extended forward |
| `bridge.face_position` | Vector2 | (0~1, 0~1) | Normalized face center on screen |
| `bridge.face_detected` | bool | | Whether a face is visible |
| `bridge.hand_gesture` | String | see below | Current hand gesture |
| `bridge.body_detected` | bool | | Whether a body is visible |

### Hand gestures
`"none"`, `"fist"`, `"thumbsUp"`, `"peace"`, `"ok"`, `"heart"`, `"wave"`

### Control pattern: POSITION CONTROL with lerp (REQUIRED)
**IMPORTANT**: Use POSITION CONTROL (lerp to target position), NOT velocity control
(position += speed * delta). Velocity control feels sluggish and unresponsive.

```gdscript
# Move player/cursor to position indicated by body lean or face tracking.
# This is the RECOMMENDED pattern — copy and adapt it.
func _move_player(delta: float) -> void:
    var target_x: float = player_pos.x
    var target_y: float = player_pos.y
    if bridge != null:
        if bridge.body_detected:
            # body_lean maps -1~1 to screen position offset from center
            var cx: float = screen_size.x * 0.5
            var cy: float = screen_size.y * 0.5
            target_x = cx + bridge.body_lean_x * cx * 1.15
            target_y = cy - bridge.body_lean_y * cy * 1.15
        elif bridge.face_detected:
            # face_position (0~1) maps to screen; scale from center to reach edges
            var fx: float = (bridge.face_position.x - 0.5) * 2.0 + 0.5
            var fy: float = (bridge.face_position.y - 0.5) * 2.0 + 0.5
            target_x = fx * screen_size.x
            target_y = fy * screen_size.y
    # Smooth follow with lerp — responsive without jitter
    var lerp_speed: float = 12.0
    player_pos.x = clampf(lerpf(player_pos.x, target_x, lerp_speed * delta),
                          margin, screen_size.x - margin)
    player_pos.y = clampf(lerpf(player_pos.y, target_y, lerp_speed * delta),
                          margin, screen_size.y - margin)
```

### Gestures as actions
```gdscript
func _process(delta):
    if bridge and bridge.is_hands_up and not bomb_used:
        trigger_bomb()
    if bridge and bridge.hand_gesture == "fist":
        activate_shield()
```

### Key rules
- Use `body_lean_x/y` (body detected) as the PRIMARY control, `face_position` as FALLBACK
- Always use `lerpf()` with `lerp_speed = 12.0` for smooth, responsive movement
- Always `clampf()` positions to keep the player within screen bounds
- NEVER use velocity-based movement (position += lean * speed * delta) — it feels like moving through oil"""


def _load_audio_manifest() -> dict:
    """Load the audio library manifest. Returns {} if file missing."""
    manifest_path = AUDIO_LIBRARY_DIR / "manifest.json"
    if not manifest_path.exists():
        return {"sounds": []}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[GameTool] failed to parse audio manifest: %s", e)
        return {"sounds": []}


def _build_assets_section(game_dir: Path) -> str:
    """Render the user-uploaded `assets/` directory into a prompt section.

    The iOS make-image sheet uploads images via POST /api/games/{id}/assets,
    landing files in `games/{id}/assets/`. Enumerating them in the modify_game
    prompt lets Claude Code reference each by name (`load("res://assets/<f>")`)
    instead of inventing placeholder shapes.

    Returns "" when assets/ is empty or missing — preserves prompt brevity for
    games without uploaded backgrounds.
    """
    assets_dir = game_dir / "assets"
    if not assets_dir.is_dir():
        return ""
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    files = sorted(
        p.name for p in assets_dir.iterdir()
        if p.is_file() and p.suffix.lower() in image_exts
    )
    if not files:
        return ""
    bullets = "\n".join(f"- assets/{name}" for name in files)
    return f"""

## Image files available in `assets/`

These image files are present in the project. **Do NOT assume the user just
uploaded them or wants to swap them** — they may be canonical placeholders
(e.g. `background.png` is auto-created on game create) or actually uploaded
content. Either way: load them with `load("res://assets/<filename>")`
(e.g. `load("res://assets/{files[0]}")`) and integrate where appropriate.

{bullets}
"""


def _build_audio_section() -> str:
    """Render the audio catalog into a system-prompt section.

    AI references sounds by ID via `_audio_play("id")` (skeleton helper that
    routes through `/root/AudioManager` by node path, so iOS embedded Godot
    can resolve audio without registering the autoload at parse time).
    Backend post-process (`_harvest_audio`) will copy each referenced .ogg
    from the master library into the game's audio/ directory at zip time.
    """
    manifest = _load_audio_manifest()
    sounds = manifest.get("sounds", [])
    if not sounds:
        # Library not yet populated — emit a neutral note so AI knows the API
        # exists but doesn't fabricate sound names.
        return """\
## Audio
- AudioManager autoload is registered, but the sound library on this server is currently empty.
- Skip audio in this game; do NOT call `_audio_play(...)` or `_audio_play_bgm(...)` until the library is populated.
"""
    by_cat: dict[str, list[dict]] = {}
    for s in sounds:
        by_cat.setdefault(s.get("category", "misc"), []).append(s)

    lines = [
        "## Audio",
        "",
        "Use the skeleton helpers (already pre-bound; they route through `/root/AudioManager` by node path so iOS embedded Godot can resolve audio without registering the autoload at parse time). Do NOT instantiate AudioStreamPlayer nodes directly and do NOT load .ogg / .wav files yourself.",
        "",
        "### API",
        "- `_audio_play(<sfx_id>)` — fire-and-forget short SFX",
        "- `_audio_play_bgm(<bgm_id>)` — start looping background music (call once per scene)",
        "- `_audio_stop_bgm()` — stop the current BGM",
        "",
        "**Strict whitelist**: `<sfx_id>` / `<bgm_id>` MUST be a string literal copied byte-for-byte from "
        "the IDs listed below. Common mistakes to avoid:",
        "- Do NOT pass `\"name\"` — that's the placeholder word, not an ID. Calling `_audio_play(\"name\")` will be dropped at packaging.",
        "- Do NOT pass category names like `\"event\"` / `\"action\"` / `\"ui\"` — those are section headers below, not IDs.",
        "- Do NOT invent IDs like `\"explosion\"` / `\"jump_sound\"` if they are not in the list — pick the closest match from the manifest instead (e.g. `hit`, `jump`).",
        "- Do NOT use a runtime variable for the ID — only a string literal from the manifest. (Runtime-computed IDs cannot be statically harvested.)",
        "",
        "### Available sound IDs (use these literal strings)",
        "",
    ]
    cat_order = ["ui", "event", "action", "robot", "bgm"]
    cat_titles = {
        "ui": "UI",
        "event": "Game events",
        "action": "Actions",
        "robot": "Robot / 8-bit",
        "bgm": "Background music (looping)",
    }
    for cat in cat_order:
        if cat not in by_cat:
            continue
        lines.append(f"**{cat_titles.get(cat, cat)}**")
        for s in by_cat[cat]:
            lines.append(f"- `{s['id']}` — {s.get('description', '')}")
        lines.append("")

    lines.extend([
        "### Usage rules",
        "- Pick names ONLY from the list above. Names not on the list will be silently dropped (file not packaged).",
        "- Pair gameplay events with audio: button clicks → `click`; score → `score_up`; lose life → `life_lost`; game over → `level_fail`; collision/hit → `hit`; pickup → `pickup`; jump/dash → `jump`/`dash`; level cleared → `level_complete`.",
        "- BGM is OPTIONAL. Pick a `bgm_*` only when the genre fits (action / arcade / casual / menu). Quiet / meditative / puzzle games may skip BGM. At most ONE BGM per scene; call `play_bgm` once when the gameplay scene starts.",
        "- Mute is handled by the host app; do NOT add your own mute toggle.",
    ])
    return "\n".join(lines)


def _build_system_prompt(
    input_mode: str = "touch",
    screen_resolution: str = "1194x834",
    use_mcp: bool = False,
) -> str:
    """Build the full Godot system prompt with the appropriate input docs.

    use_mcp=False（默认，含 modify / 现已含 create 路径）会省掉
    ## Godot MCP Tools 段（~137 tok），因为不挂 MCP server，create_scene /
    add_node 不在工具列表里。godot-mcp 历史上是 create 期望使用的（场景
    操作 / 运行时验证），但服务器端默认未装 godot-mcp 也未授权 permission，
    每次 create 会触发 `mcp__godot__list_projects permission not granted`
    warning + 浪费 turn，且 AI 总能 fallback 到 Read/Write/Edit 完成生成。
    需要 godot-mcp 时显式传 use_mcp=True。
    """
    if input_mode == "body_pose":
        input_section = _BODY_POSE_INPUT_DOCS
    else:
        input_section = _TOUCH_INPUT_DOCS
    mcp_section = _GODOT_MCP_SECTION if use_mcp else ""
    return _GODOT_SYSTEM_PROMPT_BASE.format(
        screen_resolution=screen_resolution,
        input_section=input_section,
        audio_section=_build_audio_section(),
        mcp_section=mcp_section,
    )


def _build_skeleton_system_prompt(
    input_mode: str = "touch",
    screen_resolution: str = "1194x834",  # absorbed into CLAUDE.md; kept for caller-compat
) -> str:
    """Build the SKELETON-mode system prompt.

    Now a thin opener: input mode + MCP tools toggle + full project
    `_template_slowpath/CLAUDE.md` content (rendered with the live audio
    catalogue). The CLAUDE.md is the authoritative cross-platform spec —
    same file the macOS Motem Studio terminal claude consumes via the
    template-bundle endpoint.
    """
    del screen_resolution  # canonical 1194x834 lives in CLAUDE.md
    mcp_section = ""  # MCP tools are opt-in elsewhere; SDK path keeps it empty
    return _GODOT_SKELETON_SYSTEM_PROMPT.format(
        input_mode=input_mode,
        mcp_section=mcp_section,
    )


def _tr(key: str, **kwargs: object) -> str:
    return t(ctx.get_user_lang(), key, **kwargs)


def _generate_game_id(title: str) -> str:
    ts = int(time.time())
    h = hashlib.md5(f"{title}{ts}".encode()).hexdigest()[:6]
    return f"ai_{ts}_{h}"


def _coerce_i18n_map(value: dict | None, fallback: str) -> dict[str, str]:
    if not isinstance(value, dict):
        value = {}
    en = str(value.get("en") or fallback).strip()[:_MAX_DESCRIPTION_LEN]
    zh = str(value.get("zh-Hans") or value.get("zh") or fallback).strip()[:_MAX_DESCRIPTION_LEN]
    return {"en": en or fallback[:_MAX_DESCRIPTION_LEN], "zh-Hans": zh or fallback[:_MAX_DESCRIPTION_LEN]}


def _extract_description_i18n_from_result(result: str) -> dict[str, str] | None:
    """Parse the structured bilingual summary requested from the code agent."""
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


def _sanitize_path_for_user(fpath: str, cwd: str) -> str:
    """把 AI 工具调用里的 file_path 转成给用户看的相对路径，去掉 server 本地
    路径泄漏：
      `/home/developer/work/.../games/ai_xxx/scenes/main.gd`
        → `scenes/main.gd`
      `/home/developer/.../games/ai_xxx`（目录）
        → `<game>`
      `scenes/main.gd`（已经相对路径）
        → 原样
      AI hallucinate 的 `/root/scenes/main.gd`
        → `scenes/main.gd`（兜底取 basename 后两级）

    AI 拿到工具结果里的绝对路径（File does not exist / cwd is XYZ）后会
    在后续 turn 里习惯性回填绝对路径，泄漏出去。这里在发送给 iOS 之前
    统一脱敏。
    """
    if not fpath or fpath == "?":
        return fpath
    cwd_abs = str(Path(cwd).resolve())
    fpath_abs = fpath
    try:
        fpath_abs = str(Path(fpath).resolve()) if fpath.startswith("/") else fpath
    except Exception:
        pass
    # 1. 绝对路径恰好在 cwd 下：相对化
    if fpath_abs.startswith(cwd_abs + "/"):
        return fpath_abs[len(cwd_abs) + 1:]
    if fpath_abs == cwd_abs:
        return "<game>"
    # 2. 已经是相对路径：原样
    if not fpath.startswith("/"):
        return fpath
    # 3. AI hallucinate 的绝对路径（如 /root/...）：取末尾的 game-style 路径
    parts = fpath.strip("/").split("/")
    # 尝试找 scenes / scripts / assets / audio / fonts 这种已知顶层目录
    for marker in ("scenes", "scripts", "assets", "audio", "fonts"):
        if marker in parts:
            idx = parts.index(marker)
            return "/".join(parts[idx:])
    # 最后兜底：basename
    return parts[-1] if parts else fpath


async def _send_game_progress(message: str) -> None:
    """Push game creation/modification progress to iOS via WebSocket."""
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    if ctx._ws_manager:
        logger.info("[GameTool] progress → %s (owner=%s user=%s)",
                    message, owner_id, user_id)
        try:
            await ctx._ws_manager.send_to_user(owner_id, user_id, {
                "type": "robot_command",
                "data": {"action": "game_progress", "message": message},
            })
        except Exception as e:
            logger.error("[GameTool] progress send failed: %s", e)
    else:
        logger.warning(
            "[GameTool] progress skipped (no ws_manager): %s", message)


async def _send_game_progress_key(key: str, **kwargs: object) -> None:
    await _send_game_progress(_tr(key, **kwargs))


async def _send_game_modification_failed(game_id: str, reason: str) -> None:
    """通知 iOS：modify_game 失败 — 客户端解锁 isSending / aiGameCreating，
    避免 GameCreator UI 卡死。

    Why: widget 路径有 `widget_creation_failed` 三点闭环，create 路径有
    `game_creation_failed`，唯独 modify 之前**完全缺这条**，于是 timeout /
    no-diffs / 路径校验拒绝 / 内部异常都会让 iOS UI 永久 stuck（三方 review
    共识 P0）。
    How: 与 create_game 失败通知一致的 robot_command + action 协议，新增
    action="game_modification_failed"；reason 是机器可读字串（invalid_game_id /
    game_not_found / not_owner / no_changes / internal_error），iOS 拿到事件
    立即翻 `aiGameCreating=false`。
    """
    if not ctx._ws_manager:
        return
    try:
        owner_id = ctx._current_owner_id.get()
        user_id = ctx._current_user_id.get()
        await ctx._ws_manager.send_to_user(
            owner_id,
            user_id,
            {
                "type": "robot_command",
                "data": {
                    "action": "game_modification_failed",
                    "game_id": game_id,
                    "reason": reason,
                },
            },
        )
        logger.info("[GameTool] ✗ game_modification_failed sent | game_id=%s reason=%s", game_id, reason)
    except Exception as e:
        logger.warning("[GameTool] failed to notify iOS of game_modification_failed: %s", e)


async def _run_claude_agent(cwd: str, prompt: str, input_mode: str = "touch", max_turns: int = 20, use_mcp: bool = False, session_id: str = "", model: str = "") -> tuple[str, str, int]:
    """Run Claude Code SDK to generate/modify game code.

    ``model``: 显式指定 OpenRouter 模型 id；为空时退回 settings.game_code_model
    （create_game 默认走这条）。modify_game 一般传 settings.game_modify_model
    （haiku 4.5 比 sonnet 4.6 便宜 3-5× 且足够应付 EDIT 模式的小改）。

    Returns (result_text, session_id) tuple.
    """
    import os
    from robot.config import settings

    # Set Anthropic env vars for Claude Code SDK / Claude CLI
    # 三种 token 模式（auth_token 字段按前缀自动分发到正确 env var）：
    #   - PAYG API key:      ANTHROPIC_API_KEY=sk-ant-...        + base_url 空
    #   - 订阅 OAuth:        CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-* + base_url 空
    #   - 第三方 router:     ANTHROPIC_AUTH_TOKEN=sk-or-/etc.    + base_url=代理
    #
    # 关键：`sk-ant-oat01-*` OAuth token 不能放进 ANTHROPIC_AUTH_TOKEN —— Claude
    # CLI 把后者当成 bearer 发给 Anthropic Messages API（401 "OAuth authentication
    # is currently not supported"）。OAuth 必须用 CLAUDE_CODE_OAUTH_TOKEN，
    # CLI 走订阅 OAuth 协议。两个 env var 分别对应不同认证路径。
    auth_token = (settings.anthropic_auth_token or "").strip()
    is_oauth = auth_token.startswith("sk-ant-oat01-")
    if auth_token and is_oauth:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = auth_token
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ["ANTHROPIC_API_KEY"] = ""
    elif auth_token:
        # 第三方 router bearer
        os.environ["ANTHROPIC_AUTH_TOKEN"] = auth_token
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ["ANTHROPIC_API_KEY"] = ""
    elif settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    # 显式 unset 而非 set-only：避免父 shell 残留 export 干扰。
    # 比如开发期 `export ANTHROPIC_BASE_URL=https://openrouter.ai/api` 后切到
    # 订阅 OAuth 模式时，仅改 .env 不会清父进程 env，SDK 还会路由到 OpenRouter
    # 把 oat01 token 错发给第三方。`pop` + 默认值确保 always 覆盖。
    if settings.anthropic_base_url:
        os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    else:
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    logger.info(
        "[CodeAgent] env: mode=%s token=%s API_KEY=%s BASE_URL=%s",
        "oauth" if is_oauth else ("router" if auth_token else ("payg" if settings.anthropic_api_key else "none")),
        (auth_token[:16] + "...") if auth_token else "(empty)",
        (settings.anthropic_api_key[:12] +
         "...") if settings.anthropic_api_key else "(empty)",
        settings.anthropic_base_url or "(empty)",
    )

    try:
        from claude_code_sdk import (
            query, ClaudeCodeOptions,
            AssistantMessage, UserMessage, SystemMessage, ResultMessage,
            TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )
    except ImportError:
        logger.error("[CodeAgent] claude-code-sdk not installed")
        return _tr("CLAUDE_SDK_NOT_INSTALLED"), ""

    # Build system prompt with actual device resolution
    device_model = ctx._current_device_model.get("")
    screen_resolution = "1194x834"  # default
    if device_model:
        # device_model format: "iPhone 1194x834" or "iPad 1024x768"
        parts = device_model.split()
        for part in parts:
            if "x" in part:
                try:
                    w, h = part.split("x")
                    int(w)
                    int(h)
                    screen_resolution = part
                except ValueError:
                    pass
        logger.info("[CodeAgent] device_model=%s screen=%s",
                    device_model, screen_resolution)

    system_prompt = _build_skeleton_system_prompt(
        input_mode=input_mode, screen_resolution=screen_resolution)

    # CLI 默认行为：从 cwd 向上递归到 $HOME 加载所有 CLAUDE.md，注入
    # messages[0] 的 <system-reminder>。实测加载 4 份共 ~7300 tokens：
    #   ~/.claude/CLAUDE.md             (oh-my-claudecode framework, 866 tok) ❌
    #   robot/CLAUDE.md                 (大杂烩 5573 tok) ❌
    #   motem-backend-game-gen-optimize/CLAUDE.md (100 tok) ❌
    #   games/ai_xxx/CLAUDE.md          (游戏专属规则 777 tok) ✅ 唯一相关
    # 用 --bare 关掉 auto-discovery（同时关 hooks/LSP/plugin sync/auto-memory
    # 等无关功能；auth 仍走 ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY 环境变量），
    # 然后把 per-game CLAUDE.md 手动 append 到 system_prompt，保持游戏专属
    # 规则可见。预计省 ~6500 tokens / round-trip。
    game_claude_md = Path(cwd) / "CLAUDE.md"
    if game_claude_md.is_file():
        try:
            md_text = game_claude_md.read_text(encoding="utf-8").strip()
            if md_text:
                system_prompt = (
                    system_prompt.rstrip()
                    + "\n\n## Game-specific rules (from game's CLAUDE.md)\n\n"
                    + md_text
                )
        except OSError as e:
            logger.warning("[CodeAgent] failed to read per-game CLAUDE.md: %s", e)

    # Build godot-mcp server config (only for create, not modify)
    mcp_servers: dict = {}
    mcp_tool_names: list[str] = []
    if use_mcp:
        godot_mcp_env: dict[str, str] = {}
        if settings.godot_path:
            godot_mcp_env["GODOT_PATH"] = settings.godot_path
        mcp_servers["godot"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["@coding-solo/godot-mcp"],
            **({"env": godot_mcp_env} if godot_mcp_env else {}),
        }
        mcp_tool_names = ["mcp__godot__create_scene",
                          "mcp__godot__add_node", "mcp__godot__get_project_info"]

    # Per-game session storage: tell the CLI to put projects/<slug>/<uuid>.jsonl
    # under <game_dir>/.claude/ instead of the user-global ~/.claude/. Makes
    # remix-as-clone (shutil.copytree) carry sessions along automatically and
    # delete-game cleanup self-contained. See robot/games/_session_paths.py.
    from robot.games._session_paths import (
        per_game_config_dir,
        per_game_session_jsonl,
        global_session_jsonl,
        compact_session,
        flush_active_to_archive,
        count_prompt_boundaries,
    )
    per_game_dir = per_game_config_dir(cwd)

    # 模型选择：caller 显式传 → 用 caller 的；否则默认 game_code_model（create 路径）
    effective_model = model or settings.game_code_model or None

    def _make_options(resume_session: str) -> ClaudeCodeOptions:
        # CLI flag --tools 限制**注入给 model 的 schema 集合**（区别于
        # --allowedTools 只管 permission）。不传时 CLI 默认把 29 个内置工具
        # （Bash / TodoWrite / Agent / Cron / Worktree / Team / WebFetch ...）
        # 全部 schema 发给 model，每次 round-trip 多花 ~17K tokens 给我们
        # 永远不用的工具。我们只用 Write/Edit/Read/Glob/Grep 5 个。
        # 实测后 tools schema 总量 92K → ~12K bytes，省 80%。
        tools_to_send = ",".join(["Write", "Edit", "Read", "Glob", "Grep"] + mcp_tool_names)

        # --bare：关闭 CLAUDE.md auto-discovery + hooks/LSP/plugin sync/auto-memory
        # /attribution/keychain reads 等运行时杂项。我们通过 system_prompt 显式
        # 提供所需上下文（_GODOT_SYSTEM_PROMPT_BASE + 游戏目录 CLAUDE.md），
        # 不依赖 CLI 自动行为。auth：使用 ANTHROPIC_AUTH_TOKEN（OpenRouter 路径）
        # 或 ANTHROPIC_API_KEY，OAuth 路径在 --bare 下不可用，但我们没用 OAuth。
        extra_args: dict = {
            "tools": tools_to_send,
            "bare": None,
            # sonnet 4.6 默认 effort=high，extended thinking 单轮可 dump 9K+
            # chars 思考块（~2.3K output tokens × $15/M ≈ $0.035/次），
            # 同时影响 turn 1 耗时（实测 211s）。降到 medium 减半 thinking
            # 输出，质量轻损。haiku 4.5（modify 路径）不受 effort 影响 ——
            # haiku 没 extended thinking 这个 API feature。
            "effort": "medium",
        }

        # 一次性诊断开关：env ROBOT_CODE_AGENT_DEBUG_DUMP_DIR=/path 时 ——
        # 把 CLI --debug api 输出写到该目录。诊断完手动 unset 避免日志膨胀。
        dump_dir = _os.environ.get("ROBOT_CODE_AGENT_DEBUG_DUMP_DIR", "").strip()
        if dump_dir:
            dump_path = Path(dump_dir).expanduser()
            dump_path.mkdir(parents=True, exist_ok=True)
            log_file = dump_path / f"claude_debug_{int(time.time())}.log"
            extra_args["debug"] = "api"
            extra_args["debug-file"] = str(log_file)
            logger.info("[CodeAgent] DEBUG DUMP enabled → %s", log_file)

        opts = ClaudeCodeOptions(
            system_prompt=system_prompt,
            cwd=cwd,
            model=effective_model,
            allowed_tools=["Write", "Edit", "Read",
                           "Glob", "Grep"] + mcp_tool_names,
            mcp_servers=mcp_servers if use_mcp else None,
            max_turns=max_turns,
            permission_mode="acceptEdits",
            env={"CLAUDE_CONFIG_DIR": str(per_game_dir)},
            extra_args=extra_args,
        )
        if resume_session:
            opts.resume = resume_session
        return opts

    # Legacy migration: pre per-game CLAUDE_CONFIG_DIR sessions live in
    # ~/.claude/projects/<global-slug>/. If the per-game path is empty but
    # the global one has the jsonl, copy it once. Idempotent: subsequent
    # runs see the per-game file and skip. If migration fails, the SDK will
    # naturally fall through to the retry-without-resume branches below.
    if session_id:
        pg_jsonl = per_game_session_jsonl(cwd, session_id)
        gl_jsonl = global_session_jsonl(cwd, session_id)
        if not pg_jsonl.is_file() and gl_jsonl.is_file():
            try:
                pg_jsonl.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gl_jsonl, pg_jsonl)
                logger.info(
                    "[CodeAgent] migrated legacy session %s… into per-game store",
                    session_id[:12],
                )
            except OSError as e:
                logger.warning(
                    "[CodeAgent] legacy session migration failed (%s); "
                    "will fall back to retry-without-resume", e,
                )

    # Sliding-window compaction: jsonl 太大就把最旧 prompt 的 turns 切到
    # archive，active 留尾部 N 个 prompt。CLI resume 看裁剪后的 active，模型
    # 仍有最近几轮上下文；溢出部分进入 sibling/archive/<sid>.archive.jsonl 永
    # 久保留。compact 失败则 flush active 到 archive + 删旧 active jsonl 后
    # 走 cliff fallback（清空 session_id → SDK 起 fresh sid），保证不丢历史
    # 也不留磁盘垃圾。
    #
    # Threshold 选择（中度收紧，针对 Claude SDK jsonl 实测 ~0.6 byte/token 校准）：
    #   trigger 50KB ≈ ~80K tokens（OpenRouter 反推，含 system + tools + active）
    #   target  25KB ≈ ~40K tokens 裁剪后 input 起步
    # 给 haiku 4.5 (200K context) 留 80% 余量，给 Sonnet 4.6 同样。byte/token
    # 比早先按"中文 3 byte/token"估的低 5×，因为 jsonl 是 JSON 结构（每个
    # `{`、键名、parentUuid hex 串都消耗独立 token）。
    SESSION_TRIM_TRIGGER_BYTES = 50_000   # ~80K tokens input ceiling
    SESSION_TRIM_TARGET_BYTES = 25_000    # ~40K tokens input floor
    # min=1：当 target 不达时（最后一个 prompt 自身就大于 target），保留**最近
    # 1 个完整 prompt** 而不是 2 个。这样能把"上一次 modify 累积的巨型对话
    # （tool_use / tool_result 多轮 100KB+）"切到 archive，model 仍有当前 prompt
    # 续接 + 当前代码现状作锚点。min=2 会卡在 100KB+ 不动（实测 modify 7 只切
    # 了 5KB → 仍 120K tokens 输入 → $0.43 / 123s）。
    SESSION_MIN_PROMPTS_KEPT = 1

    # 注：先前在这里调过 truncate_stale_tool_results 替换老 prompt 里的 Read
    # tool_result.content。实测在当前 compact + min=1 配置下 active 稳定 1
    # prompt，truncate 永远没工作目标（keep_recent_prompts=3 把仅有的 1 个
    # prompt 完全归入保留区）。撤回 wiring；helper 函数本身保留在
    # _session_paths.py 备用 —— 未来若提高 compact target 让 active 容纳
    # 更多 prompt 时可重新启用。

    if session_id:
        pg_jsonl = per_game_session_jsonl(cwd, session_id)
        if pg_jsonl.is_file() and pg_jsonl.stat().st_size > SESSION_TRIM_TRIGGER_BYTES:
            old_size_kb = pg_jsonl.stat().st_size // 1024
            # 边界探测：compact_session 需要 ≥ MIN+1 个 promptId 才能切。
            # 历史 bug：第一次 modify 时 active 只有 create 的 1 个 promptId，
            # compact 必返 False → cliff fallback → session_id="" → fresh sid
            # → 新 jsonl 又只有 1 promptId → 每次 modify 都 cliff，**多次
            # modify 永远不会累积多 prompt**。修法：边界不够时**跳过 compact
            # 但保留 session_id**，让本轮 SDK 把新 prompt 追加进同一份 active
            # jsonl，下一轮 modify 看到 2 个 promptId 时 compact 真的能切。
            # 边界够但 compact_session 仍 False 才走真 cliff fallback。
            n_prompts = count_prompt_boundaries(cwd, session_id)
            if n_prompts <= SESSION_MIN_PROMPTS_KEPT:
                logger.info(
                    "[CodeAgent] session %s… size %dKB but only %d promptId(s) "
                    "(need >%d); skipping compact, letting jsonl accumulate "
                    "boundaries (resume continues)",
                    session_id[:12], old_size_kb, n_prompts,
                    SESSION_MIN_PROMPTS_KEPT,
                )
            else:
                ok = compact_session(
                    cwd, session_id,
                    target_bytes=SESSION_TRIM_TARGET_BYTES,
                    min_prompts_kept=SESSION_MIN_PROMPTS_KEPT,
                )
                if ok:
                    new_size_kb = pg_jsonl.stat().st_size // 1024
                    logger.info(
                        "[CodeAgent] compacted session %s…  %dKB → %dKB (head archived)",
                        session_id[:12], old_size_kb, new_size_kb,
                    )
                else:
                    # 边界够但 compact_session 仍 False —— IO 错误 / malformed
                    # jsonl / 找不到满足 target 的切点。这才是真失败，cliff
                    # fallback：flush active 全文到 archive，删除孤儿 active
                    # jsonl，让 SDK 起 fresh sid。
                    logger.warning(
                        "[CodeAgent] session %s… size %dKB with %d promptIds "
                        "but compact failed (likely IO / malformed jsonl); "
                        "flushing to archive + cliff-fallback to fresh",
                        session_id[:12], old_size_kb, n_prompts,
                    )
                    flush_active_to_archive(cwd, session_id)
                    try:
                        pg_jsonl.unlink()
                    except OSError as e:
                        logger.warning(
                            "[CodeAgent] could not unlink orphan active %s (%s)",
                            pg_jsonl, e,
                        )
                    session_id = ""

    options = _make_options(session_id)
    if session_id:
        logger.info("[CodeAgent] resuming session %s", session_id)

    result_text = ""
    out_session_id = ""
    turn = 0
    tool_calls_total = 0
    files_written: list[str] = []
    progress_sent_design = False  # Only send "designing" progress once
    t0 = time.time()
    t_last = t0

    logger.info("[CodeAgent] ═══ START | model=%s cwd=%s prompt_len=%d ═══",
                effective_model or "default", cwd, len(prompt))

    try:
        # 包一层 inactivity timeout：每次拉新消息最多等 _INACTIVITY_TIMEOUT_SECONDS。
        # 防 Sonnet extended thinking 死循环（实测有过 21 分钟单次 API call 烧 $1.32 + 0
        # 文件输出的灾难案例）。超时则提前 break 并由下方的 zero-output 分支处理。
        agen = query(prompt=prompt, options=options).__aiter__()
        while True:
            try:
                message = await asyncio.wait_for(
                    agen.__anext__(), timeout=_INACTIVITY_TIMEOUT_SECONDS,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                gap = time.time() - t_last
                logger.warning(
                    "[CodeAgent] ⚠ inactivity timeout at turn %d (no message for %.1fs); aborting",
                    turn, gap,
                )
                await _send_game_progress_key(
                    "GAME_PROGRESS_TIMEOUT", elapsed=time.time() - t0
                )
                break
            now = time.time()
            # Wall-clock budget check — 防 extended thinking 持续 emit token 绕过 inactivity
            if now - t0 > _WALL_CLOCK_BUDGET_SECONDS:
                logger.warning(
                    "[CodeAgent] ⚠ wall-clock budget %.0fs exceeded at turn %d; aborting",
                    _WALL_CLOCK_BUDGET_SECONDS, turn,
                )
                await _send_game_progress_key("GAME_PROGRESS_TIMEOUT", elapsed=now - t0)
                break
            elapsed = now - t0
            delta = now - t_last
            t_last = now

            if isinstance(message, SystemMessage):
                if message.subtype == "api_retry":
                    logger.warning("[CodeAgent] +%.1fs API_RETRY attempt=%s error=%s",
                                   elapsed, message.data.get("attempt"), str(message.data.get("error", ""))[:200])
                elif message.subtype not in ("hook_started", "hook_response"):
                    logger.info("[CodeAgent] +%.1fs SYSTEM: subtype=%s data=%s",
                                elapsed, message.subtype, str(message.data)[:120])

            elif isinstance(message, AssistantMessage):
                turn += 1
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        preview = (block.thinking or "")[
                            :200].replace("\n", " ")
                        logger.info("[CodeAgent] +%.1fs [turn %d] 💭 thinking (%d chars): %s",
                                    elapsed, turn, len(block.thinking or ""), preview)
                        if not progress_sent_design:
                            progress_sent_design = True
                            await _send_game_progress_key("GAME_PROGRESS_ANALYZING", elapsed=elapsed)

                    elif isinstance(block, TextBlock) and block.text.strip():
                        preview = block.text[:150].replace("\n", " ")
                        logger.info("[CodeAgent] +%.1fs [turn %d] 💬 %s",
                                    elapsed, turn, preview)
                        result_text += block.text

                    elif isinstance(block, ToolUseBlock):
                        tool_calls_total += 1
                        tool_input = block.input or {}
                        if block.name in ("Write", "Edit"):
                            fpath = tool_input.get(
                                "file_path", tool_input.get("path", "?"))
                            content_len = len(tool_input.get(
                                "content", tool_input.get("new_string", "")))
                            files_written.append(fpath)
                            logger.info("[CodeAgent] +%.1fs [turn %d] ✏️  %s → %s (%d chars)",
                                        elapsed, turn, block.name, fpath, content_len)
                            # Sanitize before sending to iOS — 移除 server 本地路径
                            # （/home/developer/games/ai_xxx/scenes/main.gd → scenes/main.gd），
                            # 防止用户隐私泄漏（owner 用户名 / 服务部署路径）。
                            await _send_game_progress_key("GAME_PROGRESS_WRITING",
                                path=_sanitize_path_for_user(fpath, cwd),
                                chars=content_len, turn=turn)
                        elif block.name == "Read":
                            fpath = tool_input.get("file_path", "?")
                            logger.info("[CodeAgent] +%.1fs [turn %d] 📖 Read → %s",
                                        elapsed, turn, fpath)
                            await _send_game_progress_key("GAME_PROGRESS_READING",
                                path=_sanitize_path_for_user(fpath, cwd))
                        elif block.name == "Glob":
                            logger.info("[CodeAgent] +%.1fs [turn %d] 🔍 Glob %s",
                                        elapsed, turn, tool_input.get("pattern", "?"))
                        elif block.name == "Grep":
                            logger.info("[CodeAgent] +%.1fs [turn %d] 🔎 Grep %s",
                                        elapsed, turn, tool_input.get("pattern", "?"))
                        else:
                            logger.info("[CodeAgent] +%.1fs [turn %d] 🔧 %s %s",
                                        elapsed, turn, block.name, str(tool_input)[:100])

            elif isinstance(message, UserMessage):
                # Tool results arrive as UserMessage(content=[ToolResultBlock, ...])
                blocks = message.content if isinstance(
                    message.content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        status = "❌ ERROR" if block.is_error else "✅"
                        preview = str(block.content)[:120].replace(
                            "\n", " ") if block.content else ""
                        logger.info("[CodeAgent] +%.1fs [turn %d] %s result (+%.2fs): %s",
                                    elapsed, turn, status, delta, preview)

            elif isinstance(message, ResultMessage):
                if message.session_id:
                    out_session_id = message.session_id
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "?"
                logger.info(
                    "[CodeAgent] ═══ DONE | turns=%d tool_calls=%d files=%d "
                    "elapsed=%.1fs api_ms=%s cost=%s ═══",
                    turn, tool_calls_total, len(set(files_written)),
                    elapsed, message.duration_api_ms, cost,
                )
                n_files = len(set(files_written))
                if n_files > 0:
                    await _send_game_progress_key("GAME_PROGRESS_CODE_DONE", count=n_files, elapsed=elapsed)
                else:
                    await _send_game_progress_key("GAME_PROGRESS_ANALYSIS_DONE", elapsed=elapsed)

    except Exception as e:
        # Stale session resume (Claude Code SDK "No conversation found ...") exits
        # code 1 before any turns run. Fall back to a fresh session once by
        # recursing with session_id="" — reuses all the handlers above and the
        # recursion terminates because the retry has no session_id.
        if session_id and turn == 0 and tool_calls_total == 0:
            logger.warning(
                "[CodeAgent] resume %s… failed (%s); retrying without resume",
                session_id[:12], str(e)[:120])
            return await _run_claude_agent(
                cwd, prompt,
                input_mode=input_mode,
                max_turns=max_turns,
                use_mcp=use_mcp,
                session_id="",
                model=model,
            )
        # Cosmetic exit-1 检测：Claude Code CLI 偶尔在 ResultMessage 之后还
        # 额外发一条 status=error 帧（最后阶段 Bedrock SSE 收尾不规范 / CLI
        # cleanup phase 出错），SDK 把它当 Exception 抛上来。如果工作已经实质
        # 完成（≥1 file written + 跑过 5s 真有工作产出），降级为 WARNING +
        # 返回 result_text 让上层 modify_game 走 diffs 判定路径，不污染 result。
        elapsed = time.time() - t0
        n_files_done = len(set(files_written))
        is_cosmetic_cli_exit = (
            "exit code 1" in str(e)
            and n_files_done > 0
            and elapsed > 5.0
        )
        if is_cosmetic_cli_exit:
            logger.warning(
                "[CodeAgent] CLI cosmetic exit-1 after work done (elapsed=%.1fs "
                "files=%d turns=%d). Treating as success; result preserved.",
                elapsed, n_files_done, turn,
            )
            return result_text or "", out_session_id, n_files_done
        logger.error("[CodeAgent] ✗ error after +%.1fs: %s",
                     elapsed, e, exc_info=True)
        return _tr("GAME_CODE_GENERATION_ERROR", e=e), out_session_id, n_files_done

    # If resume returned zero work (session expired silently without raising),
    # retry once with a fresh session so the user's request still gets executed.
    if session_id and turn == 0 and tool_calls_total == 0:
        logger.warning(
            "[CodeAgent] resume %s… produced no output; retrying without resume",
            session_id[:12])
        return await _run_claude_agent(
            cwd, prompt,
            input_mode=input_mode,
            max_turns=max_turns,
            use_mcp=use_mcp,
            session_id="",
            model=model,
        )

    if files_written:
        logger.info("[CodeAgent] files written: %s",
                    ", ".join(dict.fromkeys(files_written)))
    return result_text, out_session_id, len(set(files_written))


def _run_gdparse(game_dir: Path) -> list[str]:
    """Run gdparse on all .gd files, return list of errors."""
    import subprocess
    errors = []
    for gd_file in sorted(game_dir.rglob("*.gd")):
        try:
            result = subprocess.run(
                ["gdparse", str(gd_file)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                rel = gd_file.relative_to(game_dir)
                errors.append(f"{rel}: {result.stderr.strip()}")
        except FileNotFoundError:
            logger.info(
                "[GameTool] gdparse not installed, skipping syntax check")
            return []  # gdtoolkit not installed, skip gracefully
        except subprocess.TimeoutExpired:
            pass
    return errors


async def _validate_and_fix(game_dir: Path, game_id: str, max_rounds: int = 2, check_only: bool = False) -> list[str]:
    """Run gdtoolkit static check + (optional) Claude auto-fix. Returns fix log.

    ``check_only``: 仅运行 gdparse 校验，不调 SDK 修复。create 流程下游还有一道
    godot --check-only + haiku fix 兜底（更准），跑两套 fix 会重复烧 token。
    所以 create 用 check_only=True 把这里降级成纯报告。modify 路径下游没有
    Loop B，仍然需要这里完成 fix（默认 check_only=False）。
    """
    fix_log = []
    for round_num in range(max_rounds):
        errors = _run_gdparse(game_dir)
        if not errors:
            if round_num > 0:
                logger.info(
                    "[GameTool] validation passed after %d fix round(s)", round_num)
            else:
                logger.info("[GameTool] validation passed, no syntax errors")
            break

        fix_log.append(f"Round {round_num + 1}: {len(errors)} error(s)")
        logger.warning(
            "[GameTool] gdparse found %d error(s): %s", len(errors), errors)

        if check_only:
            # 只报告，不修：错误交给下游 godot --check-only + haiku fix loop
            # 统一处理，避免 gdparse + godot 两套 fix 重复烧 token。
            logger.info(
                "[GameTool] _validate_and_fix check_only=True — leaving %d error(s) "
                "for downstream godot --check-only fix loop", len(errors))
            return fix_log

        await _send_game_progress_key("GAME_PROGRESS_FIXING_SYNTAX", count=len(errors))

        error_text = "\n".join(errors)
        fix_prompt = t("en", "GAME_FIX_PROMPT", errors=error_text)
        # Syntax fix is a 1-2 Edit task — use the cheap modify model (haiku)
        # and cap turns. Default was sonnet + unlimited turns, ~5× more
        # expensive without quality gain.
        await _run_claude_agent(
            str(game_dir), fix_prompt,
            use_mcp=False,
            model=settings.game_modify_model,
            max_turns=4,
        )  # ignore session_id
    else:
        # Exhausted max_rounds without passing
        remaining = _run_gdparse(game_dir)
        if remaining:
            logger.warning("[GameTool] validation still has %d error(s) after %d rounds", len(
                remaining), max_rounds)

    return fix_log


def _ensure_canonical_assets(game_dir: Path) -> None:
    """Pre-create the **only** canonical asset placeholder: `assets/background.png`.

    所有 AI 游戏都必须有背景，所以这个是基线。其他游戏内元素（玩家、敌人、
    子弹、blocks、tiles、notes、cards 等等）由 AI 自由用 Godot 内置图形 API
    （ColorRect / Polygon2D / draw_circle / draw_rect ...）绘制，**不创建 PNG**
    文件 —— 这样：
      (a) AI 不需要 Bash（沙箱拒绝 Bash 会导致 AI 死循环 thinking 数十分钟）
      (b) 游戏类型保持通用 —— 解谜 / 跑酷 / 节奏 / 拼图都不需要硬塞 player/
          enemy 等射击游戏专属概念
      (c) 后续要支持其他元素图片替换时，再单独扩展
    """
    assets_dir = game_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / "background.png"
    if target.exists():
        return  # 不覆盖（用户已 upload 的真实图保留）
    try:
        from PIL import Image as _PILImage
        # 1280x720 深蓝（typical landscape game 的 base）
        img = _PILImage.new("RGBA", (1280, 720), (16, 24, 48, 255))
        img.save(target, "PNG")
    except Exception as e:
        logger.warning("[GameTool] failed to create background.png: %s", e)


def _harvest_audio(game_dir: Path) -> dict:
    """Scan the generated game for AudioManager.play / play_bgm references and
    copy each referenced sound from the master library into the game's audio/
    directory.

    Also ensures the AudioManager autoload + scripts/audio_manager.gd exist
    when the game references audio (covers the modify_game path on older
    games that pre-date the audio system).

    Returns a small summary dict for logging:
        {"requested": [..], "copied": [..], "missing": [..]}
    """
    import re
    global _AUDIO_REF_RE, _BGM_TRACK_RE
    if _AUDIO_REF_RE is None:
        # _audio_play("id") / _audio_play_bgm("id") (the helper indirection
        # added to the skeleton so iOS embedded Godot can resolve audio at
        # runtime instead of at parse time) AND the legacy direct
        # AudioManager.play("id") / .play_bgm("id") form, both with double
        # or single quotes.
        _AUDIO_REF_RE = re.compile(
            r"(?:AudioManager\s*\.\s*(?:play|play_bgm)|_audio_play(?:_bgm)?)"
            r"\s*\(\s*[\"']([a-zA-Z0-9_]+)[\"']\s*[\),]"
        )
        # `bgm_track = "id"` —— 骨架 plumbing 用 `AudioManager.play_bgm(bgm_track)`
        # 把变量传给 play 函数，regex 看不到字面量。Strategy / AI 通常通过赋值
        # 这个 bound 变量来决定 BGM，所以单独识别这一模式。
        _BGM_TRACK_RE = re.compile(
            r"\bbgm_track\s*=\s*[\"']([a-zA-Z0-9_]+)[\"']"
        )

    manifest = _load_audio_manifest()
    by_id = {s["id"]: s for s in manifest.get("sounds", [])}

    # 1. collect references — skip lines that are GDScript comments (start
    # with `#` after optional whitespace) to avoid picking up doc-string /
    # skeleton-template examples like `# Use AudioManager.play("name") for SFX`.
    # 不再用 `re.MULTILINE` 一把扫整个文件，逐行过滤更准确（多行字符串里的
    # `play("x")` 暂忽略不计 — 实际生产代码里不会用 multi-line string 嵌
    # GDScript call）。
    requested: set[str] = set()
    for path in list(game_dir.rglob("*.gd")) + list(game_dir.rglob("*.tscn")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in _AUDIO_REF_RE.finditer(line):
                requested.add(m.group(1))
            for m in _BGM_TRACK_RE.finditer(line):
                requested.add(m.group(1))

    if not requested:
        return {"requested": [], "copied": [], "missing": []}

    # 2. copy referenced files into game's audio/ dir
    copied: list[str] = []
    missing: list[str] = []
    target_dir = game_dir / "audio"
    target_dir.mkdir(exist_ok=True)
    for sid in sorted(requested):
        entry = by_id.get(sid)
        if entry is None:
            missing.append(sid)
            logger.warning("[GameTool] audio: AI referenced unknown id %r", sid)
            continue
        src = AUDIO_LIBRARY_DIR / "library" / entry["file"]
        if not src.exists():
            missing.append(sid)
            logger.warning("[GameTool] audio: library file missing for %s (%s)",
                           sid, src)
            continue
        # Game references res://audio/<id>.ogg (or .wav). Strip the category
        # prefix from manifest's `file` field so the on-disk name matches.
        suffix = Path(entry["file"]).suffix or ".ogg"
        dst = target_dir / f"{sid}{suffix}"
        try:
            shutil.copy2(src, dst)
            copied.append(sid)
        except Exception as e:
            missing.append(sid)
            logger.warning("[GameTool] audio: copy failed for %s: %s", sid, e)

    # 3. backstop: ensure audio_manager.gd + autoload entry exist for old games
    am_dst = game_dir / "scripts" / "audio_manager.gd"
    if not am_dst.exists():
        am_src = TEMPLATE_DIR / "scripts" / "audio_manager.gd"
        if am_src.exists():
            shutil.copy2(am_src, am_dst)
            logger.info("[GameTool] audio: injected audio_manager.gd into %s",
                        game_dir.name)

    proj_file = game_dir / "project.godot"
    if proj_file.exists():
        proj_text = proj_file.read_text(encoding="utf-8")
        if "AudioManager=" not in proj_text:
            # Insert / append into [autoload] section
            if "[autoload]" in proj_text:
                proj_text = proj_text.replace(
                    "[autoload]",
                    '[autoload]\n\nAudioManager="*res://scripts/audio_manager.gd"',
                    1,
                )
            else:
                proj_text += '\n\n[autoload]\n\nAudioManager="*res://scripts/audio_manager.gd"\n'
            proj_file.write_text(proj_text, encoding="utf-8")
            logger.info("[GameTool] audio: registered AudioManager autoload in %s",
                        game_dir.name)

    summary = {
        "requested": sorted(requested),
        "copied": sorted(copied),
        "missing": sorted(missing),
    }
    logger.info("[GameTool] audio harvest: %s", summary)
    return summary


def _import_assets(game_dir: Path) -> bool:
    """跑 `godot --headless --import .` 生成 PNG/OGG 的 `.import` 元数据 +
    `.godot/imported/<hash>.ctex` 已编译纹理 / `.oggvorbisstr` 流式音频。

    根因：iOS SwiftGodotKit mobile build 加载 `res://assets/x.png` 时**必须**
    走 Godot resource pipeline，需要旁边的 `.import` + `.godot/imported/` 配套。
    平时是 Godot editor 自动生成；后端 headless 流程没 editor，所以模板游戏
    从无 `.import`，iOS 上 `load()` 返回 null → `.get_size()` 触发 segfault
    （2026-05-10 故障复盘）。

    成本：
      - 首次 import 3-5s（小游戏 ~9 资源）
      - 增量 import < 1s（Godot 用 .md5 跳过未变文件）
      - zip 体积 +50-150KB（主要是 oggvorbisstr 转码后的音频，PNG ctex 通常
        只是 indirection 引用 raw png，几百字节）

    幂等：godot --import 自带增量，重跑只处理变更资源。

    失败处理：godot 不可用 / 超时 / 非零 退出 → 仅 warn 不中断；生成出的游戏
    可能在 iOS 上加载 PNG 失败，但 GDScript 仍能跑（model 应当写 null check）。
    """
    import subprocess
    import shutil as _shutil
    godot_bin = settings.godot_path or _shutil.which("godot")
    if not godot_bin:
        logger.warning("[GameTool] _import_assets skipped: godot binary not found")
        return False
    t0 = time.time()
    try:
        result = subprocess.run(
            [godot_bin, "--headless", "--import", "."],
            cwd=str(game_dir),
            timeout=120,
            capture_output=True,
            text=True,
        )
        elapsed = time.time() - t0
        if result.returncode == 0:
            logger.info(
                "[GameTool] _import_assets ✓ %.1fs (cwd=%s)",
                elapsed, game_dir.name,
            )
            return True
        logger.warning(
            "[GameTool] _import_assets ✗ rc=%d elapsed=%.1fs stderr=%r",
            result.returncode, elapsed, (result.stderr or "")[:500],
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("[GameTool] _import_assets timed out (120s)")
        return False
    except Exception as e:
        logger.error("[GameTool] _import_assets failed: %s", e)
        return False


def _validate_gdscript(game_dir: Path) -> tuple[bool, str]:
    """跑 godot --headless --check-only 检查 main.gd 是否能 parse。

    返回 (ok, error_text)。error_text 仅在 ok=False 时有内容。AI 经常踩两个坑：
    - duplicate `func name` 想 override（GDScript 不允许）
    - `var x := game.X` 类型推导失败（Variant 推不出具体类型）

    用 godot 的真实 parser 检测，比正则准。失败的 error_text 直接给 fix-loop
    用来 prompt AI 修复。
    """
    import subprocess
    import shutil as _shutil
    godot_bin = settings.godot_path or _shutil.which("godot")
    if not godot_bin:
        logger.warning("[GameTool] _validate_gdscript: godot not found, skip")
        return (True, "")
    try:
        result = subprocess.run(
            [godot_bin, "--headless", "--check-only", "--script", "scenes/main.gd"],
            cwd=str(game_dir),
            timeout=30,
            capture_output=True,
            text=True,
        )
        out = (result.stdout or "") + (result.stderr or "")
        # 关键错误 pattern（忽略 res:// 资源 not found 这类非 GDScript 语法错误）
        errors = []
        for line in out.splitlines():
            if "SCRIPT ERROR" in line and "Parse Error" in line:
                errors.append(line.strip())
            elif "Parse Error" in line and "main.gd:" in line:
                errors.append(line.strip())
        if errors:
            err_text = "\n".join(errors[:5])
            logger.warning("[GameTool] _validate_gdscript ✗ %s", err_text)
            return (False, err_text)
        return (True, "")
    except subprocess.TimeoutExpired:
        logger.warning("[GameTool] _validate_gdscript timed out, assuming OK")
        return (True, "")
    except Exception as e:
        logger.warning("[GameTool] _validate_gdscript failed (%s), assuming OK", e)
        return (True, "")


def _generate_script_cache(game_dir: Path) -> None:
    """Generate .godot/global_script_cache.cfg so Godot doesn't warn on iOS startup.

    Godot normally generates this file when the editor imports the project.
    Since we skip the editor, we generate it manually from all .gd files.
    """
    godot_dir = game_dir / ".godot"
    godot_dir.mkdir(exist_ok=True)

    # Collect all .gd files relative to game_dir as res:// paths
    scripts = []
    for gd_file in sorted(game_dir.rglob("*.gd")):
        rel = gd_file.relative_to(game_dir)
        # Skip input_bridge and game_manager (they are autoloaded, not global classes)
        scripts.append(f"res://{rel.as_posix()}")

    # Format: simple cfg with one entry per script
    lines = ['[gd_resource type="ScriptCache" format=3]\n']
    lines.append("[resource]\n")
    if scripts:
        scripts_str = ", ".join(f'"{s}"' for s in scripts)
        lines.append(f"scripts = PackedStringArray({scripts_str})\n")
    else:
        lines.append("scripts = PackedStringArray()\n")

    cache_path = godot_dir / "global_script_class_cache.cfg"
    cache_path.write_text("".join(lines), encoding="utf-8")
    # Also write the alternate filename some Godot versions look for
    (godot_dir / "global_script_cache.cfg").write_text("".join(lines), encoding="utf-8")
    logger.info("[GameTool] generated script cache: %s (%d scripts)",
                cache_path, len(scripts))


_TEXT_EXTS = (".gd", ".tscn", ".godot", ".cfg", ".tres")
_BINARY_EXTS = (".ogg", ".wav", ".mp3", ".png", ".jpg", ".jpeg", ".webp",
                ".otf", ".ttf")


def _is_binary_ext(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _BINARY_EXTS


def _compute_file_manifest(game_dir: Path) -> dict[str, str]:
    """Return ``{relative_path: content}`` for all relevant files in the game.

    - 文本（``.gd / .tscn / .godot / .cfg / .tres``）：原 utf-8 内容
    - 二进制（``.ogg / .png / 字体`` 等）：base64 编码的 ASCII 字符串

    base64 编码同样可参与 dict equality 比较，保证 ``_compute_diffs`` 的
    add / modify / delete 判定对二进制文件继续工作。两类内容的真实区分由
    ``_compute_diffs`` 写入 ``encoding`` 字段标注，iOS 据此选择 utf-8 写文
    本 / base64 解码写二进制。
    """
    import base64
    manifest = {}
    for f in game_dir.rglob("*"):
        if not f.is_file():
            continue
        suf = f.suffix.lower()
        rel = str(f.relative_to(game_dir))
        try:
            if suf in _TEXT_EXTS:
                manifest[rel] = f.read_text(encoding="utf-8")
            elif suf in _BINARY_EXTS:
                manifest[rel] = base64.b64encode(f.read_bytes()).decode("ascii")
        except Exception:
            pass
    return manifest


def _compute_diffs(old_manifest: dict, new_manifest: dict) -> list[dict]:
    """Compute file-level diffs between two manifests.

    Each diff entry carries an ``encoding`` field:
      - ``"utf-8"`` (default) for text files — iOS writes ``content`` as UTF-8
      - ``"base64"`` for binary files (``.ogg / .png / 字体``) — iOS must
        ``Data(base64Encoded:)`` decode then write bytes

    Binary support fixes the long-standing limitation where audio/image
    changes produced by ``modify_game`` only landed on iOS after the user
    relaunched the game (which re-downloaded the full zip).
    """
    diffs = []
    all_files = set(old_manifest.keys()) | set(new_manifest.keys())
    for path in sorted(all_files):
        old_content = old_manifest.get(path)
        new_content = new_manifest.get(path)
        encoding = "base64" if _is_binary_ext(path) else "utf-8"
        if old_content is None and new_content is not None:
            diffs.append({"op": "add", "path": path,
                          "encoding": encoding, "content": new_content})
        elif old_content is not None and new_content is None:
            diffs.append({"op": "delete", "path": path})
        elif old_content != new_content:
            diffs.append({"op": "modify", "path": path,
                          "encoding": encoding, "content": new_content})
    return diffs


from contextvars import ContextVar

# When True, create_game skips its internal fast-path attempt. Set by the iOS
# [game_create] streaming route, which already tries fast path before calling
# create_game — avoids a redundant second router call on miss. Isolated per
# asyncio task (concurrency-safe).
_skip_fastpath: ContextVar[bool] = ContextVar("skip_fastpath", default=False)


async def _try_fastpath(instruction: str, input_mode: str) -> str | None:
    """Try Fast Path (archetype router → deterministic, instant) creation.

    Centralized here so every create path benefits: the iOS [game_create] route
    (via streaming), the main robot agent, and the bot all reach it through
    create_game. Returns the GAME_CREATE_DONE string on hit (after audio /
    import / zip / DB / game_ready / web-export-trigger via finalize), or None
    on any miss / failure. **Never raises** — callers fall through to slow path.
    """
    import uuid

    from robot.config import settings as _settings
    from robot.games.fastpath import RecipeCompiler
    from robot.games.fastpath.compiler import CompileError  # noqa: F401
    from robot.games.fastpath.customizer import Customizer
    from robot.games.fastpath.finalize import FinalizeError, finalize_fastpath_game
    from robot.games.fastpath.llm_client import make_openrouter_client
    from robot.games.fastpath.orchestrator import FastPathOrchestrator
    from robot.games.fastpath.router import Router
    from robot.i18n import t as _t

    try:
        model = _settings.game_modify_model
        orchestrator = FastPathOrchestrator(
            router=Router(make_openrouter_client(model=model)),
            customizer=Customizer(make_openrouter_client(model=model)),
            compiler=RecipeCompiler(
                GAMES_DIR / "_template",
                addon_dirs=(
                    GAMES_DIR / "_template_fastpath",
                    GAMES_DIR / "_template_slowpath",
                ),
            ),
            confidence_threshold=_settings.fastpath_confidence_threshold,
        )
    except Exception as e:
        logger.warning("[Fastpath] orchestrator setup failed: %s", e)
        return None

    game_id = _generate_game_id(instruction[:20] or "fastpath")
    if not _is_safe_ai_game_id(game_id):
        logger.warning("[Fastpath] generated unsafe game_id=%r, abort", game_id)
        return None
    target_dir = GAMES_DIR / game_id
    owner_id = ctx._current_owner_id.get("")
    user_id = ctx._current_user_id.get("")
    request_id = uuid.uuid4().hex[:12]

    try:
        await _send_game_progress_key("GAME_PROGRESS_PREPARING_TEMPLATE")
    except Exception:
        pass

    try:
        outcome = await orchestrator.try_create(
            user_text=instruction,
            input_mode=input_mode,
            target_dir=target_dir,
            request_id=request_id,
            user_id=user_id or None,
        )
    except Exception as e:
        logger.warning("[Fastpath] orchestrator.try_create exception: %s", e)
        return None

    if not outcome.hit or outcome.recipe is None or outcome.compile_result is None:
        return None  # miss / low confidence / compile error → slow path

    try:
        await _send_game_progress_key("GAME_PROGRESS_PACKING")
    except Exception:
        pass

    try:
        await finalize_fastpath_game(
            recipe=outcome.recipe,
            game_dir=outcome.compile_result.game_dir,
            owner_id=str(owner_id),
            user_id=str(user_id),
            input_mode=input_mode,
            description=instruction,
        )
    except FinalizeError as e:
        logger.warning("[Fastpath] finalize failed, cleanup + fallback: %s", e)
        try:
            from robot.games._session_paths import wipe_game_artifacts
            wipe_game_artifacts(target_dir)
        except Exception:
            pass
        return None

    title = outcome.recipe.meta.title_i18n.get("zh-Hans", outcome.recipe.meta.title_i18n["en"])
    return _t(ctx.get_user_lang(), "GAME_CREATE_DONE", title=title, game_id=game_id)


@tool
async def create_game(title: str, description: str, input_mode: str = "touch", icon: str = "🎮", template_game_id: str = "", title_i18n: dict | None = None, description_i18n: dict | None = None) -> str:
    """Create a brand-new game when the user asks to make or build a game.

    **Idempotent / always-call**: Every "do a game / 做个游戏" utterance is a
    fresh request for a NEW game. Even if the conversation history shows you
    already called `create_game` successfully earlier, you MUST call this tool
    again — DO NOT skip with text like "you already have a game / 已经创建了".
    Each successful call yields a different `game_id`.

    Args:
        title: Game name, for example "Space Shooter" or "Parkour Adventure".
        description: Detailed game description, including gameplay, controls, and level design.
        input_mode: Control mode: "touch" or "body_pose".
        icon: Emoji icon representing the game theme.
        template_game_id: Optional existing game ID to use as a template. Empty means use the default template.
        title_i18n: Optional bilingual title map with "en" and "zh-Hans".
        description_i18n: Optional bilingual description map with "en" and "zh-Hans".
    """
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    t0 = time.time()
    logger.info("[GameTool] ▶ create_game | title=%r owner=%s",
                title, owner_id)

    # 0. Fast Path (archetype router → deterministic, instant). One place,
    # every create path benefits (main agent, bot, and — on miss — the iOS
    # [game_create] route which sets _skip_fastpath to avoid a double attempt).
    # Skipped for template-based / mock creation. Never raises.
    if (settings.fastpath_enabled and not template_game_id
            and not _is_mock_creation_mode() and not _skip_fastpath.get()):
        fast = await _try_fastpath(description, input_mode)
        if fast is not None:
            logger.info("[GameTool] ✓ fast path hit (in create_game) | %.1fs", time.time() - t0)
            return fast

    # 1. Create game directory from template
    game_id = _generate_game_id(title)
    game_dir = GAMES_DIR / game_id

    # 默认 template 是 skeleton（_template，自 Phase 5 起永远启用）。骨架内置
    # 全套外层管道，AI 只填 3 个 required hook + 可选 override 5 个 optional
    # hook。
    if not TEMPLATE_DIR.is_dir():
        logger.error("[GameTool] template dir missing: %s", TEMPLATE_DIR)
        return _tr("GAME_TEMPLATE_MISSING")

    # Determine template source: official curated template (in _templates/) or
    # the default blank _template scaffold.
    #
    # template_game_id 是 streaming.py 从 _templates/_registry.json 里挑出来的
    # registry id（例如 "shooter"）。我们只在 GAMES_DIR/_templates/<id>/ 下查找；
    # 不再 fallback 到任意用户游戏目录 —— 那条老路径属于隐私 / IP 风险源，
    # 已经从候选源（_load_official_templates）侧关掉。
    source_dir = TEMPLATE_DIR
    template_label = "_template"
    if template_game_id and not _re.match(r'^[a-zA-Z0-9_-]{1,64}$', template_game_id):
        logger.warning("[GameTool] template_game_id rejected (invalid format): %r", template_game_id)
        template_game_id = ""
    if template_game_id:
        candidate = GAMES_DIR / "_templates" / template_game_id
        if candidate.is_dir() and (candidate / "scenes" / "main.gd").exists():
            source_dir = candidate
            template_label = template_game_id
            logger.info("[GameTool] using official template: _templates/%s", template_game_id)
        else:
            logger.warning(
                "[GameTool] official template _templates/%s missing, "
                "falling back to blank _template", template_game_id,
            )

    await _send_game_progress_key("GAME_PROGRESS_PREPARING_TEMPLATE")
    shutil.copytree(source_dir, game_dir)

    # 叠加 slow path addon（CLAUDE.md 等）—— 只有走 default `_template/` 时叠加，
    # official curated _templates/<id>/ 和 clone-as-template 路径都自带各自的
    # 上下文，不再注入 slow path 通用提示。dirs_exist_ok=True 让 base 和 addon
    # 共存 / 覆盖一致 key。addon 缺失（测试 fixture 或 v2 还没填）就跳过。
    slowpath_addon_dir = TEMPLATE_DIR.parent / "_template_slowpath"
    if source_dir == TEMPLATE_DIR and slowpath_addon_dir.is_dir():
        shutil.copytree(slowpath_addon_dir, game_dir, dirs_exist_ok=True)
        # The on-disk template has `{audio_section}` as a literal placeholder
        # so a single file is the source of truth (see _render_slowpath_claude_md).
        # The per-game copy must have the catalogue baked in — modify-time the
        # agent reads it directly from cwd and doesn't run the renderer again.
        rendered = _render_slowpath_claude_md()
        if rendered:
            (game_dir / "CLAUDE.md").write_text(rendered, encoding="utf-8")

    # 模板带的 session 继承：official 模板和老路径里的「另一款现有游戏」做模板
    # 时，源若有 sibling .claude/，把会话搬到新 game_dir 的 sibling，让 SDK
    # resume 原作者 / 模板作者的设计对话。源没 sibling 就是 fresh 起步，
    # 行为与之前等同（只有 _template 默认模板会走这条 fresh 分支）。
    template_inherited_sid: str | None = None
    if template_label != "_template":
        from robot.games._session_paths import inherit_session_from
        template_inherited_sid = inherit_session_from(source_dir, game_dir)
        if template_inherited_sid:
            logger.info(
                "[GameTool] inherited bundled session %s… from template %s",
                template_inherited_sid[:12], template_label,
            )

        # Clean up source game artifacts when copying from an existing game
        for cleanup in [game_dir / "meta.json", game_dir / ".godot"]:
            if cleanup.exists():
                if cleanup.is_dir():
                    shutil.rmtree(cleanup, ignore_errors=True)
                else:
                    cleanup.unlink()

    # Pre-create canonical asset placeholders so iOS direct-replace UI always
    # has known filenames to target. AI 必须按 prompt 要求引用这些文件。
    _ensure_canonical_assets(game_dir)

    logger.info("[GameTool] [1/5] game dir created: %s (template=%s)", game_dir, template_label)
    # 注：以前在这里推过 `game_dir_ready` 让 iOS 提前上传背景图。但那条路径
    # 有两个问题：(a) 上传端点 owner-only DB 校验，但 DB 行此时还没写，
    # 导致 iOS 上传 404；(b) 早写 DB 又会在 AI 失败 / 用户取消 / app 被杀时
    # 留草稿行污染 Mine Tab。改成 iOS 等 game_ready 后再上传到 background.png
    # （那时 DB 行已存在），canonical placeholder + Sprite2D + 后期上传同样
    # 实现"零 AI 二轮"的目标。

    # Update project.godot with actual device resolution
    device_model = ctx._current_device_model.get("")
    if device_model:
        for part in device_model.split():
            if "x" in part:
                try:
                    w, h = part.split("x")
                    w, h = int(w), int(h)
                    proj_file = game_dir / "project.godot"
                    proj_text = proj_file.read_text(encoding="utf-8")
                    proj_text = proj_text.replace(
                        "viewport_width=1194", f"viewport_width={w}")
                    proj_text = proj_text.replace(
                        "viewport_height=834", f"viewport_height={h}")
                    proj_file.write_text(proj_text, encoding="utf-8")
                    logger.info(
                        "[GameTool] updated project.godot resolution to %dx%d", w, h)
                except ValueError:
                    pass

    # 2. Run Claude Code SDK to generate game code
    # Skeleton hook 模式：让 AI 自己 Read scenes/main.gd 了解骨架结构，不
    # 嵌入文件内容到 prompt（骨架 307 行 ~10K chars，嵌入会让 prompt 飙到
    # 15K+ chars 触发 sonnet 大段 thinking + cost 翻倍）。AI 完整流程：
    # Read 骨架 → Edit 3 个 required hook + 选 override 0-5 个 optional hook。
    #
    # template_label 现在永远是 "_template"（自 Phase 5 单一模板），不再分支。
    if template_label != "_template":
        # Reserved for future curated _templates/ (shooter/runner/...) override
        prompt = f"""# MODE: CREATE NEW GAME (skeleton hook mode, curated template)

Build a playable game starting from the curated template that's already in the project directory.

New game name: "{title}"

User's game description:
{description}

## Task
1. Read scenes/main.gd to understand the curated template's gameplay structure.
2. Modify it to match the user's description. Edit the AI ZONE hooks (and override optional hooks by Editing their default implementation in the plumbing section).
3. Update scripts/i18n_strings.gd (`title` key + game-specific strings; every key needs `en` and `zh-Hans`).

Important: all Read/Write/Edit paths must be relative (e.g. `scenes/main.gd`).
Do not modify project.godot / input_bridge.gd / i18n.gd / game_manager.gd / audio_manager.gd.

Final response: 1-2 sentence summary of what you built. No code output."""
    else:
        # Default (skeleton) — read template, fill the 3 required hooks, optionally
        # override 5 default hooks. Skeleton already enforces z_index / null check /
        # i18n routing / state machine — AI focuses on gameplay only.
        prompt = f"""# MODE: CREATE NEW GAME (skeleton hook mode)

The project already has `scenes/main.gd` which is a 307-line **skeleton** with
the full outer shell baked in (state machine / HUD / background / font / i18n /
body-pose helper / BGM auto-start). Your job: fill the gameplay-specific hooks.

New game name: "{title}"

User's game description:
{description}

## Workflow
1. Read `scenes/main.gd` to see the skeleton's hooks and helpers.
2. Edit the AI IMPLEMENTATION ZONE at the bottom — fill in:
   - `_init_game()`   — set up `game.*` state; optionally set `bgm_track`
   - `_update_gameplay(delta)` — per-frame logic (move / spawn / collide / score)
   - `_draw_gameplay()` — render game objects (skeleton handles bg + HUD)
3. To customize defaults (HUD layout / game-over condition / mid-game tap /
   start screen / game-over screen): **Edit the existing default `func` in the
   plumbing section** above the AI ZONE. Do NOT redeclare them in the AI ZONE
   (GDScript has no `override` keyword — duplicate `func name` = Parse Error).
4. Update `scripts/i18n_strings.gd` — overwrite `title` value, add any
   game-specific keys you use via `_tr(...)`. Every key needs `en` + `zh-Hans`.

## Pre-bound state & helpers (don't redeclare; see skeleton)
- `score / lives / anim_timer / game (Dictionary) / bgm_track / state`
- `screen_size / HUD_SAFE_LEFT/RIGHT/TOP / game_font / bridge / i18n`
- `_tr(key, params)` for all UI text
- `_safe_load_texture("res://...")` for extra textures
- `get_body_target(margin)` for body-pose / face → screen position

## Constraints
- Paths must be relative (`scenes/main.gd`, not absolute).
- Do NOT modify: project.godot / input_bridge.gd / i18n.gd / game_manager.gd / audio_manager.gd.
- `draw_colored_polygon` 2nd arg = single `Color` (not `PackedColorArray`).
- `:= game.X` or `:= dict["k"]` → type inference fail; use `: <Type> =` instead.
- No inner classes (use Dictionary).
- No Bash tool, no TodoWrite.

## Image assets
Only `assets/background.png` is pre-created (1280×720 dark navy placeholder, already loaded by the skeleton with `z_index = -100`). All game elements (player / enemies / projectiles / blocks / items / cards / notes / tiles ...) must be drawn with `draw_rect / draw_circle / draw_polygon / draw_colored_polygon / draw_line`. Do NOT create new PNG files.

## Audio (optional)
For BGM: set `bgm_track = "bgm_action"` (or any name from the audio catalog in
the system prompt) in `_init_game()` — the skeleton auto-plays it. For SFX:
call `AudioManager.play("hit")` / `play("score_up")` etc. at gameplay events.

Final response format: 1-2 sentence summary of what you built. Do not output code."""

    # Inject input mode hint
    if input_mode == "body_pose":
        prompt += "\n\nThis game uses body pose / visual control. Use InputBridge body_lean_x/y and face_position as the primary gameplay input. Do not use touch input for gameplay; touch is only for start/retry.\nControl style: use position control with lerp smoothing, following the _move_player example in the template. Do not use velocity control (position += speed * delta)."
    else:
        prompt += "\n\nThis game uses touch control. Use InputEventScreenTouch/InputEventScreenDrag for input. Do not use InputBridge."

    if _is_mock_creation_mode():
        # Mock mode — skip the Claude Code SDK; the template's scenes/main.gd
        # already renders a valid "Tap to Start" screen so the resulting zip
        # is loadable on iOS, just trivially playable.
        result = "[MOCK] empty scaffold from template (ROBOT_MOCK_CREATION=1)"
        sid = ""
        # mock 模式手动 patch 了 main.gd，算作 1 个文件写入 —— 让下游
        # generation_failed 的 n_files == 0 检查放行，否则 mock 永远 false-positive
        # 失败。
        n_files = 1
        gd_path = game_dir / "scenes" / "main.gd"
        gd_text = gd_path.read_text(encoding="utf-8")
        # 追加 `[MOCK]` 标记 — 之前用 str.replace 找模板里某个固定注释字符串，
        # 模板内容一改（如重写成 SKELETON 风格）replace 就静默失效，导致 mock
        # 路径下 main.gd 不含 MOCK 标识，下游 iOS QA 看不到「这是 mock 生成」
        # 提示。改成 append 后无论模板长什么样都能稳定打 marker。
        if "[MOCK]" not in gd_text:
            gd_text = gd_text.rstrip() + "\n\n# [MOCK] template scaffold (no LLM was invoked).\n"
            gd_path.write_text(gd_text, encoding="utf-8")
        logger.info(
            "[GameTool] [2/5] MOCK MODE — skipped CodeAgent, using template scaffold")
    else:
        logger.info(
            "[GameTool] [2/5] launching CodeAgent for %r (input_mode=%s, resume=%s) ...",
            title, input_mode,
            template_inherited_sid[:12] + "…" if template_inherited_sid else "none",
        )
        await _send_game_progress_key("GAME_PROGRESS_GENERATING_CODE")
        # 把模板继承的 sid 透传给 SDK，让 SDK 在新 cwd 下 resume 模板的设计
        # 对话；模板没带 session 时（_template 默认空脚手架）走 fresh start。
        result, sid, n_files = await _run_claude_agent(
            str(game_dir), prompt, input_mode=input_mode,
            session_id=template_inherited_sid or "",
        )
        logger.info("[GameTool] [2/5] CodeAgent done | elapsed=%.1fs result_len=%d files=%d session=%s",
                    time.time() - t0, len(result), n_files, sid or "(none)")

        # 2.5. Validate syntax — gdparse 仅作快速预报告，不在这里修。
        # create 流程下游 (步骤 4) 还有一道 godot --check-only + haiku fix loop
        # （更准、能抓类型错），让它一次性收尾。
        await _send_game_progress_key("GAME_PROGRESS_CHECKING_QUALITY")
        fix_log = await _validate_and_fix(game_dir, game_id, check_only=True)
        if fix_log:
            logger.info("[GameTool] [2.5/5] gdparse pre-report: %s (handled downstream)", fix_log)

    # 3. Verify basic file structure — check main files exist
    await _send_game_progress_key("GAME_PROGRESS_VERIFYING_STRUCTURE")
    main_gd = game_dir / "scenes" / "main.gd"
    main_tscn = game_dir / "scenes" / "main.tscn"

    gd_ok = main_gd.exists()
    tscn_ok = main_tscn.exists()

    # 删除历史"wrong path fallback"：旧实现在 gd_ok=False 时主动从硬编码绝对路
    # 径 `/home/developer/work/robot/scenes/main.gd` 等位置拷文件回正路径。
    # 风险：(1) 路径泄漏部署主机；(2) 与"AI 只在 cwd 内操作"的安全模型冲突，
    # 配合 prompt-injection 可让 AI 把 main.gd Write 到 cwd 外的"诱饵路径"，
    # 后端自动 copy 进受害游戏目录 → 攻击链终点。实测在生产命中率 ~0（开发者
    # 本地路径），删掉零代价。AI 没写 main.gd 直接走 incomplete=True 分支，
    # `game_creation_failed` 已闭环（W1 P0-4）。

    incomplete = False
    if not gd_ok or not tscn_ok:
        logger.warning(
            "[GameTool] [3/5] ⚠ missing main files (main.gd=%s main.tscn=%s)", gd_ok, tscn_ok)
        incomplete = True
    else:
        logger.info("[GameTool] [3/5] file structure OK")

    # 这里以前有一道 cancel_check："如果 speech.stop / WS 断了，删 game_dir 直接 return"。
    # 实际场景下 cancel_check 会被 speech.stop（用户在 60s 等待里换个话题、
    # 短暂停顿后再说话）触发，但代码已经写了 67s、AI 已经吐完文件、validation
    # 已经过 —— 这时再 wipe game_dir 等于把沉没成本扔了，前端永远收不到
    # game_ready，UI 卡在最后一条 progress 消息。所以已经移除。
    # 用户如果真的不想要这个游戏，可以在 Mine Tab 里删（DB 行删除会级联清 zip + 目录）。

    # 生成失败检测：三道防线 ——
    # 1) incomplete: main.gd / main.tscn 缺失（极少触发，_template copy 失败才会）
    # 2) result 空: SDK 无任何文本输出（inactivity timeout 后零输出）
    # 3) n_files == 0: SDK 没用过 Write/Edit 工具（CLI 缺失、SDK 抛异常、wall-clock
    #    budget exceeded 等场景。历史上 CLI 缺失时 result 会被填成 _tr(error) 字符串
    #    绕开 result-empty 检查，n_files 是唯一可靠信号）。
    # 任一为真都绝不能继续 zip/DB/game_ready —— iOS 会下载到一个只有空骨架的 zip。
    # 处理方式与 cancel 一致：删 game_dir、跳过 DB、给 iOS 推 game_creation_failed。
    result_empty = not (result or "").strip()
    no_files_written = n_files == 0
    generation_failed = incomplete or result_empty or no_files_written
    if generation_failed:
        # reason 三档（iOS 文案分别对应不同的 retry 引导）：
        # - result 为空 ⇒ SDK 完全没说话（CLI 缺失 / inactivity timeout）。
        # - result 有内容但 n_files=0 或 main.gd 缺失 ⇒ AI 在「干瞪眼」，
        #   说了些话但没真改文件，最常见原因是 prompt 误导或 model 摆烂。
        if result_empty:
            reason = "ai_no_output"
        else:
            reason = "missing_main_files"
        logger.warning(
            "[GameTool] generation FAILED (incomplete=%s result_empty=%s n_files=%d reason=%s) "
            "— cleaning up game_dir %s",
            incomplete, result_empty, n_files, reason, game_id,
        )
        from robot.games._session_paths import wipe_game_artifacts
        wipe_game_artifacts(game_dir)
        # 通知 iOS：清掉 loading 状态、显示重试 toast
        if ctx._ws_manager:
            try:
                await ctx._ws_manager.send_to_user(
                    owner_id,
                    user_id,
                    {
                        "type": "robot_command",
                        "data": {
                            "action": "game_creation_failed",
                            "game_id": game_id,
                            "title": title,
                            "reason": reason,
                        },
                    },
                )
            except Exception as e:
                logger.warning(
                    "[GameTool] failed to notify iOS of game_creation_failed: %s", e)
        return _tr("GAME_CREATE_FAILED", title=title)

    # 4. Validate GDScript syntax + auto self-fix loop
    # AI 反复踩两个 GDScript 坑：duplicate func（想 override 但 GDScript 不允许）+
    # `:= game.X` 类型推导失败（Variant 推不出）。Prompt 强约束不够保险；这里
    # 跑 godot --check-only 实测 parse，发现 Parse Error 就把错误 + 行号传给
    # CodeAgent 让它 self-fix。最多 2 轮，避免无限循环。
    ok, errs = _validate_gdscript(game_dir)
    fix_attempt = 0
    while not ok and fix_attempt < 2:
        fix_attempt += 1
        logger.warning("[GameTool] [3.5/5] syntax fix attempt %d/2: %s",
                       fix_attempt, errs[:200])
        await _send_game_progress_key("GAME_PROGRESS_FIXING_SYNTAX") if False else None
        fix_prompt = (
            f"# MODE: SYNTAX FIX\n\n"
            f"GDScript parse error in `scenes/main.gd`:\n```\n{errs}\n```\n\n"
            f"## Workflow\n"
            f"1. `Read scenes/main.gd` (use RELATIVE path, cwd is already the game dir).\n"
            f"2. Locate the error at the reported line number.\n"
            f"3. `Edit scenes/main.gd` to fix the issue.\n\n"
            f"## Common causes & fixes\n"
            f"- **Duplicate `func name`** (most common): GDScript has no `override` "
            f"keyword. If you added a 2nd `func _get_hud_text` / `_check_game_over` / "
            f"`_handle_gameplay_tap` / `_draw_start_screen` / `_draw_game_over_screen` "
            f"at the bottom of main.gd, **DELETE that duplicate** (the whole `func` "
            f"block including body). The skeleton already has the original at the top.\n"
            f"- **`var x := game.X` or `var x := dict[\"k\"]`** type inference fail: "
            f"change `:=` to `: <Type> =` (e.g. `var spd: float = game.wave * 10.0`).\n\n"
            f"## Constraints\n"
            f"- Use RELATIVE paths only (`scenes/main.gd`, not `/root/...` or absolute).\n"
            f"- Do NOT Read the entire game directory; Read the specific file.\n"
            f"- One Edit is usually enough. No need for description_i18n in your reply.")
        try:
            _fix_result, _fix_sid, _fix_n = await _run_claude_agent(
                str(game_dir), fix_prompt, input_mode=input_mode,
                max_turns=4, use_mcp=False,
                session_id=sid or "", model=settings.game_modify_model,
            )
        except Exception as e:
            logger.error("[GameTool] syntax fix-loop error: %s", e)
            break
        ok, errs = _validate_gdscript(game_dir)

    if not ok:
        logger.error("[GameTool] [3.5/5] syntax still broken after %d fix attempts: %s",
                     fix_attempt, errs[:300])
    else:
        logger.info("[GameTool] [3.5/5] syntax OK (fix_attempts=%d)", fix_attempt)

    # 5. Harvest audio + import assets + generate script cache + zip
    await _send_game_progress_key("GAME_PROGRESS_PACKING")
    _harvest_audio(game_dir)
    # iOS SwiftGodotKit 加载 res://assets/*.png 需要 .import 元数据 +
    # .godot/imported/<hash>.ctex 配套；后端 headless 流程不走 editor，必须
    # 显式跑一次 godot --headless --import 生成。否则 load("res://x.png")
    # 返回 null → game scripts 触发 segfault。
    _import_assets(game_dir)
    _generate_script_cache(game_dir)
    zip_path = GAMES_DIR / f"{game_id}.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))
    zip_size_kb = zip_path.stat().st_size // 1024
    logger.info("[GameTool] [4/5] zip created: %s (%d KB)",
                zip_path.name, zip_size_kb)

    # zip 完成后不再 honor cancel_check —— 同上理由：代码、zip 都已就绪，
    # 不要因为用户语音节奏触发的 speech.stop 把整局丢了。让 game_ready 发出去，
    # 不想要的用户可以从 Mine Tab 删。

    # 5. Save metadata
    title_map = _coerce_i18n_map(title_i18n, title)
    description_map = _coerce_i18n_map(description_i18n, description)
    # 当前阶段只支持背景图替换 —— host 已预创建 background.png，meta 里登记
    # 这一项足以让 iOS view tab 列出"替换背景"入口。其他元素图片替换以后再说。
    replaceable_assets = [{
        "filename": "background.png",
        "role": "background",
        "label_en": "Background",
        "label_zh": "背景",
    }]
    meta = {
        "game_id": game_id,
        "title": title_map.get(ctx.get_user_lang(), title_map["en"]),
        "title_i18n": title_map,
        "icon": icon,
        "description": description_map.get(ctx.get_user_lang(), description_map["en"]),
        "description_i18n": description_map,
        "input_mode": input_mode,
        "created_at": int(time.time()),
        "modified_at": int(time.time()),
        "owner_id": owner_id,
        "session_id": sid,
        "template_from": template_game_id if template_label != "_template" else "",
        "replaceable_assets": replaceable_assets,
        "history": [
            {"role": "user", "content": description[:200]},
            {"role": "assistant", "content": result[:200]},
        ],
    }
    (game_dir / "meta.json").write_text(json.dumps(meta,
                                                   ensure_ascii=False), encoding="utf-8")

    # 5b. Persist DB row (ai_games marketplace 主表 + 写入 user_game_view).
    # Best-effort —— DB 不可用时游戏创建路径不应该失败，meta.json 在盘上
    # 仍然完整，下次 alembic 迁移会回灌。
    try:
        from robot.games.service import upsert_game_row
        # `template_from` 反映这个 game 是用哪个 template clone 出来的，与 meta.json
        # 同 source；纯新建（template=_template 默认空模板）记空字符串。
        template_from = (
            template_game_id if template_label != "_template" else ""
        )
        await upsert_game_row(
            game_id=game_id,
            owner_id=str(owner_id or ""),
            title=title,
            title_i18n=title_i18n,
            description=description,
            description_i18n=description_i18n,
            icon=icon,
            input_mode=input_mode,
            template_from=template_from,
            session_id=sid,
            visibility="private",  # 创建默认 private，由用户进 visibility picker 后改公开
            kind="ai",
        )
    except Exception as e:
        logger.warning("[GameTool] upsert_game_row failed (non-fatal): %s", e)

    # 6. Notify iOS via robot_command
    if ctx._ws_manager:
        await ctx._ws_manager.send_to_user(
            owner_id,
            user_id,
            {
                "type": "robot_command",
                "data": {
                    "action": "game_ready",
                    "game_id": game_id,
                    "title": title,
                    "title_i18n": title_map,
                    "description_i18n": description_map,
                    "icon": icon,
                    "input_mode": input_mode,
                },
            },
        )
        logger.info(
            "[GameTool] [5/5] ✓ game_ready sent to iOS | game_id=%s total=%.1fs", game_id, time.time() - t0)
    else:
        logger.warning(
            "[GameTool] [5/5] ws_manager not available, game_ready NOT sent")

    # Web (HTML5) export + publish — async, fire-and-forget, gated by
    # settings.web_export_enabled. Publishes the public /play/{game_id}/ link.
    try:
        from robot.games.web_export import export_and_publish_async

        asyncio.create_task(export_and_publish_async(game_id))
    except Exception as e:  # noqa: BLE001 - must never break create
        logger.warning("[GameTool] web export schedule failed (non-fatal): %s", e)

    # 注：incomplete 分支已在前面 generation_failed 检测中提前 return 处理。
    # 走到这里 = 文件齐全 + 用户没取消 + zip/DB/game_ready 都成功。
    return _tr("GAME_CREATE_DONE", title=title, game_id=game_id)


@tool
async def modify_game(game_id: str, instruction: str) -> str:
    """Modify an existing game when the user asks for a game change.

    **Always-call**: Every "改一下 / 把 X 改成 Y" utterance is a fresh modify
    request — DO NOT short-circuit with text like "I already changed that".
    Always call the tool again with the new instruction.

    Args:
        game_id: ID of the game to modify.
        instruction: Modification request describing what to change.
    """
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    t0 = time.time()
    logger.info(
        "[RemixDiag] modify start game_id=%s owner_ctx=%s user_ctx=%s instruction=%r",
        game_id, owner_id, user_id, instruction[:100],
    )

    # P0 强校验：game_id 必须严格匹配 `ai_<ts>_<md5[:6]>`，否则任意登录用户可
    # 通过 [game_modify]{"game_id":"/abs/path"} 让 Claude Code SDK 以 backend
    # 进程身份在主机任意目录读写文件（pathlib 在右侧绝对路径时返回右侧本身，
    # 旧版 ownership check 因 meta.json 缺失 `if meta_owner` 整段跳过）。
    if not _is_safe_ai_game_id(game_id):
        logger.warning("[GameTool] modify_game refused: invalid game_id=%r", game_id)
        await _send_game_modification_failed(game_id, "invalid_game_id")
        return _tr("GAME_NOT_FOUND_DETAIL", game_id=game_id)

    game_dir = GAMES_DIR / game_id
    # Defense in depth：即便上面 pattern 校验通过，也再检查 resolve 后仍在
    # GAMES_DIR 内（保险措施，正则匹配的 id 理论上不可能逃出 GAMES_DIR）。
    try:
        if not game_dir.resolve().is_relative_to(GAMES_DIR.resolve()):
            logger.error("[GameTool] modify_game refused: resolved path escapes GAMES_DIR (%s)", game_dir)
            await _send_game_modification_failed(game_id, "invalid_game_id")
            return _tr("GAME_NOT_FOUND_DETAIL", game_id=game_id)
    except (OSError, ValueError) as e:
        logger.error("[GameTool] modify_game refused: resolve failed for %s: %s", game_dir, e)
        await _send_game_modification_failed(game_id, "invalid_game_id")
        return _tr("GAME_NOT_FOUND_DETAIL", game_id=game_id)

    if not game_dir.is_dir():
        logger.error("[GameTool] game dir not found: %s", game_dir)
        await _send_game_modification_failed(game_id, "game_not_found")
        return _tr("GAME_NOT_FOUND_DETAIL", game_id=game_id)

    # 1. Snapshot current files
    old_manifest = _compute_file_manifest(game_dir)
    logger.info("[GameTool] [1/4] snapshot: %d files", len(old_manifest))

    # 2. Read meta.json for input_mode + prev session_id（不再读 history，
    # B 体系已删）
    import json as _json
    meta_path = game_dir / "meta.json"
    meta = {}
    input_mode = "touch"
    prev_session_id = ""
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            input_mode = meta.get("input_mode", "touch")
            prev_session_id = meta.get("session_id", "")
        except Exception:
            pass

    # 2.1. Ownership gate — modify_game **必须**只允许游戏作者改自己的游戏。
    # 不加这个 check 的话，bob 进 alice 的 public_remixable 游戏，游戏内
    # ✏️ pencil 触发的 modify 会直接覆盖 alice 的原作（前端虽然已经隐藏
    # 该按钮，但 WS endpoint 仍是裸接口；其它客户端 / 旧版本 iOS / 直接
    # 拼 [game_modify] payload 都能绕过 UI 校验）。
    # Ownership 校验 — 必须以 DB 中 ai_games.owner_id 为准。之前只读
    # `meta.json.owner_id` 有两个 P0 弱点：
    #  (1) meta.json 在 game_dir 里，AI 在 modify 过程中通过 Write 工具完全可
    #      以重写它（prompt 注入 → 改 owner_id 为攻击者 → 下一次 modify 校验过）。
    #  (2) `if meta_owner and ...` 在 meta_owner 为空（缺 / 解析失败）时**整段
    #      跳过**校验，等价默认放行。
    # 现在改为：先查 DB row，找到 → 强校验 row.owner_id == user_id；DB 行缺失
    # 时拒绝（防御性，老 ai_* 未迁移已经被 P0 visibility 修复覆盖）。
    # meta.json.owner_id 仅在 DB 暂不可用（连接错误等基础设施故障）时做软兜底，
    # 不再作为权威源。
    db_owner: str | None = None
    try:
        from robot.db.database import async_session
        from robot.db.models import AIGame
        from sqlalchemy import select
        async with async_session() as _db:
            row = (
                await _db.execute(select(AIGame).where(AIGame.game_id == game_id))
            ).scalar_one_or_none()
            if row is not None:
                db_owner = (row.owner_id or "").strip()
                logger.info(
                    "[RemixDiag] modify ownership game_id=%s user_ctx=%s db_owner=%s "
                    "visibility=%s original_game_id=%s kind=%s",
                    game_id, user_id, db_owner, row.visibility,
                    row.original_game_id, row.kind,
                )
            else:
                logger.warning(
                    "[RemixDiag] modify refused: no ai_games row game_id=%s user_ctx=%s",
                    game_id, user_id,
                )
                await _send_game_modification_failed(game_id, "game_not_found")
                return _tr("GAME_NOT_FOUND_DETAIL", game_id=game_id)
    except Exception as e:
        # DB 不可用时 fall back 到 meta.json — 仅在基础设施故障场景下放行，
        # 同时记 ERROR 让监控告警。绝大多数生产请求走 DB 分支。
        logger.error(
            "[GameTool] modify_game ownership DB check failed (falling back to meta.json): %s", e)

    meta_owner = (meta.get("owner_id") or "").strip()
    authoritative_owner = db_owner if db_owner is not None else meta_owner
    logger.info(
        "[RemixDiag] modify owner decision game_id=%s user_ctx=%s db_owner=%s "
        "meta_owner=%s authoritative_owner=%s",
        game_id, user_id, db_owner, meta_owner, authoritative_owner,
    )
    if authoritative_owner and user_id and authoritative_owner != user_id:
        logger.warning(
            "[RemixDiag] modify refused: not owner user_ctx=%s authoritative_owner=%s "
            "game_id=%s source=%s",
            user_id, authoritative_owner, game_id,
            "db" if db_owner is not None else "meta_fallback",
        )
        await _send_game_modification_failed(game_id, "not_owner")
        return _tr("GAME_NOT_OWNED")
    # 安全收紧：缺 owner（DB row 有但 owner_id 空 + meta.json 也无 owner）时
    # 不再静默放行；只允许 user_id 为空（未登录调用，理论上不该发生）通过。
    if not authoritative_owner and user_id:
        logger.warning(
            "[GameTool] modify_game refused: orphan game with no owner_id (game=%s user=%s)",
            game_id, user_id,
        )
        await _send_game_modification_failed(game_id, "not_owner")
        return _tr("GAME_NOT_OWNED")

    # 注：之前这里会把 meta["history"] 最近 N 条 user/AI 摘要拼成
    # "## Modification history" 段塞进 prompt。在 session resume 工作起来
    # 之后这段 200-char 摘要相对完整 jsonl 几乎全是冗余，每次 modify 多花
    # 1-6KB 输入 tokens。现已删除注入 + 写回逻辑：jsonl (sliding-window
    # 后) 是 model 唯一的对话记忆来源，cliff fallback 那一瞬间靠当前代码现
    # 状 + 用户 instruction 推断。

    # Enumerate user-uploaded assets so the agent can reference them by name.
    # Filter to common image / audio formats and skip the cover.png itself.
    assets_section = _build_assets_section(game_dir)

    prompt = f"""# MODE: EDIT (modify an existing working game)

This game already works. Your job is to apply the user's requested change so that **a player actually notices it** — not just shift internal state.

User's edit request:
{instruction}
{assets_section}

## Step 1 — classify task complexity (do this silently, no output)

- **SIMPLE** (most cases): tweak a value, swap a color/sound, change a single visible behavior.
  Examples: "火球改大", "结束页字体调小", "音效换一下", "加一种敌人颜色".
  → Use minimal targeted Edits. Skip extensive planning. **Still must do at least one Edit/Write** —
    SIMPLE never means "no action needed".

- **VISUAL POLISH** (a flavor of complexity): request to improve a visual element with
  vague verbs like "优化 / 美化 / 让 X 更好看 / 漂亮一点".
  Examples: "优化一下背景", "美化结束页", "玩家小球漂亮一点".
  → The user wants **active rendering improvements in code** — gradients, particles,
    glow effects, animation, layered drawing. **Do NOT** interpret this as "user
    uploaded a new file" or "no action needed". Edit `_draw_*` functions or
    `_process` to add visible enhancement.

- **COMPLEX (gameplay redesign)**: request mentions gameplay feel / pacing / multi-channel feedback,
  or describes a perceptual shift like "感觉 / 节奏 / 玩起来像 / 改成 X 模式".
  Examples: "改成一波一波", "更刺激", "加难度曲线", "战斗反馈强一点".
  → Plan the player-facing changes across **all** relevant channels before editing.
    A complex task usually needs:
      (a) **code/logic** change (the underlying mechanic)
      (b) **visible UI cue** — on-screen text, color, animation
      (c) **audio cue** — AudioManager.play("xxx") at the right moment
      (d) **timing/pacing** — delays, pauses, transitions
    Internal-state-only changes for a COMPLEX task are **wrong** — the player
    won't notice. Implement at least (a) + one of (b)/(c)/(d).

**Hard rule**: regardless of category, never end with 0 tool calls and 0 file changes.
If you genuinely cannot determine what to change, ask a clarifying question via the
description text — but for any actionable visual/gameplay request, you **must** Edit
something.

## Step 2 — implement

- Edit for local changes. Write for whole-file rewrites (only if most of file changes).
- **DO NOT use the Bash tool** — it is not available in this sandbox and will fail.
- **If an Edit fails twice on the same target** (string-not-found / mismatched whitespace),
  fall back to Write the whole file. Do NOT loop on whitespace debugging.
- Don't refactor code the user didn't ask to change.
- Don't re-read a file you just edited — trust the Edit result.

Important: all Read/Write/Edit paths must be relative (e.g., `scenes/main.gd`). Absolute paths forbidden.

## Step 3 — self-check before responding

For SIMPLE tasks: verify the value/visual you intended to change is actually
different in the code now.

For COMPLEX tasks: walk through your changes and verify at least one of the
non-(a) channels was actually touched:
  - Did you add `AudioManager.play(...)` somewhere meaningful?
  - Did you add on-screen text or visible UI element?
  - Did you add a delay/pause/transition?
If only (a) (logic state) was touched, you missed the user's intent.
**Add the missing channel(s) now before writing the description**.

Final response format: a single line, nothing else:
description_i18n: {{"en": "one English sentence describing the current complete gameplay, including this modification", "zh-Hans": "一句简体中文，描述当前完整玩法并包含本次修改"}}"""

    # Inject input mode hint
    if input_mode == "body_pose":
        prompt += "\n\nThis game uses body pose / visual control. Use InputBridge body_lean_x/y and face_position. Do not use touch input for gameplay; touch is only for start/retry. Use position control with lerp smoothing. Do not use velocity control."

    if _is_mock_creation_mode():
        # Mock mode — append a comment line to scenes/main.gd so the diff
        # computer produces exactly one modify diff; gives iOS a real
        # game_patch payload to test the round-trip without burning LLM tokens.
        gd_path = game_dir / "scenes" / "main.gd"
        gd_path.write_text(
            gd_path.read_text(encoding="utf-8")
            + f"\n# [MOCK modify @ {int(time.time())}] {instruction[:80]}\n",
            encoding="utf-8",
        )
        result = "[MOCK] no-op modify (ROBOT_MOCK_CREATION=1)"
        sid = prev_session_id or ""
        logger.info(
            "[GameTool] [2/4] MOCK MODE — appended mock comment to main.gd")
    else:
        logger.info(
            "[GameTool] [2/4] launching CodeAgent for modify (input_mode=%s, model=%s, resume=%s) ...",
            input_mode, settings.game_modify_model,
            prev_session_id[:12] + "..." if prev_session_id else "none")
        await _send_game_progress_key("GAME_PROGRESS_MODIFYING")
        # modify 路径用专门配的 game_modify_model（默认 haiku 4.5），与 create
        # 路径用的 game_code_model（默认 glm 5.1）拆开。理由见 config.py 注释。
        result, sid, n_files = await _run_claude_agent(
            str(game_dir), prompt, input_mode=input_mode, max_turns=12,
            use_mcp=False, session_id=prev_session_id,
            model=settings.game_modify_model,
        )
        logger.info("[GameTool] [2/4] CodeAgent done | elapsed=%.1fs result_len=%d files=%d session=%s",
                    time.time() - t0, len(result), n_files, sid or "(none)")

        # 2.5. Validate and fix syntax errors
        await _send_game_progress_key("GAME_PROGRESS_CHECKING_QUALITY")
        fix_log = await _validate_and_fix(game_dir, game_id)
        if fix_log:
            logger.info("[GameTool] [2.5/4] validation fixes: %s", fix_log)

    # 2.6. Harvest audio: copy any newly referenced .ogg into game's audio/
    # and inject AudioManager autoload if it didn't exist (covers older games
    # that pre-date the audio system).
    #
    # KNOWN LIMITATION: audio files are binary and not included in
    # _compute_file_manifest() (which only diffs text files). The on-disk zip
    # picks them up correctly, but the diffs sent over WebSocket do not.
    # Result: iOS gets the new .gd / .tscn changes via patch but won't have
    # the audio file locally until the user relaunches the game (which
    # re-downloads the full zip). Acceptable degradation for now; can be
    # improved later by base64-including audio in the diff manifest.
    _harvest_audio(game_dir)

    # 3. Compute diffs
    new_manifest = _compute_file_manifest(game_dir)
    diffs = _compute_diffs(old_manifest, new_manifest)
    logger.info("[GameTool] [3/4] diffs: %d changes | %s",
                len(diffs), ", ".join(f"{d['op']}:{d['path']}" for d in diffs))

    # 4. Re-create zip + notify iOS
    await _send_game_progress_key("GAME_PROGRESS_PACKING")
    # 增量 import：godot 用 .md5 跳过未变资源，modify 路径几乎没新增 PNG/OGG
    # 时 < 1s。仍保留为防止：模型修改时新增 asset 文件 / 老游戏首次进入流程。
    _import_assets(game_dir)
    _generate_script_cache(game_dir)
    zip_path = GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))
    zip_size_kb = zip_path.stat().st_size // 1024
    logger.info("[GameTool] [4/4] zip updated: %s (%d KB)",
                zip_path.name, zip_size_kb)

    if ctx._ws_manager and diffs:
        await ctx._ws_manager.send_to_user(
            owner_id,
            user_id,
            {
                "type": "robot_command",
                "data": {
                    "action": "game_patch",
                    "game_id": game_id,
                    "diffs": diffs,
                },
            },
        )
        logger.info("[GameTool] ✓ game_patch sent | diffs=%d total=%.1fs", len(
            diffs), time.time() - t0)
    elif not diffs:
        logger.warning("[GameTool] no diffs detected after modify")

    if not diffs:
        await _send_game_progress_key("GAME_PROGRESS_EMPTY")
        # iOS isModifying / aiGameCreating 仅在收到 game_patch 或失败事件时复位；
        # no-diffs 情况下不发 patch，必须显式推 modification_failed 让 UI 解锁
        # （否则 GameCreator 永久卡在 sending 态——三方 review 共识 P0 之一）。
        await _send_game_modification_failed(game_id, "no_changes")
        return _tr("GAME_NO_CHANGES")

    # Update meta.json session_id + description (B 体系 history 已删；不再
    # 追加 / cap meta["history"]。旧字段在老 meta 里残留也不影响——下面只读
    # session_id 和写 description_i18n / modified_at)。
    try:
        meta["modified_at"] = int(time.time())
        if sid:
            meta["session_id"] = sid
        description_i18n = _extract_description_i18n_from_result(result)
        if description_i18n:
            meta["description_i18n"] = description_i18n
            meta["description"] = description_i18n.get(ctx.get_user_lang(), description_i18n["en"])
        elif "---" in result:
            after_sep = result.split("---")[-1]
            for line in after_sep.strip().split("\n"):
                line = line.strip()
                if line.startswith("Game description:") or line.startswith("游戏描述：") or line.startswith("游戏描述:"):
                    desc = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                    if desc:
                        meta["description"] = desc[:_MAX_DESCRIPTION_LEN]
                    break
        meta_path.write_text(_json.dumps(
            meta, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("[GameTool] failed to update meta.json history: %s", e)

    # Refresh DB row — 不传 visibility / kind / original_game_id，让 service
    # 在 update 路径保留原值（与 widget 同款"作者修改不新建"语义）。失败 non-fatal。
    try:
        from robot.games.service import upsert_game_row
        await upsert_game_row(
            game_id=game_id,
            owner_id=str(owner_id or ""),
            title=meta.get("title", game_id),
            title_i18n=meta.get("title_i18n", {"en": meta.get("title", game_id), "zh-Hans": meta.get("title", game_id)}),
            description=meta.get("description", ""),
            description_i18n=meta.get("description_i18n", {"en": "", "zh-Hans": ""}),
            icon=meta.get("icon", "🎮"),
            input_mode=meta.get("input_mode", "touch"),
            template_from=meta.get("template_from", ""),
            session_id=meta.get("session_id"),
        )
    except Exception as e:
        logger.warning("[GameTool] upsert_game_row failed (non-fatal): %s", e)

    # Web (HTML5) re-export + publish — async, fire-and-forget, gated. Same
    # /play/{game_id}/ link, refreshed content (modify-in-place semantics).
    try:
        from robot.games.web_export import export_and_publish_async

        asyncio.create_task(export_and_publish_async(game_id))
    except Exception as e:  # noqa: BLE001 - must never break modify
        logger.warning("[GameTool] web export schedule failed (non-fatal): %s", e)

    await _send_game_progress_key("GAME_PROGRESS_EMPTY")
    changed_files = [d["path"] for d in diffs]
    return _tr("GAME_MODIFY_DONE", count=len(diffs), files=", ".join(changed_files))
