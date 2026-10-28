"""Audio-related game_tools tests:

- `_load_audio_manifest`: parses manifest.json, falls back gracefully.
- `_build_audio_section`: renders the system-prompt audio catalog.
- `_harvest_audio`: scans .gd / .tscn for AudioManager.play / play_bgm
  references, copies referenced files from the master library, injects the
  AudioManager autoload + script for old games.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from robot.agent.tools import game_tools
from robot.agent.tools.game_tools import (
    AUDIO_LIBRARY_DIR,
    TEMPLATE_DIR,
    _build_audio_section,
    _harvest_audio,
    _load_audio_manifest,
)


# ──────────────────────────────────────────────────────────────────────────
# manifest loader
# ──────────────────────────────────────────────────────────────────────────


class TestAudioManifest:
    def test_load_real_manifest_has_required_categories(self):
        manifest = _load_audio_manifest()
        sounds = manifest.get("sounds", [])
        assert sounds, "manifest must declare at least one sound"
        cats = {s.get("category") for s in sounds}
        # 五大类必须齐全，方便 AI 选用
        assert {"ui", "event", "action", "robot", "bgm"}.issubset(cats)

    def test_load_returns_empty_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(game_tools, "AUDIO_LIBRARY_DIR", tmp_path / "no_such_dir")
        assert _load_audio_manifest() == {"sounds": []}


# ──────────────────────────────────────────────────────────────────────────
# system prompt audio section
# ──────────────────────────────────────────────────────────────────────────


class TestAudioSection:
    def test_section_renders_real_catalog(self):
        section = _build_audio_section()
        assert "AudioManager.play" in section
        # spot-check a few well-known IDs
        assert "`click`" in section
        assert "`score_up`" in section
        assert "`jump`" in section
        # BGM section present
        assert "Background music" in section or "bgm_" in section

    def test_section_handles_empty_manifest(self, monkeypatch):
        monkeypatch.setattr(game_tools, "_load_audio_manifest",
                            lambda: {"sounds": []})
        section = _build_audio_section()
        # Empty library tells AI to skip audio rather than fabricate names.
        assert "Skip audio" in section or "currently empty" in section.lower()


# ──────────────────────────────────────────────────────────────────────────
# _harvest_audio
# ──────────────────────────────────────────────────────────────────────────


def _make_fake_library(root: Path, ids: list[str]) -> None:
    """Stand up a minimal manifest + library directory used by these tests."""
    lib = root / "library"
    (lib / "ui").mkdir(parents=True, exist_ok=True)
    sounds = []
    for sid in ids:
        f = lib / "ui" / f"{sid}.ogg"
        f.write_bytes(b"OggS\x00\x00fake-vorbis-bytes")
        sounds.append({
            "id": sid,
            "file": f"ui/{sid}.ogg",
            "category": "ui",
            "description": f"fixture-{sid}",
            "loop": False,
        })
    (root / "manifest.json").write_text(
        json.dumps({"version": 1, "sounds": sounds}), encoding="utf-8"
    )


@pytest.fixture
def fake_lib(tmp_path, monkeypatch):
    _make_fake_library(tmp_path, ["click", "score_up", "jump"])
    monkeypatch.setattr(game_tools, "AUDIO_LIBRARY_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fresh_game_dir(tmp_path):
    g = tmp_path / "game"
    (g / "scenes").mkdir(parents=True)
    (g / "scripts").mkdir(parents=True)
    return g


class TestHarvestAudio:
    def test_no_references_no_files_copied(self, fresh_game_dir, fake_lib):
        (fresh_game_dir / "scenes" / "main.gd").write_text(
            "extends Node2D\nfunc _ready(): print(\"hi\")\n", encoding="utf-8"
        )
        summary = _harvest_audio(fresh_game_dir)
        assert summary["copied"] == []
        assert summary["requested"] == []
        # Should NOT create audio/ when no refs (kept minimal)
        assert not (fresh_game_dir / "audio").exists()

    def test_copies_referenced_files(self, fresh_game_dir, fake_lib):
        (fresh_game_dir / "scenes" / "main.gd").write_text(
            'extends Node2D\nfunc _ready():\n'
            '\tAudioManager.play("click")\n'
            '\tAudioManager.play("score_up")\n',
            encoding="utf-8",
        )
        summary = _harvest_audio(fresh_game_dir)
        assert sorted(summary["copied"]) == ["click", "score_up"]
        assert (fresh_game_dir / "audio" / "click.ogg").is_file()
        assert (fresh_game_dir / "audio" / "score_up.ogg").is_file()

    def test_play_bgm_also_picked_up(self, fresh_game_dir, fake_lib):
        # Add bgm_calm to the fake library too
        bgm_dir = fake_lib / "library" / "bgm"
        bgm_dir.mkdir()
        (bgm_dir / "bgm_calm.ogg").write_bytes(b"OggS-bgm")
        manifest = json.loads((fake_lib / "manifest.json").read_text())
        manifest["sounds"].append({
            "id": "bgm_calm", "file": "bgm/bgm_calm.ogg",
            "category": "bgm", "description": "calm loop", "loop": True,
        })
        (fake_lib / "manifest.json").write_text(json.dumps(manifest))

        (fresh_game_dir / "scenes" / "main.gd").write_text(
            'extends Node2D\nfunc _ready():\n'
            '\tAudioManager.play_bgm("bgm_calm")\n',
            encoding="utf-8",
        )
        summary = _harvest_audio(fresh_game_dir)
        assert "bgm_calm" in summary["copied"]
        assert (fresh_game_dir / "audio" / "bgm_calm.ogg").is_file()

    def test_unknown_id_logged_as_missing(self, fresh_game_dir, fake_lib, caplog):
        (fresh_game_dir / "scenes" / "main.gd").write_text(
            'AudioManager.play("nonexistent_sound")\n', encoding="utf-8"
        )
        summary = _harvest_audio(fresh_game_dir)
        assert summary["copied"] == []
        assert "nonexistent_sound" in summary["missing"]

    def test_injects_audio_manager_into_old_game(self, fresh_game_dir, fake_lib):
        # Old game has no audio_manager.gd and no AudioManager autoload entry.
        proj = fresh_game_dir / "project.godot"
        proj.write_text(
            "[application]\nconfig/name=\"Old\"\n"
            "[autoload]\nInputBridge=\"*res://scripts/input_bridge.gd\"\n",
            encoding="utf-8",
        )
        (fresh_game_dir / "scenes" / "main.gd").write_text(
            'AudioManager.play("click")\n', encoding="utf-8"
        )
        # _harvest_audio falls back to TEMPLATE_DIR for audio_manager.gd.
        # Make sure it exists (it does in real repo).
        assert (TEMPLATE_DIR / "scripts" / "audio_manager.gd").exists(), \
            "template must ship audio_manager.gd for migration to work"

        _harvest_audio(fresh_game_dir)

        # New AudioManager.gd was injected
        assert (fresh_game_dir / "scripts" / "audio_manager.gd").is_file()

        # project.godot autoload section now lists AudioManager
        assert "AudioManager=" in proj.read_text(encoding="utf-8")

    def test_idempotent_when_already_set_up(self, fresh_game_dir, fake_lib):
        # Game already has audio_manager.gd and autoload entry.
        (fresh_game_dir / "scripts" / "audio_manager.gd").write_text(
            "extends Node\n# pre-existing\n", encoding="utf-8"
        )
        proj = fresh_game_dir / "project.godot"
        proj.write_text(
            '[autoload]\nAudioManager="*res://scripts/audio_manager.gd"\n',
            encoding="utf-8",
        )
        (fresh_game_dir / "scenes" / "main.gd").write_text(
            'AudioManager.play("click")\n', encoding="utf-8"
        )

        _harvest_audio(fresh_game_dir)

        # Pre-existing autoload entry not duplicated
        assert proj.read_text(encoding="utf-8").count("AudioManager=") == 1
        # Pre-existing audio_manager.gd not overwritten
        assert "pre-existing" in (fresh_game_dir / "scripts" / "audio_manager.gd").read_text()
