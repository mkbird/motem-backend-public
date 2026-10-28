"""Tests for robot/bot/tunnel.py (ngrok public-URL auto-discovery)."""

from __future__ import annotations

import io
import json

import robot.config as cfg
from robot.bot import tunnel


def _fake_urlopen(payload):
    class _CM:
        def __enter__(self):
            return io.BytesIO(json.dumps(payload).encode())

        def __exit__(self, *a):
            return False

    return lambda *a, **k: _CM()


def test_discover_prefers_backend_port(monkeypatch):
    payload = {"tunnels": [
        {"public_url": "https://other.ngrok-free.dev", "config": {"addr": "localhost:9999"}},
        {"public_url": "https://ours.ngrok-free.dev", "config": {"addr": "localhost:8000"}},
    ]}
    monkeypatch.setattr(tunnel, "urlopen", _fake_urlopen(payload))
    assert tunnel.discover_ngrok_url(8000) == "https://ours.ngrok-free.dev"


def test_discover_falls_back_to_any_https(monkeypatch):
    payload = {"tunnels": [
        {"public_url": "http://insecure.ngrok-free.dev", "config": {"addr": "localhost:8000"}},
        {"public_url": "https://any.ngrok-free.dev", "config": {"addr": "localhost:7000"}},
    ]}
    monkeypatch.setattr(tunnel, "urlopen", _fake_urlopen(payload))
    assert tunnel.discover_ngrok_url(8000) == "https://any.ngrok-free.dev"


def test_discover_empty_when_ngrok_down(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(tunnel, "urlopen", boom)
    assert tunnel.discover_ngrok_url() == ""


def test_public_base_prefers_configured(monkeypatch):
    monkeypatch.setattr(cfg.settings, "share_public_base_url", "https://fixed.example.com", raising=False)
    # configured value wins without touching ngrok
    monkeypatch.setattr(tunnel, "discover_ngrok_url", lambda *a, **k: "https://SHOULD_NOT_USE")
    assert tunnel.discover_public_base_url() == "https://fixed.example.com"


def test_public_base_autodiscovers_when_unset(monkeypatch):
    monkeypatch.setattr(cfg.settings, "share_public_base_url", "", raising=False)
    monkeypatch.setattr(tunnel, "discover_ngrok_url", lambda *a, **k: "https://auto.ngrok-free.dev")
    assert tunnel.discover_public_base_url() == "https://auto.ngrok-free.dev"
