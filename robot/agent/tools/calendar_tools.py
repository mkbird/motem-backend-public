"""Calendar tools — get_events, add_calendar_event, check_member_availability."""

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from robot.agent.tools import _context as ctx
from robot.i18n import t as _t

logger = logging.getLogger(__name__)


@tool
async def get_events(date: str = "") -> str:
    """获取指定日期的家庭日历事件。
    date: 日期字符串，格式 "YYYY-MM-DD"。留空则默认查今天。
    例如查明天就传明天的日期，查后天就传后天的日期。
    """
    from robot.calendar.service import list_events

    owner_id = ctx._current_owner_id.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] get_events: date=%s owner=%s", date, owner_id)

    local_now = datetime.now(ctx._user_tz())
    if date:
        try:
            target_local = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ctx._user_tz())
        except ValueError:
            return _t(lang, "cal_date_fmt_err")
    else:
        target_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    day_start = target_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    start_utc = ctx._local_to_utc(day_start)
    end_utc = ctx._local_to_utc(day_end)

    is_today = local_now.date() == target_local.date()
    is_tomorrow = (local_now + timedelta(days=1)).date() == target_local.date()
    is_day_after = (local_now + timedelta(days=2)).date() == target_local.date()

    if is_today:
        date_label = _t(lang, "cal_today")
    elif is_tomorrow:
        date_label = _t(lang, "cal_tomorrow")
    elif is_day_after:
        date_label = _t(lang, "cal_day_after")
    else:
        date_label = target_local.strftime("%m-%d")

    async with ctx._db_session_factory() as db:
        events = await list_events(db, owner_id, start=start_utc, end=end_utc)
        if not events:
            return _t(lang, "cal_no_events", label=date_label)
        result = []
        for e in events:
            local_start = e.start_time.replace(tzinfo=timezone.utc).astimezone(ctx._user_tz())
            local_end = e.end_time.replace(tzinfo=timezone.utc).astimezone(ctx._user_tz())
            line = (
                f"- {e.title}（{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}）"
                + (_t(lang, "cal_location", loc=e.location) if e.location else "")
            )
            result.append(line)
        return _t(lang, "cal_events_header", label=date_label) + "\n".join(result)


@tool
async def add_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    participant_identities: str = "",
    requires_confirmation: bool = False,
    location: str = "",
    description: str = "",
) -> str:
    """创建日历事件。
    start_time/end_time 格式: "2024-03-21 14:00"
    participant_identities: 逗号分隔的成员身份称呼，如 "爸爸,妈妈"
    requires_confirmation: 是否需要参与者确认
    """
    from robot.calendar.service import create_event

    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] add_calendar_event: title=%r start=%s end=%s owner=%s user=%s", title, start_time, end_time, owner_id, user_id)

    try:
        start = ctx._local_to_utc(datetime.strptime(start_time, "%Y-%m-%d %H:%M"))
        end = ctx._local_to_utc(datetime.strptime(end_time, "%Y-%m-%d %H:%M"))
    except ValueError:
        return _t(lang, "cal_time_fmt_err")

    participant_user_ids = []

    async with ctx._db_session_factory() as db:
        event = await create_event(
            db,
            owner_id=owner_id,
            user_id=user_id,
            title=title,
            start_time=start,
            end_time=end,
            description=description or None,
            location=location or None,
            participant_user_ids=participant_user_ids,
            requires_confirmation=requires_confirmation,
        )
        if ctx._ws_manager:
            await ctx._ws_manager.broadcast_to_group(
                owner_id,
                {"type": "calendar_sync", "data": {"action": "added", "event_id": event.id}},
            )
        suffix = (
            (_t(lang, "cal_participants", who=participant_identities) if participant_identities else "")
            + (_t(lang, "cal_await_confirm") if requires_confirmation else "")
        )
        return _t(lang, "cal_event_created", title=title, start=start_time, end=end_time, suffix=suffix)


@tool
async def check_member_availability(
    start_time: str, end_time: str
) -> str:
    """查询用户某时间段是否有空。
    start_time/end_time 格式: "2024-03-21 14:00"
    """
    from robot.calendar.service import check_availability

    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] check_member_availability: start=%s end=%s owner=%s", start_time, end_time, owner_id)

    try:
        start = ctx._local_to_utc(datetime.strptime(start_time, "%Y-%m-%d %H:%M"))
        end = ctx._local_to_utc(datetime.strptime(end_time, "%Y-%m-%d %H:%M"))
    except ValueError:
        return _t(lang, "cal_time_fmt_err")

    async with ctx._db_session_factory() as db:
        conflicts = await check_availability(
            db, owner_id, user_id, start, end
        )
        if not conflicts:
            return _t(lang, "cal_available", start=start_time, end=end_time)
        conflict_list = ", ".join(
            f"「{c.title}」({c.start_time.strftime('%H:%M')}-{c.end_time.strftime('%H:%M')})"
            for c in conflicts
        )
        return _t(lang, "cal_conflict", conflicts=conflict_list)
