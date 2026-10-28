"""Tests for widget_tools and the SDK patch.

Focus: 3 things must work
1. _sdk_patch tolerates `rate_limit_event` (and other unknown types) without
   exploding the iterator
2. _sdk_patch leaves real schema-violation errors alone (still raises)
3. widget_tools fail-fast: when AI didn't actually modify the template,
   widget_dir gets cleaned and we don't ship a bogus "success"
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Importing this triggers the patch automatically (apply() runs at import).
from robot.agent.tools import _sdk_patch  # noqa: F401
from robot.agent.tools.widget_tools import (
    TEMPLATE_DIR,
    WIDGETS_DIR,
    _coerce_i18n_map,
    _coerce_sizes,
    _compute_diffs,
    _compute_file_manifest,
    _extract_description_i18n_from_result,
    _extract_supported_sizes_from_result,
    _generate_widget_id,
)


# ─────────────────────────────────────────────────────────────────────
# 1. SDK patch — unknown event tolerance
# ─────────────────────────────────────────────────────────────────────


class TestSDKPatchUnknownEventTolerance:
    """Make sure `rate_limit_event` (and anything else not in SDK's switch)
    no longer kills the stream."""

    def test_rate_limit_event_returns_system_message(self):
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk.types import SystemMessage

        msg = parse_message({"type": "rate_limit_event", "tier": "default", "remaining": 0})
        assert isinstance(msg, SystemMessage)
        assert msg.subtype == "unknown_rate_limit_event"
        # Original payload preserved for downstream logging if needed.
        assert msg.data["tier"] == "default"

    def test_completely_unseen_type_also_swallowed(self):
        """OpenRouter / future proxies might invent more events. Patch must
        be generic, not a hardcoded `rate_limit_event` whitelist."""
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk.types import SystemMessage

        msg = parse_message({"type": "future_event_2030", "anything": "goes"})
        assert isinstance(msg, SystemMessage)
        assert msg.subtype == "unknown_future_event_2030"

    def test_missing_type_field_still_raises(self):
        """Real schema violations (no `type` at all) must still bubble up
        — silencing them would mask actual SDK bugs."""
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk._errors import MessageParseError

        with pytest.raises(MessageParseError):
            parse_message({"no_type_here": True})

    def test_invalid_data_shape_still_raises(self):
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk._errors import MessageParseError

        with pytest.raises(MessageParseError):
            parse_message("not a dict at all")

    def test_known_message_types_unaffected(self):
        """Patch must not touch valid message paths — system / result / etc.
        all keep parsing as before."""
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk.types import SystemMessage, ResultMessage

        sys_msg = parse_message({"type": "system", "subtype": "init", "session_id": "s1"})
        assert isinstance(sys_msg, SystemMessage)
        assert sys_msg.subtype == "init"  # NOT prefixed with `unknown_`

        res = parse_message({
            "type": "result",
            "subtype": "success",
            "duration_ms": 100,
            "duration_api_ms": 80,
            "is_error": False,
            "num_turns": 3,
            "session_id": "s1",
        })
        assert isinstance(res, ResultMessage)
        assert res.num_turns == 3

    def test_assistant_text_block_unaffected(self):
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk.types import AssistantMessage, TextBlock

        msg = parse_message({
            "type": "assistant",
            "message": {
                "model": "qwen/qwen3-coder-next",
                "content": [{"type": "text", "text": "hi"}],
            },
        })
        assert isinstance(msg, AssistantMessage)
        assert isinstance(msg.content[0], TextBlock)
        assert msg.content[0].text == "hi"

    def test_apply_is_idempotent(self):
        """Calling apply() twice should not double-wrap (which would make
        every later parse run two layers of try/except)."""
        from claude_code_sdk._internal import message_parser as mp

        before = mp.parse_message
        _sdk_patch.apply()
        after = mp.parse_message
        assert before is after, "second apply() should be a no-op"


# ─────────────────────────────────────────────────────────────────────
# 2. widget_tools utility helpers — small but worth fencing
# ─────────────────────────────────────────────────────────────────────


class TestGenerateWidgetId:
    def test_format(self):
        wid = _generate_widget_id("Lakers Score")
        # aw_<unix-seconds>_<6 hex>
        parts = wid.split("_")
        assert parts[0] == "aw"
        assert len(parts) == 3
        assert parts[1].isdigit()
        assert len(parts[2]) == 6

    def test_unique(self):
        a = _generate_widget_id("foo")
        b = _generate_widget_id("bar")
        assert a != b


class TestCoerceI18nMap:
    def test_uses_dict_when_valid(self):
        out = _coerce_i18n_map({"en": "Clock", "zh-Hans": "时钟"}, fallback="x")
        assert out == {"en": "Clock", "zh-Hans": "时钟"}

    def test_falls_back_when_partial(self):
        out = _coerce_i18n_map({"en": "Clock"}, fallback="x")
        assert out["en"] == "Clock"
        # zh-Hans missing → fallback
        assert out["zh-Hans"] == "x"

    def test_falls_back_when_none(self):
        out = _coerce_i18n_map(None, fallback="default")
        assert out == {"en": "default", "zh-Hans": "default"}

    def test_truncates_long_text(self):
        long = "x" * 500
        out = _coerce_i18n_map({"en": long, "zh-Hans": long}, fallback="y")
        # _MAX_DESCRIPTION_LEN = 100
        assert len(out["en"]) <= 100
        assert len(out["zh-Hans"]) <= 100


class TestCoerceSizes:
    def test_keeps_valid(self):
        assert _coerce_sizes(["medium", "large"]) == ["medium", "large"]

    def test_drops_unknown(self):
        assert _coerce_sizes(["medium", "huge", "xs"]) == ["medium"]

    def test_dedupes(self):
        assert _coerce_sizes(["small", "small", "large"]) == ["small", "large"]

    def test_returns_default_on_invalid(self):
        assert _coerce_sizes(None) == ["medium", "large"]
        assert _coerce_sizes("not a list") == ["medium", "large"]
        assert _coerce_sizes([]) == ["medium", "large"]
        assert _coerce_sizes(["unknown_only"]) == ["medium", "large"]


class TestExtractDescriptionI18n:
    def test_parses_valid_line(self):
        text = """
        some chatter
        description_i18n: {"en": "Clock widget", "zh-Hans": "时钟小组件"}
        more chatter
        """
        out = _extract_description_i18n_from_result(text)
        assert out == {"en": "Clock widget", "zh-Hans": "时钟小组件"}

    def test_returns_none_when_missing(self):
        assert _extract_description_i18n_from_result("no marker here") is None

    def test_returns_none_when_malformed(self):
        text = "description_i18n: not json"
        assert _extract_description_i18n_from_result(text) is None

    def test_returns_none_when_only_one_lang(self):
        text = 'description_i18n: {"en": "only English"}'
        # Both en and zh required for non-None.
        assert _extract_description_i18n_from_result(text) is None


class TestExtractSupportedSizes:
    def test_parses(self):
        text = 'supported_sizes: ["small", "medium"]'
        assert _extract_supported_sizes_from_result(text) == ["small", "medium"]

    def test_filters_invalid(self):
        text = 'supported_sizes: ["medium", "garbage"]'
        assert _extract_supported_sizes_from_result(text) == ["medium"]

    def test_returns_none_when_missing(self):
        assert _extract_supported_sizes_from_result("nothing") is None


# ─────────────────────────────────────────────────────────────────────
# 3. File manifest + diff
# ─────────────────────────────────────────────────────────────────────


class TestComputeFileManifest:
    def test_reads_text_files(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "app.js").write_text("console.log(1)", encoding="utf-8")
        (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")  # ignored

        m = _compute_file_manifest(tmp_path)
        assert "index.html" in m
        assert "app.js" in m
        assert "binary.bin" not in m  # not a tracked text ext

    def test_skips_claude_md(self, tmp_path):
        """CLAUDE.md is dev guidance; must NOT ship to client."""
        (tmp_path / "CLAUDE.md").write_text("dev guide", encoding="utf-8")
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")

        m = _compute_file_manifest(tmp_path)
        assert "CLAUDE.md" not in m
        assert "index.html" in m

    def test_recurses_into_assets(self, tmp_path):
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "base.css").write_text("body{}", encoding="utf-8")
        m = _compute_file_manifest(tmp_path)
        assert "assets/base.css" in m


class TestComputeDiffs:
    def test_add(self):
        diffs = _compute_diffs({}, {"index.html": "x"})
        assert diffs == [{"op": "add", "path": "index.html", "content": "x"}]

    def test_modify(self):
        diffs = _compute_diffs({"app.js": "old"}, {"app.js": "new"})
        assert diffs == [{"op": "modify", "path": "app.js", "content": "new"}]

    def test_delete(self):
        diffs = _compute_diffs({"old.js": "x"}, {})
        assert diffs == [{"op": "delete", "path": "old.js"}]

    def test_no_change(self):
        diffs = _compute_diffs({"a": "1"}, {"a": "1"})
        assert diffs == []

    def test_sorted_paths(self):
        old = {}
        new = {"z.html": "z", "a.html": "a", "m.html": "m"}
        diffs = _compute_diffs(old, new)
        paths = [d["path"] for d in diffs]
        assert paths == sorted(paths)


# ─────────────────────────────────────────────────────────────────────
# 4. Fail-fast detection — template untouched ⇒ scrapped, not shipped
# ─────────────────────────────────────────────────────────────────────


class TestTemplateIsValid:
    """Sanity: the on-disk _template/ ships the bytes we expect to compare
    against. If someone changes the template files, these tests anchor the
    expectation."""

    def test_template_dir_exists(self):
        assert TEMPLATE_DIR.is_dir()

    def test_template_index_html_exists(self):
        assert (TEMPLATE_DIR / "index.html").exists()

    def test_template_app_js_exists(self):
        assert (TEMPLATE_DIR / "app.js").exists()

    def test_template_meta_json_supported_sizes_present(self):
        import json
        meta = json.loads((TEMPLATE_DIR / "meta.json").read_text(encoding="utf-8"))
        assert "supported_sizes" in meta
        assert set(meta["supported_sizes"]).issubset({"small", "medium", "large", "xlarge"})


@pytest_asyncio.fixture
async def fake_widget_dir(tmp_path, monkeypatch):
    """Redirect WIDGETS_DIR → tmp so test runs don't touch real data."""
    monkeypatch.setattr("robot.agent.tools.widget_tools.WIDGETS_DIR", tmp_path)
    return tmp_path


class TestFailFastTemplateUnchanged:
    """If `_run_widget_code_agent` returns without modifying any file (the
    bug we hit with rate_limit_event), `create_widget` MUST:
    1. delete the still-template-only widget_dir
    2. return an error (not pretend success)
    3. NOT send `widget_ready` to iOS
    """

    @pytest.mark.asyncio
    async def test_unchanged_template_is_rejected(self, tmp_path, monkeypatch):
        # Fake widgets dir so we can inspect cleanup
        monkeypatch.setattr("robot.agent.tools.widget_tools.WIDGETS_DIR", tmp_path)

        # Stub the SDK call to return WITHOUT writing anything.
        async def stub_agent(*args, **kwargs):
            return ("Widget code generation failed: Unknown message type", "", 0)

        monkeypatch.setattr("robot.agent.tools.widget_tools._run_widget_code_agent", stub_agent)

        # Stub progress + ws_manager noise.
        async def noop_progress(*args, **kwargs):
            return None
        monkeypatch.setattr("robot.agent.tools.widget_tools._send_widget_progress_key", noop_progress)
        monkeypatch.setattr("robot.agent.tools.widget_tools._send_widget_progress", noop_progress)

        from robot.agent.tools import _context as ctx
        # Stub ws_manager so any send_to_user is observable, not blocking.
        sent = []
        async def fake_send(owner_id, user_id, payload):
            sent.append(payload)
        ctx._ws_manager = MagicMock(send_to_user=AsyncMock(side_effect=fake_send))
        ctx._current_owner_id.set("test-owner")
        ctx._current_user_id.set("test-user")

        # Stub upsert so it doesn't try to touch a real DB.
        async def noop_upsert(**kwargs):
            return None
        with patch("robot.widgets.service.upsert_widget_row", new=noop_upsert):
            from robot.agent.tools.widget_tools import create_widget

            result = await create_widget.ainvoke({
                "title": "Test",
                "description": "Test widget",
                "title_i18n": {"en": "Test", "zh-Hans": "测试"},
                "description_i18n": {"en": "T", "zh-Hans": "测"},
                "icon": "🧪",
                "template_widget_id": "",
            })

        # 1. result is the upstream error string (NOT a "...is ready" success)
        assert "ready" not in result.lower(), \
            f"create_widget should not claim success when AI did nothing; got: {result!r}"

        # 2. widget_dir cleaned up — no leftover folders in WIDGETS_DIR
        leftover = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("aw_")]
        assert leftover == [], f"unchanged-template widget_dir should be removed; left: {leftover}"

        # 3. NO widget_ready broadcast — iOS would otherwise silently install a placeholder
        ready_pushes = [p for p in sent
                        if isinstance(p, dict)
                        and p.get("data", {}).get("action") == "widget_ready"]
        assert ready_pushes == [], "widget_ready must NOT be sent for failed generations"


# ─────────────────────────────────────────────────────────────────────
# 4. template_unchanged 检查覆盖 meta.json
#
# 修复前：检查只比 index.html + app.js，忽略 meta.json → AI 只改 meta.json
# 也被当成 "AI 啥都没干" → wipe widget_dir + 报 generation_failed →
# 合法的 "clone 已有 widget + 改个 title" 场景挂掉。
# 修复后：三个文件**都**与 source 字节一致才算 template_unchanged。
# ─────────────────────────────────────────────────────────────────────


class TestTemplateUnchangedIncludesMeta:
    """`template_unchanged` failure detection must include meta.json equality.
    Otherwise the router→template→only-rename case (which is a legit creation
    when the matched template is already what the user wanted) explodes."""

    @staticmethod
    def _setup_fake_template(tmp_path: Path) -> Path:
        """Create a fake _template dir under tmp_path/widgets/_template with
        the three files create_widget cares about."""
        tpl = tmp_path / "_template"
        tpl.mkdir()
        (tpl / "index.html").write_text("<html>template</html>", encoding="utf-8")
        (tpl / "app.js").write_text("// template", encoding="utf-8")
        (tpl / "meta.json").write_text('{"title":"Template","supported_sizes":["small"]}',
                                       encoding="utf-8")
        return tpl

    @staticmethod
    def _stub_common_io(monkeypatch, sent_collector):
        """Stub progress + ws_manager + DB upsert so create_widget doesn't try
        to touch real I/O outside tmp_path."""
        async def noop_progress(*args, **kwargs):
            return None
        monkeypatch.setattr("robot.agent.tools.widget_tools._send_widget_progress_key",
                            noop_progress)
        monkeypatch.setattr("robot.agent.tools.widget_tools._send_widget_progress",
                            noop_progress)

        from robot.agent.tools import _context as ctx
        async def fake_send(owner_id, user_id, payload):
            sent_collector.append(payload)
        ctx._ws_manager = MagicMock(send_to_user=AsyncMock(side_effect=fake_send))
        ctx._current_owner_id.set("test-owner")
        ctx._current_user_id.set("test-user")

    @pytest.mark.asyncio
    async def test_meta_only_change_is_accepted(self, tmp_path, monkeypatch):
        """The fix: AI only edited meta.json (index.html + app.js byte-identical
        to source). MUST NOT be flagged template_unchanged. widget_dir survives;
        widget_ready may or may not fire depending on other code paths, but
        critically widget_dir must not be wiped."""
        monkeypatch.setattr("robot.agent.tools.widget_tools.WIDGETS_DIR", tmp_path)
        tpl = self._setup_fake_template(tmp_path)
        monkeypatch.setattr("robot.agent.tools.widget_tools.TEMPLATE_DIR", tpl)

        async def stub_agent(cwd, prompt, **kw):
            # 模拟 AI 只改 meta.json，html / js 不变
            from pathlib import Path as _P
            meta_p = _P(cwd) / "meta.json"
            meta_p.write_text(
                '{"title":"User Changed","supported_sizes":["small"]}',
                encoding="utf-8",
            )
            return ("done", "fake-sid", 1)
        monkeypatch.setattr("robot.agent.tools.widget_tools._run_widget_code_agent",
                            stub_agent)

        sent = []
        self._stub_common_io(monkeypatch, sent)

        async def noop_upsert(**kwargs):
            return None
        with patch("robot.widgets.service.upsert_widget_row", new=noop_upsert):
            from robot.agent.tools.widget_tools import create_widget
            result = await create_widget.ainvoke({
                "title": "Test",
                "description": "Test widget",
                "title_i18n": {"en": "Test", "zh-Hans": "测试"},
                "description_i18n": {"en": "T", "zh-Hans": "测"},
                "icon": "🧪",
                "template_widget_id": "",
            })

        # widget_dir 应当**仍然存在**（不像 unchanged template 那样被 rmtree）
        leftover = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("aw_")]
        assert len(leftover) == 1, \
            f"meta-only change should NOT wipe widget_dir; got: {leftover}"
        # 也不应有 widget_creation_failed 通知
        failed = [p for p in sent
                  if isinstance(p, dict)
                  and p.get("data", {}).get("action") == "widget_creation_failed"]
        assert failed == [], f"meta-only change must not emit widget_creation_failed; sent: {failed}"
        # result 应该是某种成功消息（不含 "INCOMPLETE" / "失败"）
        assert "incomplete" not in result.lower(), \
            f"create_widget should not report incomplete; got: {result!r}"

    @pytest.mark.asyncio
    async def test_all_three_byte_identical_still_rejected(self, tmp_path, monkeypatch):
        """Regression guard: 当 AI 真的什么都没改（三个文件都和 source 字节一致）
        时，必须保持原有 fail-fast 行为：rmtree widget_dir + 通知失败。"""
        monkeypatch.setattr("robot.agent.tools.widget_tools.WIDGETS_DIR", tmp_path)
        tpl = self._setup_fake_template(tmp_path)
        monkeypatch.setattr("robot.agent.tools.widget_tools.TEMPLATE_DIR", tpl)

        async def stub_agent(cwd, prompt, **kw):
            # 不写任何文件 —— widget_dir 里就是 copytree 完的原始 source 状态
            return ("did nothing", "fake-sid", 1)  # file_count=1 但实际没改
        monkeypatch.setattr("robot.agent.tools.widget_tools._run_widget_code_agent",
                            stub_agent)

        sent = []
        self._stub_common_io(monkeypatch, sent)

        async def noop_upsert(**kwargs):
            return None
        with patch("robot.widgets.service.upsert_widget_row", new=noop_upsert):
            from robot.agent.tools.widget_tools import create_widget
            result = await create_widget.ainvoke({
                "title": "Test",
                "description": "Test widget",
                "title_i18n": {"en": "Test", "zh-Hans": "测试"},
                "description_i18n": {"en": "T", "zh-Hans": "测"},
                "icon": "🧪",
                "template_widget_id": "",
            })

        # widget_dir 应被 wipe（fail-fast 行为保留）
        leftover = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("aw_")]
        assert leftover == [], \
            f"identical-template widget_dir should be removed; left: {leftover}"
        # widget_creation_failed 应发出
        failed = [p for p in sent
                  if isinstance(p, dict)
                  and p.get("data", {}).get("action") == "widget_creation_failed"]
        assert len(failed) == 1, \
            f"expected one widget_creation_failed; sent: {sent}"
        assert failed[0]["data"].get("reason") == "template_unchanged", \
            f"reason should be template_unchanged; got: {failed[0]}"

    @pytest.mark.asyncio
    async def test_index_html_changed_is_accepted(self, tmp_path, monkeypatch):
        """Control case：AI 改 index.html，meta/app 不变 —— 应当成功
        （这条以前修复前后行为都对，作为对照保证 fix 没破坏正常路径）。"""
        monkeypatch.setattr("robot.agent.tools.widget_tools.WIDGETS_DIR", tmp_path)
        tpl = self._setup_fake_template(tmp_path)
        monkeypatch.setattr("robot.agent.tools.widget_tools.TEMPLATE_DIR", tpl)

        async def stub_agent(cwd, prompt, **kw):
            from pathlib import Path as _P
            idx = _P(cwd) / "index.html"
            idx.write_text("<html>changed</html>", encoding="utf-8")
            return ("done", "fake-sid", 1)
        monkeypatch.setattr("robot.agent.tools.widget_tools._run_widget_code_agent",
                            stub_agent)

        sent = []
        self._stub_common_io(monkeypatch, sent)

        async def noop_upsert(**kwargs):
            return None
        with patch("robot.widgets.service.upsert_widget_row", new=noop_upsert):
            from robot.agent.tools.widget_tools import create_widget
            await create_widget.ainvoke({
                "title": "Test",
                "description": "Test widget",
                "title_i18n": {"en": "Test", "zh-Hans": "测试"},
                "description_i18n": {"en": "T", "zh-Hans": "测"},
                "icon": "🧪",
                "template_widget_id": "",
            })

        leftover = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("aw_")]
        assert len(leftover) == 1, "index.html change should be accepted"
        failed = [p for p in sent
                  if isinstance(p, dict)
                  and p.get("data", {}).get("action") == "widget_creation_failed"]
        assert failed == []


# ─────────────────────────────────────────────────────────────────────
# 5. Token-diet + session-isolation 源码级 regression guard
#
# 这些行如果被人 revert 掉，input tokens 会回到 60K 量级（实测）。
# 用 source-grep 保护比较脆但 cheap，单元级测 SDK options 闭包构造
# 需要 ClaudeCodeOptions stub，性价比不高。
# ─────────────────────────────────────────────────────────────────────


class TestTokenDietAndSessionWiring:
    """Guard against accidental revert of:
    - extra_args["bare"]
    - extra_args["tools"]   (whitelist)
    - env["CLAUDE_CONFIG_DIR"]   (per-widget session isolation)
    - sliding-window compaction wiring (compact_session import + invocation)
    """

    @pytest.fixture
    def source(self) -> str:
        from robot.agent.tools import widget_tools
        return Path(widget_tools.__file__).read_text(encoding="utf-8")

    def test_bare_extra_arg_present(self, source):
        """`--bare` 关 SDK 默认 CLAUDE.md auto-discovery / hooks / skills 注入；
        丢了它 messages[0] 回涨 ~55KB。"""
        assert '"bare": None' in source, \
            "extra_args must include {'bare': None} to disable OMC framework noise"

    def test_tools_whitelist_present(self, source):
        """`--tools` 限制 schema 注入集合，砍 82 个内置 tool schema 到 5 个。"""
        assert '"tools": "Write,Edit,Read,Glob,Grep"' in source, \
            "extra_args must restrict tool schemas to the 5 widgets actually use"

    def test_per_widget_config_dir_env(self, source):
        """`CLAUDE_CONFIG_DIR` env 把 session jsonl 落到 <widget_dir>.claude/
        而不是 user-global ~/.claude/projects/，让 remix / delete 自包含。"""
        assert 'CLAUDE_CONFIG_DIR' in source, \
            "session must be stored per-widget (mirrors game_tools.py:819)"
        assert 'per_widget_dir' in source or '_per_widget_config_dir' in source, \
            "per-widget config dir helper must be wired"

    def test_compaction_helpers_imported(self, source):
        """sliding-window compaction 必须接上 — 否则 resume 路径在多次 modify
        后 jsonl 无限增长，input tokens 爆炸。"""
        assert 'compact_session' in source, \
            "compact_session must be imported and called"
        assert 'flush_active_to_archive' in source, \
            "cliff fallback (flush_active_to_archive) must be wired"

    def test_compaction_thresholds_widget_tuned(self, source):
        """widget 阈值应小于 game (50K trigger / 25K target) 一档，因为
        widget 文件小、增长慢；用 game 的阈值实际从不触发。"""
        assert 'WIDGET_SESSION_TRIM_TRIGGER_BYTES = 30_000' in source, \
            "widget compact trigger should be 30KB (smaller than game's 50KB)"
        assert 'WIDGET_SESSION_TRIM_TARGET_BYTES = 15_000' in source, \
            "widget compact target should be 15KB (smaller than game's 25KB)"


# ─────────────────────────────────────────────────────────────────────
# 6. Sliding-window compaction 在 widget_dir 上工作正确
#
# compact_session 是 cwd-参数化的（虽然函数名带 per_game_），传 widget_dir
# 应当能正常找到 jsonl、裁剪、归档。这条 smoke test 确认导入和路径都对。
# 函数算法本身的覆盖见 test_game_session_inherit.py。
# ─────────────────────────────────────────────────────────────────────


def _make_synthetic_jsonl(target_path: Path, n_prompts: int = 5, msg_padding: int = 500):
    """复制自 test_game_session_inherit.py 的同名 helper，造仿真 SDK jsonl。"""
    import json as _json
    import uuid as _uuid
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    last_uuid = None
    pad = "x" * msg_padding
    for p in range(n_prompts):
        pid = str(_uuid.uuid4())
        u_uid = str(_uuid.uuid4())
        lines.append(_json.dumps({
            "type": "user",
            "parentUuid": last_uuid,
            "promptId": pid,
            "uuid": u_uid,
            "message": {"role": "user", "content": f"prompt {p} {pad}"},
        }, ensure_ascii=False) + "\n")
        last_uuid = u_uid
        for t in range(3):
            a_uid = str(_uuid.uuid4())
            lines.append(_json.dumps({
                "type": "assistant",
                "parentUuid": last_uuid,
                "uuid": a_uid,
                "message": {"role": "assistant", "content": f"reply {p}.{t} {pad}"},
            }, ensure_ascii=False) + "\n")
            last_uuid = a_uid
    target_path.write_text("".join(lines), encoding="utf-8")


def test_compact_session_works_on_widget_cwd(tmp_path):
    """smoke: 把 helper 当 widget cwd 用 —— per_game_session_jsonl 找到 jsonl，
    compact_session 成功裁剪。证明 widget 复用 game session 工具集是安全的。"""
    from robot.games._session_paths import (
        per_game_session_jsonl,
        session_archive_jsonl,
        compact_session,
    )
    # 模拟一个 widget_dir（名字带 aw_，明确不是 game）
    widget_dir = tmp_path / "aw_test_compact"
    widget_dir.mkdir()
    sid = "widget-sid-1"

    active = per_game_session_jsonl(widget_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=8, msg_padding=400)

    before = active.stat().st_size
    assert before > 10_000

    # 用 widget 默认阈值
    ok = compact_session(widget_dir, sid, target_bytes=15_000, min_prompts_kept=1)
    assert ok is True, "compact_session should succeed on widget_dir layout"

    after = active.stat().st_size
    archive = session_archive_jsonl(widget_dir, sid).stat().st_size
    assert after < before, "active should shrink"
    assert archive > 0, "archive should grow"
    # 字节守恒（允许 parentUuid rewrite 的小差异）
    assert abs((after + archive) - before) < 100
