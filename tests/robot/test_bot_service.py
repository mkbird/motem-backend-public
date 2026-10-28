"""Tests for the bot service helpers (build_play_url / format_error /
wait_for_bundle). Gating-block formatting is covered in test_bot_gating.py."""

from __future__ import annotations

import asyncio
import json

from robot.bot import service
from robot.games import web_export


# ── load_game_card ──

def _write_meta(tmp_path, monkeypatch, game_id, meta):
    monkeypatch.setattr(web_export, "GAMES_DIR", tmp_path)
    (tmp_path / game_id).mkdir(parents=True)
    (tmp_path / game_id / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_load_game_card_i18n(tmp_path, monkeypatch):
    _write_meta(tmp_path, monkeypatch, "ai_1_abcdef", {
        "title_i18n": {"en": "Catch", "zh-Hans": "接香蕉"},
        "description_i18n": {"en": "Catch bananas", "zh-Hans": "接住香蕉"},
        "icon": "🐵",
    })
    zh = service.load_game_card("ai_1_abcdef", zh=True)
    assert zh == {"title": "接香蕉", "description": "接住香蕉", "icon": "🐵"}
    en = service.load_game_card("ai_1_abcdef", zh=False)
    assert en["title"] == "Catch" and en["description"] == "Catch bananas"


def test_load_game_card_legacy_fallback(tmp_path, monkeypatch):
    _write_meta(tmp_path, monkeypatch, "ai_2_bbbbbb", {"title": "Old", "description": "legacy"})
    card = service.load_game_card("ai_2_bbbbbb", zh=True)
    assert card["title"] == "Old" and card["description"] == "legacy" and card["icon"] == "🎮"


def test_load_game_card_missing_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "GAMES_DIR", tmp_path)
    card = service.load_game_card("ai_3_cccccc", zh=True)
    assert card == {"title": "ai_3_cccccc", "description": "", "icon": "🎮"}


# ── build_play_url ──

def test_build_play_url_with_base():
    assert (service.build_play_url("ai_1_abcdef", base_url="https://m.example.com")
            == "https://m.example.com/play/ai_1_abcdef/")


def test_build_play_url_strips_trailing_slash():
    assert service.build_play_url("g", base_url="https://x.com/") == "https://x.com/play/g/"


def test_build_play_url_default_fallback():
    assert service.build_play_url("g", base_url="").endswith("/play/g/")


# ── format_error ──

def test_format_error_bilingual():
    assert "boom" in service.format_error("boom", zh=True)
    assert "boom" in service.format_error("boom", zh=False)


# ── wait_for_bundle ──

def test_wait_for_bundle_ready_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "PUBLISH_DIR", tmp_path)
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "index.html").write_text("<html></html>", encoding="utf-8")
    assert asyncio.run(service.wait_for_bundle("g", 0.0)) is True


def test_wait_for_bundle_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "PUBLISH_DIR", tmp_path)
    assert asyncio.run(service.wait_for_bundle("missing", 0.0)) is False


def test_wait_for_bundle_appears_during_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "PUBLISH_DIR", tmp_path)
    (tmp_path / "g").mkdir()

    async def scenario():
        async def writer():
            await asyncio.sleep(0.2)
            (tmp_path / "g" / "index.html").write_text("ok", encoding="utf-8")

        task = asyncio.create_task(writer())
        ready = await service.wait_for_bundle("g", 3.0)
        await task
        return ready

    assert asyncio.run(scenario()) is True
