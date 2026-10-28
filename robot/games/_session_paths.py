"""Claude Code SDK session path helpers.

The CLI stores sessions under ``<CLAUDE_CONFIG_DIR>/projects/<slug>/<uuid>.jsonl``.
The slug is derived from the cwd by replacing every non-alphanumeric char
with ``-``. Empirically verified against ``~/.claude/projects/`` on 30 sample
project dirs (all conform).

We point ``CLAUDE_CONFIG_DIR`` at a **sibling** directory of each game,
``<games_root>/<game_id>.claude/``, NOT at ``<game_dir>/.claude/`` — the game
directory itself is packaged into a ``.zip`` and shipped to iOS clients;
shipping the LLM conversation jsonl there would leak the original creator's
prompts / tool calls / thinking blocks to anyone who downloads the game,
and bloat the zip with hundreds of KB of irrelevant data.

Layout::

    <games_root>/
    ├── ai_xxx/                ← Godot project (zipped → iOS)
    ├── ai_xxx.zip             ← shipped artifact
    ├── ai_xxx.claude/         ← session storage (server-only)
    │   └── projects/<encoded-cwd>/<sid>.jsonl

Clone/remix copies the sibling ``.claude`` dir explicitly (since
``shutil.copytree`` of the game_dir itself doesn't reach siblings); delete
removes the sibling alongside the game_dir.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def cwd_to_project_slug(cwd: str | Path) -> str:
    """Mirror Claude Code CLI's project-dir naming.

    Any character that is not ``[A-Za-z0-9]`` becomes ``-``.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def per_game_config_dir(game_dir: str | Path) -> Path:
    """Sibling to ``game_dir`` (NOT inside it).

    Returns ``<game_dir.parent>/<game_dir.name>.claude``.
    """
    gd = Path(game_dir)
    return gd.parent / f"{gd.name}.claude"


def per_game_session_jsonl(game_dir: str | Path, session_id: str) -> Path:
    """Where the SDK / CLI will look for a session jsonl when
    ``CLAUDE_CONFIG_DIR`` is per-game and ``cwd == game_dir``.
    """
    gd = Path(game_dir)
    return (
        per_game_config_dir(gd)
        / "projects"
        / cwd_to_project_slug(gd)
        / f"{session_id}.jsonl"
    )


def global_session_jsonl(game_dir: str | Path, session_id: str) -> Path:
    """Where legacy games (created before per-game ``CLAUDE_CONFIG_DIR``)
    stored sessions, in the user's global ``~/.claude/projects/`` tree.

    Used by ``_run_claude_agent`` for one-shot migration of legacy sessions
    into the per-game store.
    """
    return (
        Path("~/.claude/projects").expanduser()
        / cwd_to_project_slug(game_dir)
        / f"{session_id}.jsonl"
    )


def inherit_session_from(
    src_dir: str | Path,
    dst_dir: str | Path,
    expected_sid: str | None = None,
) -> str | None:
    """Copy ``<src_dir>.claude/`` to ``<dst_dir>.claude/`` and rename the
    inner ``projects/<slug>/`` subdir from the source's cwd-slug to the
    destination's cwd-slug, so the SDK can resume the inherited session
    when ``cwd == dst_dir``.

    Used by:

    * **clone_game (remix)** — pass ``expected_sid=source.session_id`` to
      verify the bundled jsonl matches DB state.
    * **create_game (template clone)** — pass ``expected_sid=None`` to
      auto-discover the bundled session id (templates ship with a
      pre-built jsonl whose sid was decided by the platform author at
      template-build time, not stored in DB anywhere).

    Returns the inherited session_id on success, or ``None`` when:

    * src has no sibling ``.claude/`` dir (no bundled session)
    * src's ``projects/`` dir is empty / ambiguous
    * ``expected_sid`` provided but its jsonl is missing
    * any IO error during copy/rename

    Failure is non-fatal: the calling path should fall back to fresh session.

    The helper handles the cross-machine case for templates:
    when the bundled ``projects/<author-slug>/`` was written on a different
    machine, its slug doesn't match ``cwd_to_project_slug(src_dir)`` on
    prod. We auto-discover by ``iterdir()`` when the deterministic lookup
    fails — assumes exactly one project subdir, which holds for our
    pipeline (each game / template only ever runs in one cwd).
    """
    src = Path(src_dir)
    dst = Path(dst_dir)
    src_claude = per_game_config_dir(src)
    if not src_claude.is_dir():
        return None

    src_proj_root = src_claude / "projects"
    if not src_proj_root.is_dir():
        return None

    # Prefer the deterministic slug (same-machine case: clone_game on prod).
    # Fall back to iterdir auto-discovery (cross-machine case: bundled template).
    src_proj = src_proj_root / cwd_to_project_slug(src)
    if not src_proj.is_dir():
        subdirs = [d for d in src_proj_root.iterdir() if d.is_dir()]
        if len(subdirs) != 1:
            return None
        src_proj = subdirs[0]

    if expected_sid:
        if not (src_proj / f"{expected_sid}.jsonl").is_file():
            return None
        sid = expected_sid
    else:
        jsonls = [p for p in src_proj.glob("*.jsonl") if p.is_file()]
        if len(jsonls) != 1:
            return None
        sid = jsonls[0].stem

    dst_claude = per_game_config_dir(dst)
    try:
        if dst_claude.exists():
            shutil.rmtree(dst_claude, ignore_errors=True)
        shutil.copytree(src_claude, dst_claude)
        copied_proj = dst_claude / "projects" / src_proj.name
        renamed_proj = dst_claude / "projects" / cwd_to_project_slug(dst)
        if copied_proj.is_dir() and copied_proj != renamed_proj:
            renamed_proj.parent.mkdir(parents=True, exist_ok=True)
            copied_proj.rename(renamed_proj)
        elif not copied_proj.is_dir() and not renamed_proj.is_dir():
            # 复制后子目录不见了——异常状态，清干净降级
            logger.warning(
                "[Games] inherit_session_from: inner project subdir missing "
                "after copy (%s)", copied_proj,
            )
            shutil.rmtree(dst_claude, ignore_errors=True)
            return None
        return sid
    except OSError as e:
        logger.warning(
            "[Games] inherit_session_from failed (%s); falling back to fresh", e,
        )
        if dst_claude.exists():
            shutil.rmtree(dst_claude, ignore_errors=True)
        return None


def session_archive_jsonl(game_dir: str | Path, session_id: str) -> Path:
    """Single append-only archive file holding all turns ever cut from active.

    Layout: ``<per_game_config_dir>/archive/<session_id>.archive.jsonl``.

    Used by :func:`compact_session` to preserve content trimmed off the
    active jsonl, so the full history is always recoverable as
    ``cat archive.jsonl active.jsonl`` (with a one-line parentUuid fix-up).
    """
    return per_game_config_dir(game_dir) / "archive" / f"{session_id}.archive.jsonl"


def count_prompt_boundaries(game_dir: str | Path, session_id: str) -> int:
    """Count unique ``promptId`` first-appearances in the active session jsonl.

    Each ``query()`` SDK call appends turns with a single ``promptId``; multiple
    successive ``query()`` calls (via resume) append to the same active jsonl
    and accumulate boundaries. :func:`compact_session` needs at least
    ``min_prompts_kept + 1`` boundaries to find a safe cut point.

    Callers use this to distinguish two failure modes of compact_session:

    * **transient — boundaries not yet accumulated** (return value ≤
      ``min_prompts_kept``): skip compact, keep ``session_id`` intact, let the
      jsonl grow naturally so future modifies build up cut points. This is the
      normal flow on the FIRST modify after create.

    * **real failure** (boundaries sufficient but ``compact_session`` still
      returned ``False``): IO error, malformed jsonl, or pathological cut
      geometry → fall back to ``flush_active_to_archive`` + cliff (fresh sid).

    Returns 0 when the active jsonl is missing or unreadable.
    """
    active = per_game_session_jsonl(game_dir, session_id)
    if not active.is_file():
        return 0
    try:
        seen: set[str] = set()
        with active.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                pid = d.get("promptId")
                if pid and pid not in seen:
                    seen.add(pid)
        return len(seen)
    except OSError as e:
        logger.warning(
            "[Games] count_prompt_boundaries: read failed %s (%s)", active, e,
        )
        return 0


def compact_session(
    game_dir: str | Path,
    session_id: str,
    target_bytes: int = 80_000,
    min_prompts_kept: int = 1,
) -> bool:
    """Sliding-window trim of the active session jsonl.

    Algorithm:

    1. Walk the active jsonl, find each prompt boundary (a ``user`` message
       whose ``promptId`` first appears — this is a "fresh user instruction"
       boundary, safe to cut at because no tool_use/tool_result chain
       crosses it).
    2. Walking from the latest prompt backwards, find the **earliest** cut
       point such that the kept tail is ``<= target_bytes`` AND keeps at
       least ``min_prompts_kept`` complete prompts.
    3. Append all dropped head lines to ``session_archive_jsonl``
       (single append-only file — cumulative history). Atomic via
       ``open('a')`` then close.
    4. Rewrite the active jsonl with the kept tail. The new first message
       gets ``parentUuid = None`` so the SDK treats it as a conversation
       root. Atomic via tmp file + ``replace``.

    Returns ``True`` on success, ``False`` when:

    * active jsonl missing
    * fewer than ``min_prompts_kept + 1`` prompts present (nothing to cut)
    * couldn't find a cut point that satisfies both invariants
    * IO / parse error during rewrite (caller should fall back to cliff)

    On failure the active jsonl is **not** mutated; archive is **not**
    written. Idempotent rerun is safe.
    """
    active = per_game_session_jsonl(game_dir, session_id)
    if not active.is_file():
        return False

    try:
        with active.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("[Games] compact_session: read failed %s (%s)", active, e)
        return False

    # 字节级大小（Python str.len 是 char 数，对中文 UTF-8 1→3 字节会低估
    # ~30%；这里预先 encode 一次，后续算大小都用 byte_sizes 索引）
    byte_sizes = [len(l.encode("utf-8")) for l in lines]

    # Find each promptId's first appearance — these are safe cut boundaries.
    seen_pids: set[str] = set()
    prompt_starts: list[int] = []
    for i, line in enumerate(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pid = d.get("promptId")
        if pid and pid not in seen_pids:
            seen_pids.add(pid)
            prompt_starts.append(i)

    # 至少要有 min_prompts_kept + 1 个边界才有"切头" 的余地
    if len(prompt_starts) < min_prompts_kept + 1:
        return False

    # 选切点：从最早的 prompt 边界往后扫，找第一个满足
    # (kept_size <= target_bytes AND n_kept >= min_prompts_kept) 的位置。
    # = 保留尽量多的前提下满足大小目标。
    cut_at: int | None = None
    for i in range(len(prompt_starts)):
        n_kept = len(prompt_starts) - i
        if n_kept < min_prompts_kept:
            break  # 后面都不满足 min_prompts_kept
        kept_size = sum(byte_sizes[prompt_starts[i]:])
        if kept_size <= target_bytes:
            cut_at = prompt_starts[i]
            break

    # 上面循环找不到（target 太小放不下 min_prompts_kept 个 prompt）
    # → 退而求其次：保最小数量的 prompt（哪怕超 target）
    if cut_at is None:
        idx = len(prompt_starts) - min_prompts_kept
        if idx <= 0:
            return False
        cut_at = prompt_starts[idx]

    if cut_at <= 0:
        return False

    head = lines[:cut_at]
    tail = lines[cut_at:]
    if not tail:
        return False

    # Step 1: 追加 head 到 archive（先动 archive，archive 是 source of truth）
    archive = session_archive_jsonl(game_dir, session_id)
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as f:
            f.writelines(head)
    except OSError as e:
        logger.warning("[Games] compact_session: archive append failed (%s)", e)
        return False

    # Step 2: 把 tail 第一行的 parentUuid 改 null（让 SDK 视为新会话起点）
    try:
        first = json.loads(tail[0])
        if "parentUuid" in first:
            first["parentUuid"] = None
            tail[0] = json.dumps(first, ensure_ascii=False) + "\n"
    except Exception as e:
        logger.warning(
            "[Games] compact_session: parentUuid rewrite failed (%s); "
            "active jsonl untouched, archive may now overlap with active",
            e,
        )
        # archive 已经写了 head，但 active 没改 → 下次调用会再次 append head 到 archive，
        # 重叠但不丢数据。重叠由后续重建逻辑通过 uuid 去重处理。
        return False

    # Step 3: 原子写 active（tmp + replace）
    tmp = active.with_suffix(".jsonl.compact.tmp")
    try:
        tmp.write_text("".join(tail), encoding="utf-8")
        tmp.replace(active)
    except OSError as e:
        logger.warning("[Games] compact_session: active rewrite failed (%s)", e)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False

    return True


def flush_active_to_archive(game_dir: str | Path, session_id: str) -> None:
    """Append the entire active jsonl to the archive file.

    Called by callers that are about to discard / reset the active session
    (e.g., cliff-fallback when :func:`compact_session` fails). Ensures the
    archive remains the source of truth even when active is abandoned.

    Idempotent / best-effort. Active is not modified — caller decides what
    to do with it next (delete, reset session_id, etc.).
    """
    active = per_game_session_jsonl(game_dir, session_id)
    if not active.is_file():
        return
    archive = session_archive_jsonl(game_dir, session_id)
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("ab") as out, active.open("rb") as inp:
            shutil.copyfileobj(inp, out)
    except OSError as e:
        logger.warning(
            "[Games] flush_active_to_archive failed (%s); "
            "in-flight active turns may be lost on next reset", e,
        )


def truncate_stale_tool_results(
    game_dir: str | Path,
    session_id: str,
    keep_recent_prompts: int = 3,
    min_content_chars: int = 200,
) -> int:
    """Replace stale Read tool_result content with short summaries.

    motivation
    ----------
    每次 modify 里 ``Read scenes/main.gd`` 返回 800+ 行全文（30-50KB / 次）。
    历史里累积 14 次 Read = 235KB 全是 **过时副本**（文件已经改过 N 次了，
    旧的 Read result model 不会 trust 也不该信）。

    把"老 prompt 里"的 Read tool_result.content 缩成 1 行摘要，能砍掉 jsonl
    里最大的膨胀源（实测 18% 缩减），不影响 tool_use_id ↔ tool_result 配对
    （只改 content 字段），SDK / CLI resume 兼容（沙箱已验证）。

    保留最近 ``keep_recent_prompts`` 个 prompt cycle 完整不动，model 续接最
    近修改时仍有完整 Read 全文可参考；更早的 prompt 里只保留摘要。

    parameters
    ----------
    keep_recent_prompts : int
        最末尾保留 N 个 prompt cycle 的 tool_result 不动。早于此的 Read
        tool_result.content 会被替换。默认 3。
    min_content_chars : int
        小于此长度的 result 不动（已经很短，没必要再缩；避免反复处理同一行）。
        默认 200。

    returns
    -------
    被截断的 tool_result 数量。0 表示文件不存在 / 无可截断 / 全在保留区。

    safety
    ------
    - 仅改 ``user.message.content[].content`` 字段（tool_result 的内容）
    - **不动** uuid / parentUuid / tool_use_id / 其他 metadata
    - 仅针对 Read 工具的 result（Edit / Grep result 短不动；Write / Bash 略）
    - 失败一律退出，active 不动（caller 走 sliding-window compact 兜底）
    """
    active = per_game_session_jsonl(game_dir, session_id)
    if not active.is_file():
        return 0

    try:
        with active.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("[Games] truncate_stale_tool_results: read failed (%s)", e)
        return 0

    # 1. 找所有 prompt cycle 边界（promptId first appearance）
    seen_pids: set[str] = set()
    prompt_starts: list[int] = []
    for i, line in enumerate(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pid = d.get("promptId")
        if pid and pid not in seen_pids:
            seen_pids.add(pid)
            prompt_starts.append(i)

    if len(prompt_starts) <= keep_recent_prompts:
        # 不够 N+1 个 prompt → 全部都在保留区，无可截断
        return 0

    # 截断分界线：保留最后 keep_recent_prompts 个，更早的 prompt 里的 tool_result 截
    # cut_off_idx = 第 keep_recent_prompts 个最早 prompt 的起点行号
    cut_off_line = prompt_starts[-keep_recent_prompts]

    # 2. 扫所有 assistant.tool_use 收集 Read 工具的 tool_use_id
    read_tool_ids: set[str] = set()
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message", {})
        if not isinstance(msg, dict):
            continue
        for c in (msg.get("content", []) if isinstance(msg.get("content"), list) else []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "Read":
                tid = c.get("id")
                if tid:
                    read_tool_ids.add(tid)

    if not read_tool_ids:
        return 0

    # 3. 在 cut_off_line 之前的行里，找含 Read 的 tool_result 替换 content
    truncated_count = 0
    for i in range(cut_off_line):
        line = lines[i]
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        msg = d.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        changed = False
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") != "tool_result":
                continue
            tid = c.get("tool_use_id", "")
            if tid not in read_tool_ids:
                continue
            # 计算原 content 大小
            inner = c.get("content", "")
            if isinstance(inner, list):
                orig_size = sum(
                    len(json.dumps(x, ensure_ascii=False)) for x in inner
                )
            else:
                orig_size = len(str(inner))
            if orig_size < min_content_chars:
                continue  # 已经很短了
            c["content"] = (
                f"[Read result truncated: original {orig_size} chars; "
                f"file may have been modified since this Read. "
                f"Re-Read if you need current state.]"
            )
            changed = True
            truncated_count += 1
        if changed:
            lines[i] = json.dumps(d, ensure_ascii=False) + "\n"

    if truncated_count == 0:
        return 0

    # 4. 原子写
    tmp = active.with_suffix(".jsonl.truncate.tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(active)
    except OSError as e:
        logger.warning("[Games] truncate_stale_tool_results: write failed (%s)", e)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return 0

    return truncated_count


def wipe_game_artifacts(game_dir: str | Path) -> None:
    """Remove everything tied to a game on disk: the game dir itself, its
    zip, and its sibling ``.claude`` session store.

    Idempotent / best-effort — safe to call when some artifacts are absent.
    """
    gd = Path(game_dir)
    zip_path = gd.parent / f"{gd.name}.zip"
    claude_dir = per_game_config_dir(gd)
    for target in (gd, claude_dir):
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    if zip_path.exists():
        try:
            zip_path.unlink()
        except OSError as e:
            logger.warning("[Games] wipe_game_artifacts: failed to unlink %s (%s)", zip_path, e)
