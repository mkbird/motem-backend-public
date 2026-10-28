"""Coverage for the single-source-of-truth game rules introduced in the
unified-game-rules refactor. The same `_template_slowpath/CLAUDE.md`
file backs:

- backend Claude Code SDK system prompt construction
  (`_build_skeleton_system_prompt`)
- per-game CLAUDE.md written on slowpath create
- `GET /api/games/template-bundle` payload consumed by macOS Motem Studio

These tests pin the invariants that make that sharing safe: the
`{audio_section}` placeholder is always rendered before reaching a
consumer, the audio manifest's mtime feeds the bundle ETag, and the
system prompt stays thin (rules content lives in the file, not in the
opener)."""

from __future__ import annotations

import os
import tarfile
import time
import io
import json
import shutil
from pathlib import Path

import pytest

from httpx import AsyncClient

from robot.agent.tools.game_tools import (
    _build_skeleton_system_prompt,
    _render_slowpath_claude_md,
    _SLOWPATH_CLAUDE_MD_PATH,
)
from robot.games import router as games_router


class TestRenderSlowpathClaudeMd:
    """`_render_slowpath_claude_md` is the single chokepoint that
    substitutes the live audio catalogue into the file template."""

    def test_returns_non_empty_when_template_exists(self):
        rendered = _render_slowpath_claude_md()
        assert rendered, "template file is missing or empty"
        assert len(rendered) > 500, "rendered content suspiciously short"

    def test_placeholder_is_substituted(self):
        rendered = _render_slowpath_claude_md()
        assert "{audio_section}" not in rendered, (
            "audio_section literal leaked through — would confuse the model"
        )

    def test_audio_catalogue_is_embedded(self):
        rendered = _render_slowpath_claude_md()
        assert "## Audio" in rendered, (
            "rendered CLAUDE.md missing audio catalogue heading"
        )
        assert "AudioManager.play" in rendered, (
            "audio catalogue should mention the playback API"
        )

    def test_keeps_skeleton_hook_architecture(self):
        rendered = _render_slowpath_claude_md()
        # Sanity that we didn't accidentally strip the foundational sections
        # while moving content around.
        for section in (
            "Operating mode: SKELETON HOOKS",
            "Required hooks",
            "Optional override hooks",
            "Runtime environment",
            "Input bridge reference",
            "Coding constraints",
        ):
            assert section in rendered, f"missing section: {section!r}"


class TestBuildSkeletonSystemPrompt:
    """The system prompt is now an opener — full rules live in the
    per-game CLAUDE.md that the caller appends from cwd. Token bloat in
    the opener would defeat the refactor."""

    def test_opener_is_thin(self):
        prompt = _build_skeleton_system_prompt(input_mode="touch")
        assert len(prompt) < 2000, (
            f"opener should stay thin (<2000 chars), got {len(prompt)}"
        )

    def test_opener_mentions_input_mode(self):
        touch = _build_skeleton_system_prompt(input_mode="touch")
        body = _build_skeleton_system_prompt(input_mode="body_pose")
        assert "touch" in touch
        assert "body_pose" in body

    def test_opener_points_to_cwd_claude_md(self):
        prompt = _build_skeleton_system_prompt(input_mode="touch")
        # The caller appends per-game CLAUDE.md (cwd) — the opener must
        # tell the model to treat that as the spec rather than fabricate
        # rules from training data.
        assert "CLAUDE.md" in prompt
        assert "authoritative" in prompt.lower() or "spec" in prompt.lower()

    def test_opener_warns_against_rewriting_main_gd(self):
        prompt = _build_skeleton_system_prompt(input_mode="touch")
        # Strong prior is the #1 way skeleton mode regresses — keep the
        # warning prominent.
        assert "scratch" in prompt.lower(), (
            "missing 'do not rewrite from scratch' warning"
        )

    def test_opener_does_not_inline_rules(self):
        prompt = _build_skeleton_system_prompt(input_mode="touch")
        # These headings live in CLAUDE.md; if they appear in the opener
        # we'd be double-shipping content (token bloat).
        for inline in ("## Audio", "## Required hooks", "## Coding constraints"):
            assert inline not in prompt, f"rule heading leaked into opener: {inline!r}"


@pytest.mark.asyncio
class TestTemplateBundleRendering:
    """The bundle served to Motem Studio must contain the rendered
    CLAUDE.md so the macOS-side claude sees the live audio catalogue
    without re-implementing the renderer."""

    async def test_bundle_claude_md_has_no_literal_placeholder(self, client: AsyncClient):
        resp = await client.get("/api/games/template-bundle")
        assert resp.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            md = tar.extractfile("_template_slowpath/CLAUDE.md").read().decode("utf-8")
        assert "{audio_section}" not in md, (
            "Motem Studio would ship a literal placeholder to the local claude"
        )
        assert "## Audio" in md, "audio catalogue missing from bundle"

    async def test_audio_manifest_change_invalidates_etag(
        self, client: AsyncClient, monkeypatch
    ):
        # Snapshot current etag.
        resp1 = await client.get("/api/games/template-bundle")
        etag1 = resp1.headers["etag"]

        # Touch the audio manifest (bump mtime by 2s) and confirm a fresh
        # etag is computed. Restore mtime in tearDown to keep the test
        # idempotent on repeated runs.
        manifest = games_router.AUDIO_MANIFEST
        if not manifest.is_file():
            pytest.skip("audio manifest missing on this environment")
        orig_mtime = manifest.stat().st_mtime
        try:
            future = orig_mtime + 2
            os.utime(manifest, (future, future))
            resp2 = await client.get("/api/games/template-bundle")
            etag2 = resp2.headers["etag"]
            assert etag1 != etag2, (
                "audio manifest mtime change should propagate into the bundle etag"
            )
        finally:
            os.utime(manifest, (orig_mtime, orig_mtime))


class TestPerGameClaudeMdIsRendered:
    """Slowpath create copies the template into the game dir, then
    overwrites CLAUDE.md with the rendered version so modify-time the
    agent reads literal audio IDs (the modify flow doesn't re-render)."""

    def test_overwrite_keeps_rendered_content(self, tmp_path):
        # Mirror the production layout: copytree the slowpath dir, then
        # apply the same overwrite the production code path applies.
        from robot.agent.tools.game_tools import _render_slowpath_claude_md

        game_dir = tmp_path / "ai_test_game"
        slowpath = _SLOWPATH_CLAUDE_MD_PATH.parent
        shutil.copytree(slowpath, game_dir, dirs_exist_ok=True)
        rendered = _render_slowpath_claude_md()
        (game_dir / "CLAUDE.md").write_text(rendered, encoding="utf-8")

        body = (game_dir / "CLAUDE.md").read_text(encoding="utf-8")
        assert "{audio_section}" not in body
        assert "## Audio" in body
