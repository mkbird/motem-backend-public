"""Integration test for the full Godot Web export (real godot subprocess).

Slow + infra-dependent (needs a Godot 4.x binary WITH Web export templates),
so it's skipped unless RUN_GODOT_INTEGRATION=1 and a godot binary is found.
Run explicitly:

    RUN_GODOT_INTEGRATION=1 conda run -n robot python -m pytest \
        tests/robot/test_web_export_integration.py -v

The pure transforms + publish + async gating are covered fast in
test_web_export.py; this exercises the godot --import/--export orchestration
and the produced bundle.
"""

from __future__ import annotations

import os
import shutil

import pytest

from robot.games import web_export


def _godot_bin() -> str | None:
    from robot.config import settings
    return settings.godot_path or shutil.which("godot")


_SKIP = not os.environ.get("RUN_GODOT_INTEGRATION") or _godot_bin() is None

pytestmark = pytest.mark.skipif(
    _SKIP, reason="set RUN_GODOT_INTEGRATION=1 and have godot+web templates installed",
)


def _a_committed_game() -> str:
    """Pick any real game dir under games/ai_* that has a main scene."""
    for d in sorted(web_export.GAMES_DIR.glob("ai_*")):
        if (d / "project.godot").is_file() and (d / "scenes" / "main.gd").is_file():
            return d.name
    pytest.skip("no committed game available to export")


def test_export_game_web_produces_playable_bundle(tmp_path):
    game_id = _a_committed_game()
    build = web_export.export_game_web(
        game_id, godot_path=_godot_bin(), workspace=tmp_path / "ws")

    # core HTML5 artifacts
    assert (build / "index.html").is_file()
    assert (build / "index.pck").is_file()
    assert (build / "index.wasm").is_file()
    # web runtime shell injected
    assert (build / web_export.RUNTIME_JS).is_file()
    assert web_export.RUNTIME_JS in (build / "index.html").read_text(encoding="utf-8")

    # workspace transforms applied
    ws = tmp_path / "ws"
    proj = (ws / "project.godot").read_text(encoding="utf-8")
    assert 'renderer/rendering_method="gl_compatibility"' in proj
    assert "pointing/emulate_touch_from_mouse=true" in proj
    assert (ws / "fonts" / "NotoSansSC-Regular.otf").is_file()
    # fixed scripts carry the web branch
    assert 'OS.has_feature("web")' in (ws / "scripts" / "input_bridge.gd").read_text(encoding="utf-8")
