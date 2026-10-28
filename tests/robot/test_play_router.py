"""Tests for the public /play/{game_id}/ web-bundle serving router.

Uses a minimal FastAPI app that includes only play_router, with PUBLISH_DIR
redirected to a tmp dir — avoids the real app's lifespan / DB startup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from robot.games import play_router as pr
from robot.games import web_export


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_export, "PUBLISH_DIR", tmp_path / "web_builds")
    app = FastAPI()
    app.include_router(pr.router)
    return TestClient(app)


def _publish(tmp_path: Path, game_id: str = "ai_play_0001") -> str:
    build = tmp_path / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "index.html").write_text("<html><body>game</body></html>", encoding="utf-8")
    (build / "index.wasm").write_bytes(b"\x00asm")
    (build / "index.pck").write_bytes(b"\x00pck")
    (build / "index.js").write_text("// js", encoding="utf-8")
    (build / "motem_input.js").write_text("// shell", encoding="utf-8")
    web_export.publish_build(game_id, build)
    return game_id


def test_index_served(client, tmp_path):
    gid = _publish(tmp_path)
    r = client.get(f"/play/{gid}/")
    assert r.status_code == 200
    assert "game" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_wasm_content_type(client, tmp_path):
    gid = _publish(tmp_path)
    r = client.get(f"/play/{gid}/index.wasm")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/wasm"


def test_pck_and_js_served(client, tmp_path):
    gid = _publish(tmp_path)
    assert client.get(f"/play/{gid}/index.pck").status_code == 200
    r = client.get(f"/play/{gid}/motem_input.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")


def test_unpublished_game_404(client):
    assert client.get("/play/ai_does_not_exist/").status_code == 404
    assert client.get("/play/ai_does_not_exist/index.wasm").status_code == 404


def test_missing_asset_404(client, tmp_path):
    gid = _publish(tmp_path)
    assert client.get(f"/play/{gid}/nope.bin").status_code == 404


def test_path_traversal_blocked(client, tmp_path, monkeypatch):
    """The resolve guard rejects paths escaping the bundle dir."""
    gid = _publish(tmp_path)
    # plant a secret OUTSIDE the bundle
    secret = (tmp_path / "web_builds" / "secret.txt")
    secret.write_text("top secret", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        pr._resolve_in_bundle(gid, "../secret.txt")
    assert exc.value.status_code == 404
