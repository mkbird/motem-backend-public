"""Unit tests for the Godot Web export pipeline's non-Godot pieces.

Covers the pure transforms (renderer flip, runtime-script injection) and
``prepare_workspace`` (copy + 4 web-compat steps). The actual ``godot
--export`` invocation is an infra-dependent integration step and is not
exercised here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robot.games import web_export


# --------------------------------------------------------------------------
# set_gl_compatibility
# --------------------------------------------------------------------------

def test_flip_renderer_from_mobile():
    text = '[rendering]\n\nrenderer/rendering_method="mobile"\n'
    out = web_export.set_gl_compatibility(text)
    assert 'renderer/rendering_method="gl_compatibility"' in out
    assert "mobile" not in out


def test_flip_renderer_already_gl_is_noop():
    text = 'renderer/rendering_method="gl_compatibility"\n'
    assert web_export.set_gl_compatibility(text) == text


def test_flip_renderer_other_value():
    text = '[rendering]\nrenderer/rendering_method="forward_plus"\n'
    out = web_export.set_gl_compatibility(text)
    assert 'renderer/rendering_method="gl_compatibility"' in out
    assert "forward_plus" not in out


def test_flip_renderer_missing_key_adds_it():
    out = web_export.set_gl_compatibility("[application]\nconfig/name=\"X\"\n")
    assert 'renderer/rendering_method="gl_compatibility"' in out
    assert "[rendering]" in out


# --------------------------------------------------------------------------
# enable_mouse_touch_emulation
# --------------------------------------------------------------------------

def test_mouse_touch_emulation_adds_section():
    out = web_export.enable_mouse_touch_emulation('[application]\nconfig/name="X"\n')
    assert "[input_devices]" in out
    assert "pointing/emulate_touch_from_mouse=true" in out


def test_mouse_touch_emulation_into_existing_section():
    out = web_export.enable_mouse_touch_emulation("[input_devices]\n\nfoo=1\n")
    assert out.count("[input_devices]") == 1
    assert "pointing/emulate_touch_from_mouse=true" in out


def test_mouse_touch_emulation_rewrites_false():
    out = web_export.enable_mouse_touch_emulation(
        "[input_devices]\npointing/emulate_touch_from_mouse=false\n"
    )
    assert "pointing/emulate_touch_from_mouse=true" in out
    assert "=false" not in out


# --------------------------------------------------------------------------
# inject_runtime_script
# --------------------------------------------------------------------------

def test_inject_runtime_before_body():
    html = "<html><body>\n<canvas></canvas>\n</body></html>"
    out = web_export.inject_runtime_script(html)
    assert web_export.RUNTIME_JS in out
    assert out.index(web_export.RUNTIME_JS) < out.index("</body>")


def test_inject_runtime_idempotent():
    html = "<html><body></body></html>"
    once = web_export.inject_runtime_script(html)
    twice = web_export.inject_runtime_script(once)
    assert once == twice
    assert once.count(web_export.RUNTIME_JS) == 1


def test_inject_runtime_no_body_tag_appends():
    out = web_export.inject_runtime_script("<html></html>")
    assert web_export.RUNTIME_JS in out


# --------------------------------------------------------------------------
# prepare_workspace
# --------------------------------------------------------------------------

def _make_fake_game(root: Path) -> Path:
    """A minimal game dir resembling games/{id}/ with STALE fixed scripts."""
    g = root / "ai_fake_000000"
    (g / "scripts").mkdir(parents=True)
    (g / "scenes").mkdir()
    (g / "audio").mkdir()
    (g / "fonts").mkdir()
    (g / ".godot" / "imported").mkdir(parents=True)  # must be ignored on copy
    (g / "project.godot").write_text(
        '[application]\nconfig/name="X"\n\n[rendering]\n\n'
        'renderer/rendering_method="mobile"\n',
        encoding="utf-8",
    )
    # stale fixed scripts WITHOUT the web branch
    (g / "scripts" / "input_bridge.gd").write_text("extends Node\n# old\n", encoding="utf-8")
    (g / "scripts" / "audio_manager.gd").write_text("extends Node\n# old\n", encoding="utf-8")
    (g / "scripts" / "i18n_strings.gd").write_text("extends Node\n", encoding="utf-8")
    (g / "scenes" / "main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (g / "meta.json").write_text("{}", encoding="utf-8")
    (g / ".godot" / "imported" / "junk.txt").write_text("x", encoding="utf-8")
    return g


def test_prepare_workspace_applies_all_steps(tmp_path):
    game = _make_fake_game(tmp_path)
    ws = tmp_path / "out"
    web_export.prepare_workspace(game, ws)

    # 1. renderer flipped + mouse-touch emulation enabled
    proj = (ws / "project.godot").read_text(encoding="utf-8")
    assert 'renderer/rendering_method="gl_compatibility"' in proj
    assert "mobile" not in proj
    assert "pointing/emulate_touch_from_mouse=true" in proj

    # 2. fixed scripts refreshed from canonical template (now have web branch)
    ib = (ws / "scripts" / "input_bridge.gd").read_text(encoding="utf-8")
    am = (ws / "scripts" / "audio_manager.gd").read_text(encoding="utf-8")
    assert 'OS.has_feature("web")' in ib
    assert "_poll_web_input" in ib
    assert "# old" not in ib
    assert 'OS.has_feature("web")' in am
    assert "ResourceLoader" in am

    # per-game script left alone
    assert (ws / "scripts" / "i18n_strings.gd").is_file()

    # 3. CJK font bundled
    assert (ws / "fonts" / "NotoSansSC-Regular.otf").is_file()

    # 4. web runtime shell staged into build/
    assert (ws / "build" / web_export.RUNTIME_JS).is_file()

    # .godot editor cache excluded from the copy
    assert not (ws / ".godot").exists()


def test_prepare_workspace_rejects_non_game(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        web_export.prepare_workspace(tmp_path / "empty", tmp_path / "out")


def test_prepare_workspace_overwrites_existing(tmp_path):
    game = _make_fake_game(tmp_path)
    ws = tmp_path / "out"
    ws.mkdir()
    (ws / "stale.txt").write_text("remove me", encoding="utf-8")
    web_export.prepare_workspace(game, ws)
    assert not (ws / "stale.txt").exists()


# --------------------------------------------------------------------------
# publish_build
# --------------------------------------------------------------------------

def _make_build(root: Path, html: str) -> Path:
    b = root / "build"
    b.mkdir(parents=True, exist_ok=True)
    (b / "index.html").write_text(html, encoding="utf-8")
    (b / "index.pck").write_bytes(b"\x00pck")
    (b / "motem_input.js").write_text("// shell", encoding="utf-8")
    return b


def test_publish_build_creates_public_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "PUBLISH_DIR", tmp_path / "web_builds")
    build = _make_build(tmp_path / "v1", "<html>v1</html>")
    out = web_export.publish_build("ai_x_000001", build)
    assert (out / "index.html").read_text(encoding="utf-8") == "<html>v1</html>"
    assert out == (tmp_path / "web_builds" / "ai_x_000001")


def test_publish_build_overwrites_in_place(tmp_path, monkeypatch):
    pub = tmp_path / "web_builds"
    monkeypatch.setattr(web_export, "PUBLISH_DIR", pub)
    web_export.publish_build("g", _make_build(tmp_path / "v1", "<html>v1</html>"))
    out = web_export.publish_build("g", _make_build(tmp_path / "v2", "<html>v2</html>"))
    # stable dir, refreshed content, no leftover staging/old dirs
    assert (out / "index.html").read_text(encoding="utf-8") == "<html>v2</html>"
    leftovers = [p.name for p in pub.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_publish_build_rejects_dir_without_index(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "PUBLISH_DIR", tmp_path / "web_builds")
    empty = tmp_path / "nope"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        web_export.publish_build("g", empty)


# --------------------------------------------------------------------------
# export_and_publish_async (gated fire-and-forget trigger)
# --------------------------------------------------------------------------

def test_async_trigger_gate_off_does_not_export(monkeypatch):
    import asyncio

    import robot.config as cfg

    monkeypatch.setattr(cfg.settings, "web_export_enabled", False, raising=False)
    called = []
    monkeypatch.setattr(web_export, "export_and_publish",
                        lambda *a, **k: called.append(a))
    res = asyncio.run(web_export.export_and_publish_async("g"))
    assert res is None
    assert called == []


def test_async_trigger_gate_on_calls_export(monkeypatch, tmp_path):
    import asyncio

    import robot.config as cfg

    monkeypatch.setattr(cfg.settings, "web_export_enabled", True, raising=False)
    sentinel = tmp_path / "published"
    monkeypatch.setattr(web_export, "export_and_publish", lambda gid, **k: sentinel)
    res = asyncio.run(web_export.export_and_publish_async("g"))
    assert res == sentinel


def test_async_trigger_swallows_failure(monkeypatch):
    import asyncio

    import robot.config as cfg

    monkeypatch.setattr(cfg.settings, "web_export_enabled", True, raising=False)

    def boom(*a, **k):
        raise RuntimeError("godot not found")

    monkeypatch.setattr(web_export, "export_and_publish", boom)
    # must not raise; returns None
    assert asyncio.run(web_export.export_and_publish_async("g")) is None


def test_canonical_template_scripts_have_web_branch():
    """Guard: the source-of-truth template must carry the web branches."""
    ib = (web_export.TEMPLATE_SCRIPTS_DIR / "input_bridge.gd").read_text(encoding="utf-8")
    am = (web_export.TEMPLATE_SCRIPTS_DIR / "audio_manager.gd").read_text(encoding="utf-8")
    assert 'OS.has_feature("web")' in ib and "window.__motem_input" in ib
    assert 'OS.has_feature("web")' in am and "ResourceLoader" in am
