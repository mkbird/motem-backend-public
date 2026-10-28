"""Task tools — add_task, get_tasks."""

import logging
from datetime import datetime

from langchain_core.tools import tool

from robot.agent.tools import _context as ctx
from robot.i18n import t as _t

logger = logging.getLogger(__name__)


@tool
async def add_task(
    title: str,
    assigned_to_identity: str = "",
    due_date: str = "",
    priority: int = 3,
) -> str:
    """创建任务。
    assigned_to_identity: 分配给谁，如 "爸爸"。留空为不分配。
    due_date 格式: "2024-03-21" 或留空
    priority: 1-5, 5最高
    """
    from robot.tasks.service import create_task

    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] add_task: title=%r due=%s priority=%d owner=%s user=%s", title, due_date, priority, owner_id, user_id)

    assigned_to = user_id

    due = None
    if due_date:
        try:
            due = ctx._local_to_utc(datetime.strptime(due_date, "%Y-%m-%d"))
        except ValueError:
            pass

    async with ctx._db_session_factory() as db:
        await create_task(
            db,
            owner_id=owner_id,
            user_id=user_id,
            title=title,
            assigned_to=assigned_to,
            due_date=due,
            priority=priority,
        )
        if ctx._ws_manager:
            await ctx._ws_manager.broadcast_to_group(
                owner_id,
                {"type": "task_sync", "data": {"action": "added", "title": title}},
            )
        assign_suffix = _t(lang, "task_assigned_suffix", who=assigned_to_identity) if assigned_to_identity else ""
        return _t(lang, "task_created", title=title, suffix=assign_suffix)


@tool
async def get_tasks(assigned_to_identity: str = "") -> str:
    """获取家庭的全部任务列表。
    assigned_to_identity: 仅当用户明确要求查看某个特定成员的任务时才传入该成员身份（如"爸爸"）。
    默认留空，返回家庭的全部任务。当用户说"我的待办""查待办""有什么任务"时，请留空获取全部。
    """
    from robot.tasks.service import list_tasks

    owner_id = ctx._current_owner_id.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] get_tasks: assigned=%s owner=%s", assigned_to_identity, owner_id)

    # Always fetch all tasks first
    async with ctx._db_session_factory() as db:
        tasks = await list_tasks(db, owner_id)
        if not tasks:
            return _t(lang, "task_none")
        result = []
        for t in tasks:
            status_emoji = "✅" if t.status.value == "completed" else "⏳"
            priority_str = _t(lang, "task_priority", p=t.priority)
            line = f"- {status_emoji} {t.title}{priority_str}"
            if t.due_date:
                line += _t(lang, "task_due", date=t.due_date.strftime('%m-%d'))
            if t.assigned_to:
                line += _t(lang, "task_assigned_marker")
            result.append(line)
        return _t(lang, "task_list_header") + "\n".join(result)
