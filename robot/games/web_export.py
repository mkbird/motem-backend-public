"""Godot Web (HTML5) export pipeline for AI-generated games.

Turns a generated Godot game dir (`games/{game_id}/`) into a self-contained,
browser-playable HTML5 bundle. Mirror runtime of the iOS SwiftGodotKit path —
same game files, different Godot runtime — so games are NOT rewritten for web.

The pipeline applies four web-compat steps (all verified end-to-end on a real
fast-path game) on a throwaway workspace copy, never mutating the source game:

  1. Renderer: ``rendering_method`` mobile (Vulkan) -> ``gl_compatibility``
     (WebGL2); Vulkan does not run on the web.
  2. Fixed infra scripts (``input_bridge.gd`` / ``audio_manager.gd``) refreshed
     from the canonical ``_template`` so older games pick up their web branches
     (``OS.has_feature("web")`` paths: InputBridge polls ``window.__motem_input``;
     AudioManager loads via ``ResourceLoader`` instead of raw bytes).
  3. CJK font bundled into ``fonts/`` (the iOS host injects it at runtime; the
     web build must ship it or Chinese text won't render).
  4. Web runtime shell (``motem_input.js`` — MediaPipe vision stack) copied in
     and injected into ``index.html``.

Then runs ``godot --headless --import`` + ``--export-release "Web"``.

Requires a Godot 4.x binary with the matching **Web export templates**
installed (``settings.godot_path`` or ``godot`` on PATH). This is an infra
dependency separate from game generation, which only uses ``--import`` /
``--check-only``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]  # .../motem-backend
GAMES_DIR = _BACKEND_ROOT / "games"
# Published, publicly-served web bundles live here (one dir per game_id),
# decoupled from the transient export workspace. Served by play_router at
# /play/{game_id}/.
PUBLISH_DIR = _BACKEND_ROOT / "web_builds"
TEMPLATE_SCRIPTS_DIR = GAMES_DIR / "_template" / "scripts"
FONT_SRC = _BACKEND_ROOT / "robot" / "games" / "assets" / "fonts" / "NotoSansSC-Regular.otf"
WEB_RUNTIME_DIR = _BACKEND_ROOT / "robot" / "games" / "assets" / "web_runtime"
RUNTIME_JS = "motem_input.js"

# Fixed-infrastructure scripts refreshed from the canonical template on every
# export. They are "DO NOT MODIFY by AI" files, so overwriting a game's
# (possibly stale) copy with the canonical one is always safe and is what
# gives older games their web branches.
FIXED_SCRIPTS = ("input_bridge.gd", "audio_manager.gd")

# Dirs/files never copied into the export workspace.
_COPY_IGNORE = shutil.ignore_patterns(
    ".godot", "build", ".webbuild", "__pycache__", ".claude", "*.zip"
)

_RUNTIME_SCRIPT_TAG = f'  <script type="module" src="{RUNTIME_JS}"></script>\n'

# Single-threaded (nothreads) Web preset: avoids SharedArrayBuffer / COOP-COEP
# cross-origin-isolation headers, so a plain static server can host it.
_EXPORT_PRESET = """[preset.0]

name="Web"
platform="Web"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="build/index.html"
patches=PackedStringArray()
encryption_include_filters=""
encryption_exclude_filters=""
seed=0
encrypt_pck=false
encrypt_directory=false
script_export_mode=2

[preset.0.options]

custom_template/debug=""
custom_template/release=""
variant/extensions_support=false
variant/thread_support=false
vram_texture_compression/for_desktop=true
vram_texture_compression/for_mobile=false
html/export_icon=true
html/custom_html_shell=""
html/head_include=""
html/canvas_resize_policy=2
html/focus_canvas_on_start=true
html/experimental_virtual_keyboard=false
html/godot_pool_size_multiplier=1
progressive_web_app/enabled=false
"""


# ---------------------------------------------------------------------------
# Pure transforms (no Godot, no filesystem) — unit-testable.
# ---------------------------------------------------------------------------

def set_gl_compatibility(project_godot_text: str) -> str:
    """Force ``rendering_method`` to ``gl_compatibility`` for WebGL2.

    Replaces an existing value, or inserts the keys if absent.
    """
    text = project_godot_text
    if 'renderer/rendering_method="mobile"' in text:
        return text.replace(
            'renderer/rendering_method="mobile"',
            'renderer/rendering_method="gl_compatibility"',
        )
    if 'renderer/rendering_method="gl_compatibility"' in text:
        return text  # already correct
    if 'renderer/rendering_method=' in text:
        # some other value (e.g. "forward_plus") -> rewrite the line
        lines = []
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("renderer/rendering_method="):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f'{indent}renderer/rendering_method="gl_compatibility"\n')
            else:
                lines.append(line)
        return "".join(lines)
    # No rendering_method anywhere: add under [rendering] (or append section).
    if "[rendering]" in text:
        return text.replace(
            "[rendering]",
            '[rendering]\n\nrenderer/rendering_method="gl_compatibility"',
            1,
        )
    sep = "" if text.endswith("\n") else "\n"
    return text + f'{sep}\n[rendering]\n\nrenderer/rendering_method="gl_compatibility"\n'


def enable_mouse_touch_emulation(project_godot_text: str) -> str:
    """Make desktop mouse clicks emit ``InputEventScreenTouch``.

    The game skeleton's ``_input`` only handles ``InputEventScreenTouch`` (built
    for mobile), so on a desktop browser a mouse click never triggers
    tap-to-start / tap-to-restart. Enabling Godot's
    ``input_devices/pointing/emulate_touch_from_mouse`` synthesizes touch from
    mouse, so the existing touch-only handlers work everywhere — no per-game
    code change. Applied only in the web workspace; iOS uses real touch.
    """
    text = project_godot_text
    key = "pointing/emulate_touch_from_mouse=true"
    if "pointing/emulate_touch_from_mouse=" in text:
        lines = []
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("pointing/emulate_touch_from_mouse="):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f"{indent}{key}\n")
            else:
                lines.append(line)
        return "".join(lines)
    if "[input_devices]" in text:
        return text.replace("[input_devices]", f"[input_devices]\n\n{key}", 1)
    sep = "" if text.endswith("\n") else "\n"
    return text + f"{sep}\n[input_devices]\n\n{key}\n"


def inject_runtime_script(index_html: str) -> str:
    """Inject the web runtime ``<script>`` before ``</body>`` (idempotent)."""
    if RUNTIME_JS in index_html:
        return index_html
    if "</body>" in index_html:
        return index_html.replace("</body>", _RUNTIME_SCRIPT_TAG + "</body>", 1)
    return index_html + "\n" + _RUNTIME_SCRIPT_TAG


# ---------------------------------------------------------------------------
# Workspace preparation (filesystem, no Godot) — unit-testable.
# ---------------------------------------------------------------------------

def prepare_workspace(game_dir: Path, workspace: Path) -> Path:
    """Copy ``game_dir`` into ``workspace`` and apply the non-Godot web steps.

    Returns the workspace path. Does NOT invoke Godot. Steps applied:
    renderer flip, fixed-script refresh, font bundling, runtime JS copy.
    """
    game_dir = Path(game_dir)
    workspace = Path(workspace)
    if not (game_dir / "project.godot").is_file():
        raise FileNotFoundError(f"not a Godot game dir (no project.godot): {game_dir}")
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(game_dir, workspace, ignore=_COPY_IGNORE)

    # 1. Renderer -> gl_compatibility; mouse-click -> touch for desktop start.
    proj = workspace / "project.godot"
    proj_text = set_gl_compatibility(proj.read_text(encoding="utf-8"))
    proj_text = enable_mouse_touch_emulation(proj_text)
    proj.write_text(proj_text, encoding="utf-8")

    # 2. Refresh fixed infra scripts from the canonical template (web branches).
    scripts_dir = workspace / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for name in FIXED_SCRIPTS:
        src = TEMPLATE_SCRIPTS_DIR / name
        if src.is_file():
            shutil.copy2(src, scripts_dir / name)
        else:
            logger.warning("canonical template script missing: %s", src)

    # 3. Bundle the CJK font (iOS injects it at runtime; web must ship it).
    fonts_dir = workspace / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    if FONT_SRC.is_file():
        shutil.copy2(FONT_SRC, fonts_dir / FONT_SRC.name)
    else:
        logger.warning("CJK font missing, Chinese text will not render: %s", FONT_SRC)

    # 4. Web runtime shell (injected into index.html after export).
    build_dir = workspace / "build"
    build_dir.mkdir(exist_ok=True)
    runtime_src = WEB_RUNTIME_DIR / RUNTIME_JS
    if runtime_src.is_file():
        shutil.copy2(runtime_src, build_dir / RUNTIME_JS)
    else:
        logger.warning("web runtime shell missing: %s", runtime_src)

    return workspace


# ---------------------------------------------------------------------------
# Godot orchestration.
# ---------------------------------------------------------------------------

def _resolve_godot(godot_path: str | None) -> str:
    if godot_path:
        return godot_path
    try:
        from robot.config import settings  # lazy: avoid import cost in unit tests

        if getattr(settings, "godot_path", ""):
            return settings.godot_path
    except Exception:  # pragma: no cover - config optional in some contexts
        pass
    found = shutil.which("godot")
    if not found:
        raise RuntimeError(
            "Godot binary not found. Set settings.godot_path or put `godot` on PATH. "
            "Web export also needs the matching Web export templates installed."
        )
    return found


def _run_godot(godot_bin: str, args: list[str], *, cwd: Path, timeout: int) -> None:
    proc = subprocess.run(
        [godot_bin, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(
            f"godot {' '.join(args)} failed (rc={proc.returncode}):\n{tail}"
        )


def export_game_web(
    game_id: str,
    *,
    godot_path: str | None = None,
    workspace: Path | None = None,
    import_timeout: int = 180,
    export_timeout: int = 180,
) -> Path:
    """Export ``games/{game_id}/`` to a browser-playable HTML5 bundle.

    Returns the path to the build dir containing ``index.html``. The source
    game dir is never mutated (all work happens in ``workspace``).
    """
    game_dir = GAMES_DIR / game_id
    if not game_dir.is_dir():
        raise FileNotFoundError(f"game not found: {game_dir}")

    godot_bin = _resolve_godot(godot_path)
    workspace = Path(workspace) if workspace else game_dir / ".webbuild"

    logger.info("[web_export] %s -> %s", game_id, workspace)
    prepare_workspace(game_dir, workspace)

    # Write the Web export preset.
    (workspace / "export_presets.cfg").write_text(_EXPORT_PRESET, encoding="utf-8")

    # Import (generates .import companions + .godot/imported resources, which
    # the AudioManager web branch depends on) then export.
    _run_godot(godot_bin, ["--headless", "--import", "."], cwd=workspace, timeout=import_timeout)
    _run_godot(
        godot_bin,
        ["--headless", "--export-release", "Web", "build/index.html"],
        cwd=workspace,
        timeout=export_timeout,
    )

    build_dir = workspace / "build"
    index_html = build_dir / "index.html"
    if not index_html.is_file():
        raise RuntimeError(f"export produced no index.html in {build_dir}")

    # Inject the web runtime shell (export overwrites index.html).
    index_html.write_text(
        inject_runtime_script(index_html.read_text(encoding="utf-8")), encoding="utf-8"
    )

    logger.info("[web_export] %s done: %s", game_id, build_dir)
    return build_dir


# ---------------------------------------------------------------------------
# Publishing (serving) — copy a build into the public PUBLISH_DIR.
# ---------------------------------------------------------------------------

def published_dir(game_id: str) -> Path:
    """The public bundle dir for ``game_id`` (may not exist yet)."""
    return PUBLISH_DIR / game_id


def publish_build(game_id: str, build_dir: Path) -> Path:
    """Atomically publish ``build_dir`` as ``PUBLISH_DIR/{game_id}/``.

    A re-export (e.g. after a modify) overwrites in place, so the public
    ``/play/{game_id}/`` URL is stable and serves fresh content. The swap goes
    via a temp sibling + rename to avoid serving a half-copied bundle.
    """
    build_dir = Path(build_dir)
    if not (build_dir / "index.html").is_file():
        raise FileNotFoundError(f"no index.html in build dir: {build_dir}")
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    final = PUBLISH_DIR / game_id
    staging = PUBLISH_DIR / f".{game_id}.staging"
    old = PUBLISH_DIR / f".{game_id}.old"
    for p in (staging, old):
        if p.exists():
            shutil.rmtree(p)
    shutil.copytree(build_dir, staging)
    if final.exists():
        final.rename(old)
    staging.rename(final)
    if old.exists():
        shutil.rmtree(old)
    logger.info("[web_export] published %s -> %s", game_id, final)
    return final


def export_and_publish(game_id: str, **kwargs) -> Path:
    """Export ``game_id`` to web and publish it. Returns the published dir."""
    build_dir = export_game_web(game_id, **kwargs)
    return publish_build(game_id, build_dir)


async def export_and_publish_async(game_id: str, **kwargs):
    """Fire-and-forget web export+publish for use from the create/modify flow.

    Gated by ``settings.web_export_enabled`` (off by default). The blocking
    Godot subprocess runs in a worker thread so it never stalls the event loop.
    Any failure (missing Godot binary / Web export templates, export error) is
    swallowed with a warning — web export is a post-process and must never
    break game creation. Returns the published dir, or ``None`` if skipped or
    failed.
    """
    import asyncio

    from robot.config import settings  # lazy: keep pure transforms test-light

    if not getattr(settings, "web_export_enabled", False):
        return None
    try:
        published = await asyncio.to_thread(export_and_publish, game_id, **kwargs)
        logger.info("[web_export] background publish ok: /play/%s/", game_id)
        return published
    except Exception as e:  # noqa: BLE001 - never break the creation flow
        logger.warning("[web_export] background export failed for %s: %s", game_id, e)
        return None
