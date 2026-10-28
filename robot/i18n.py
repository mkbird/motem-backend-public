"""Lightweight i18n helper for user-visible strings."""

from __future__ import annotations

import contextvars

_locale_ctx_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "locale", default="en"
)


def normalize_locale(locale: str | None) -> str:
    """Normalize Accept-Language/user locale to supported app locales."""
    if not locale:
        return "en"
    lang = locale.split(",", 1)[0].strip().split(";", 1)[0].strip().lower()
    if lang.startswith("en"):
        return "en"
    if "zh" in lang:
        return "zh-Hans"
    return "en"


def set_locale(locale: str | None) -> None:
    _locale_ctx_var.set(normalize_locale(locale))


def get_locale() -> str:
    return _locale_ctx_var.get()


_LEGACY_STRINGS: dict[str, tuple[str, str]] = {
    # device_tools
    "device_not_found":       ("没有找到设备「{name}」", 'Device "{name}" not found'),
    "device_restricted":      ("「{name}」是受限设备，需要爸爸或妈妈来操作哦。", '"{name}" is a restricted device, please ask a parent to operate it.'),
    "device_cmd_sent":        ("已发送指令：{name} {cmd}", "Command sent: {name} {cmd}"),
    "device_no_online":       ("当前没有在线设备可以执行操作。", "No online device available."),
    # weather_tools
    "weather_fetch_fail":     ("获取天气失败：{err}", "Failed to get weather: {err}"),
    "weather_result":         ("天气：{desc}，温度{temp}°C（体感{feels}°C），湿度{humi}%", "Weather: {desc}, {temp}°C (feels like {feels}°C), Humidity {humi}%"),
    "weather_city_required":  ("请输入城市名称", "Please enter a city name"),
    "weather_not_configured": ("天气服务未配置：请在 .env 配置 ROBOT_DASHSCOPE_API_KEY。", "Weather service not configured: set ROBOT_DASHSCOPE_API_KEY in .env"),
    "weather_city_not_found": ("未找到城市：{city}", "City not found: {city}"),
    "weather_city_map_fail":  ("天气城市映射失败：{err}", "Weather city mapping failed: {err}"),
    "weather_query_fail":     ("天气查询失败: {msg}", "Weather query failed: {msg}"),
    "weather_tool_not_found": ("未找到工具：{name}", "Tool not found: {name}"),
    "weather_unknown_action": ("未知的 action：{action}", "Unknown action: {action}"),
    "weather_svc_unavail":    ("天气服务暂时不可用", "Weather service temporarily unavailable"),
    # task_tools
    "task_none":              ("没有待办任务。", "No pending tasks."),
    "task_list_header":       ("任务列表：\n", "Task list:\n"),
    "task_created":           ("已创建任务「{title}」{suffix}", 'Task "{title}" created{suffix}'),
    "task_assigned_suffix":   ("，分配给{who}", ", assigned to {who}"),
    "task_due":               (" 截止: {date}", " Due: {date}"),
    "task_assigned_marker":   (" [已指派]", " [Assigned]"),
    "task_priority":          ("（优先级{p}）", " (Priority {p})"),
    # calendar_tools
    "cal_date_fmt_err":       ("日期格式不对，请用 YYYY-MM-DD 格式。", "Invalid date format, please use YYYY-MM-DD."),
    "cal_time_fmt_err":       ("时间格式不对，请用 YYYY-MM-DD HH:MM 格式。", "Invalid time format, please use YYYY-MM-DD HH:MM."),
    "cal_no_events":          ("{label}没有日程安排。", "No schedule on {label}."),
    "cal_events_header":      ("{label}的日程：\n", "Schedule for {label}:\n"),
    "cal_event_created":      ("已创建日历事件「{title}」{start}-{end}{suffix}", 'Calendar event "{title}" created {start}-{end}{suffix}'),
    "cal_await_confirm":      ("，等待参与者确认", ", awaiting participant confirmation"),
    "cal_participants":       ("，参与者：{who}", ", participants: {who}"),
    "cal_today":              ("今天", "Today"),
    "cal_tomorrow":           ("明天", "Tomorrow"),
    "cal_day_after":          ("后天", "Day after tomorrow"),
    "cal_available":          ("在{start}到{end}是空闲的。", "Free from {start} to {end}."),
    "cal_conflict":           ("这个时间段有冲突：{conflicts}", "This time period has conflicts: {conflicts}"),
    "cal_location":           (" 地点：{loc}", " Location: {loc}"),
    # web_search_tools
    "search_fail":            ("搜索失败", "Search failed"),
    "search_no_result":       ("未找到相关信息", "No relevant information found"),
    "search_not_configured":  ("搜索服务未配置：请在 .env 配置 ROBOT_DASHSCOPE_API_KEY。", "Search service not configured: set ROBOT_DASHSCOPE_API_KEY in .env"),
    "search_unavail":         ("搜索服务暂时不可用", "Search service temporarily unavailable"),
    "search_timeout":         ("搜索超时，请稍后再试", "Search timeout, please try again later"),
    "search_error":           ("搜索失败，请重新表述问题", "Search failed, please rephrase your question"),
    # camera_tools
    "camera_no_device":       ("当前没有在线设备可以执行拍照。", "No online device available to take photo."),
    # weather/service.py
    "location_fail":          ("无法获取位置信息", "Cannot obtain location information"),
    "weather_data_fail":      ("无法获取 {city} 的天气数据", "Cannot obtain weather data for {city}"),
    # streaming.py
    "image_content":          ("[图片内容] {analysis}", "[Image Content] {analysis}"),
    "process_error":          ("抱歉，处理出错了：{e}", "Sorry, an error occurred: {e}"),
}

MESSAGES: dict[str, dict[str, str]] = {
    key.upper(): {"zh-Hans": zh, "en": en}
    for key, (zh, en) in _LEGACY_STRINGS.items()
}

MESSAGES.update(
    {
        "EMAIL_OR_PHONE_REGISTERED": {
            "en": "Email or phone already registered",
            "zh-Hans": "邮箱或手机号已注册",
        },
        "INVALID_CREDENTIALS": {
            "en": "Invalid credentials",
            "zh-Hans": "邮箱或密码错误",
        },
        "USERNAME_INVALID_FORMAT": {
            "en": "Invalid username format",
            "zh-Hans": "用户名格式不合法",
        },
        "USERNAME_TAKEN": {
            "en": "Username already taken",
            "zh-Hans": "用户名已被占用",
        },
        "USER_NOT_FOUND": {
            "en": "User not found",
            "zh-Hans": "用户不存在",
        },
        "CANNOT_FOLLOW_SELF": {
            "en": "You cannot follow yourself",
            "zh-Hans": "不能关注自己",
        },
        "AVATAR_BAD_TYPE": {
            "en": "Unsupported image type",
            "zh-Hans": "不支持的图片格式",
        },
        "AVATAR_TOO_LARGE": {
            "en": "Avatar file too large",
            "zh-Hans": "头像文件过大",
        },
        "INVALID_REFRESH_TOKEN": {
            "en": "Invalid refresh token",
            "zh-Hans": "刷新令牌无效",
        },
        "INVALID_TOKEN": {
            "en": "Invalid token",
            "zh-Hans": "无效的身份令牌",
        },
        "USER_NOT_FOUND": {
            "en": "User not found",
            "zh-Hans": "用户不存在",
        },
        "AI_AGENT_NOT_INITIALIZED": {
            "en": "AI Agent is not initialized",
            "zh-Hans": "AI Agent 未初始化",
        },
        "DEVICE_NOT_FOUND_HTTP": {
            "en": "Device not found",
            "zh-Hans": "设备不存在",
        },
        "TASK_NOT_FOUND": {
            "en": "Task not found",
            "zh-Hans": "任务不存在",
        },
        "EVENT_NOT_FOUND": {
            "en": "Event not found",
            "zh-Hans": "事件不存在",
        },
        "NOT_EVENT_PARTICIPANT": {
            "en": "Not a participant",
            "zh-Hans": "不是该事件的参与者",
        },
        "NOTIFICATION_NOT_FOUND": {
            "en": "Notification not found",
            "zh-Hans": "通知不存在",
        },
        "APNS_ENV_INVALID": {
            "en": "APNs environment must be 'sandbox' or 'production'",
            "zh-Hans": "APNs 环境必须是 sandbox 或 production",
        },
        "MARKED_AS_READ": {
            "en": "Marked as read",
            "zh-Hans": "已标记为已读",
        },
        "MARKED_NOTIFICATIONS_AS_READ": {
            "en": "Marked {count} notifications as read",
            "zh-Hans": "已标记 {count} 条通知为已读",
        },
        "RESPONSE_RECORDED": {
            "en": "Response recorded",
            "zh-Hans": "已记录回复",
        },
        "GAME_ZIP_NOT_FOUND": {
            "en": "Game zip not found",
            "zh-Hans": "游戏压缩包不存在",
        },
        "GAME_NOT_FOUND": {
            "en": "Game not found",
            "zh-Hans": "游戏不存在",
        },
        "GAME_TITLE_EMPTY": {
            "en": "Title cannot be empty",
            "zh-Hans": "标题不能为空",
        },
        "GAME_TITLE_TOO_LONG": {
            "en": "Title is too long",
            "zh-Hans": "标题过长",
        },
        "WIDGET_TITLE_EMPTY": {
            "en": "Title cannot be empty",
            "zh-Hans": "标题不能为空",
        },
        "WIDGET_TITLE_TOO_LONG": {
            "en": "Title is too long",
            "zh-Hans": "标题过长",
        },
        "COMMAND_SENT": {
            "en": "Command sent: {name} {cmd}",
            "zh-Hans": "已发送指令：{name} {cmd}",
        },
        "CALENDAR_REMINDER": {
            "en": "Calendar Reminder",
            "zh-Hans": "日程提醒",
        },
        "CALENDAR_REMINDER_BODY": {
            "en": "'{title}' starts in {minutes} minutes ({time}).",
            "zh-Hans": "「{title}」将在{minutes}分钟后开始（{time}）",
        },
        "TASK_OVERDUE": {
            "en": "Task Overdue",
            "zh-Hans": "任务已过期",
        },
        "TASK_DUE_SOON": {
            "en": "Task Due Soon",
            "zh-Hans": "任务即将到期",
        },
        "TASK_OVERDUE_STATUS": {
            "en": "is overdue",
            "zh-Hans": "已过期",
        },
        "TASK_DUE_SOON_STATUS": {
            "en": "is due within 1 hour",
            "zh-Hans": "将在1小时内到期",
        },
        "TASK_DEADLINE_BODY": {
            "en": "'{title}' {status}.",
            "zh-Hans": "「{title}」{status}",
        },
        "GAME_OPERATION_ERROR": {
            "en": "Game operation failed: {e}",
            "zh-Hans": "游戏操作出错：{e}",
        },
        "GAME_NOT_REMIXABLE": {
            "en": "This game is view-only and cannot be remixed.",
            "zh-Hans": "该游戏设置为仅展示，无法二创。",
        },
        "GAME_BUILTIN_NOT_REMIXABLE": {
            "en": "Built-in games cannot be remixed.",
            "zh-Hans": "内置游戏无法二创。",
        },
        "GAME_CANNOT_REMIX_OWN": {
            "en": "You cannot remix your own game. Tap the pencil icon to edit it directly.",
            "zh-Hans": "无法二创自己的游戏，请直接点击铅笔图标编辑。",
        },
        "GAME_VISIBILITY_INVALID": {
            "en": "Invalid visibility value.",
            "zh-Hans": "可见性参数无效。",
        },
        "GAME_BUILTIN_VISIBILITY_LOCKED": {
            "en": "Built-in game visibility cannot be changed.",
            "zh-Hans": "内置游戏的可见性不可修改。",
        },
        "GAME_CANNOT_FAVORITE_OWN": {
            "en": "You already created this game; it is automatically in My Games.",
            "zh-Hans": "你已是该游戏作者，已自动出现在「我的」中。",
        },
        "GAME_NOT_OWNED": {
            "en": "You don't own this game.",
            "zh-Hans": "你不是该游戏的作者。",
        },
        "GAME_SOURCE_MISSING_DIR": {
            "en": "Game source directory is missing on the server.",
            "zh-Hans": "服务端缺少游戏源目录。",
        },
        "GAME_BUILTIN_NOT_DELETABLE": {
            "en": "Built-in games cannot be deleted.",
            "zh-Hans": "内置游戏无法删除。",
        },
        "GAME_COVER_INVALID_TYPE": {
            "en": "Cover image must be an image file (image/*).",
            "zh-Hans": "封面必须是图片文件（image/*）。",
        },
        "GAME_COVER_TOO_LARGE": {
            "en": "Cover image too large; max 5MB.",
            "zh-Hans": "封面文件过大，最大 5MB。",
        },
        "GAME_COVER_DECODE_FAILED": {
            "en": "Cover image could not be decoded.",
            "zh-Hans": "封面图无法解码。",
        },
        "GAME_ASSETS_NONE_PROVIDED": {
            "en": "No images provided.",
            "zh-Hans": "未提供任何图片。",
        },
        "GAME_ASSETS_TOO_MANY": {
            "en": "Too many assets in one upload (max 10).",
            "zh-Hans": "单次上传最多 10 张图片。",
        },
        "GAME_ASSETS_INVALID_TYPE": {
            "en": "Asset must be an image file (PNG / JPEG / WebP).",
            "zh-Hans": "图片必须是 PNG / JPEG / WebP 格式。",
        },
        "GAME_ASSETS_TOO_LARGE": {
            "en": "Asset image too large; max 8MB per file.",
            "zh-Hans": "单个图片过大，单文件最大 8MB。",
        },
        "GAME_ASSETS_INVALID_FILENAME": {
            "en": "Asset filename invalid after sanitization.",
            "zh-Hans": "图片文件名无法处理。",
        },
        "GAME_ASSETS_DECODE_FAILED": {
            "en": "Asset image could not be decoded.",
            "zh-Hans": "图片无法解码。",
        },
        "CLAUDE_SDK_NOT_INSTALLED": {
            "en": "Error: Claude Code SDK is not installed. Run pip install claude-code-sdk.",
            "zh-Hans": "错误：Claude Code SDK 未安装，请运行 pip install claude-code-sdk",
        },
        "GAME_CODE_GENERATION_ERROR": {
            "en": "Code generation failed: {e}",
            "zh-Hans": "代码生成出错：{e}",
        },
        "GAME_TEMPLATE_MISSING": {
            "en": "Error: game template directory does not exist. Contact the administrator.",
            "zh-Hans": "错误：游戏模板目录不存在，请联系管理员。",
        },
        "GAME_NOT_FOUND_DETAIL": {
            "en": "Error: game {game_id} not found",
            "zh-Hans": "错误：找不到游戏 {game_id}",
        },
        "GAME_CREATE_INCOMPLETE": {
            "en": "Game '{title}' may be incomplete, but files were saved. Game ID: {game_id}",
            "zh-Hans": "游戏「{title}」生成可能不完整，但文件已保存。游戏ID: {game_id}",
        },
        "GAME_CREATE_FAILED": {
            "en": "Game '{title}' generation failed (AI did not produce a valid game). Please retry.",
            "zh-Hans": "游戏「{title}」生成失败（AI 没有产出有效内容），请重试。",
        },
        "GAME_CREATE_DONE": {
            "en": "Game '{title}' is ready. Game ID: {game_id}",
            "zh-Hans": "游戏「{title}」创建完成！游戏ID: {game_id}",
        },
        "GAME_NO_CHANGES": {
            "en": "No file changes were detected. The modification request may not be specific enough.",
            "zh-Hans": "没有检测到文件变更，可能修改指令不够具体。",
        },
        "GAME_MODIFY_DONE": {
            "en": "Game updated. Modified {count} files: {files}",
            "zh-Hans": "游戏已修改，更新了 {count} 个文件：{files}",
        },
        "GAME_PROGRESS_ANALYZING": {
            "en": "AI is analyzing the request... ({elapsed:.0f}s elapsed)",
            "zh-Hans": "AI 正在分析需求... (已用 {elapsed:.0f}s)",
        },
        "GAME_PROGRESS_WRITING": {
            "en": "Writing {path} ({chars} chars, turn {turn})",
            "zh-Hans": "正在编写 {path} ({chars} 字符, 第{turn}轮)",
        },
        "GAME_PROGRESS_READING": {
            "en": "Reading {path}",
            "zh-Hans": "正在阅读 {path}",
        },
        "GAME_PROGRESS_CODE_DONE": {
            "en": "Code generation complete ({count} files written, {elapsed:.0f}s)",
            "zh-Hans": "代码生成完成 (写入 {count} 个文件, 用时 {elapsed:.0f}s)",
        },
        "GAME_PROGRESS_ANALYSIS_DONE": {
            "en": "AI analysis complete ({elapsed:.0f}s)",
            "zh-Hans": "AI 分析完成 (用时 {elapsed:.0f}s)",
        },
        "GAME_PROGRESS_TIMEOUT": {
            "en": "AI took too long, aborting (after {elapsed:.0f}s)",
            "zh-Hans": "AI 思考超时，已放弃本次生成 (用时 {elapsed:.0f}s)",
        },
        "GAME_PROGRESS_FIXING_SYNTAX": {
            "en": "Found {count} syntax issues. Fixing...",
            "zh-Hans": "发现 {count} 个语法问题，正在修复...",
        },
        "GAME_PROGRESS_PREPARING_TEMPLATE": {
            "en": "Preparing game template...",
            "zh-Hans": "正在准备游戏模板...",
        },
        "GAME_PROGRESS_GENERATING_CODE": {
            "en": "AI is generating game code...",
            "zh-Hans": "AI 正在生成游戏代码...",
        },
        "GAME_PROGRESS_CHECKING_QUALITY": {
            "en": "Checking code quality...",
            "zh-Hans": "正在检查代码质量...",
        },
        "GAME_PROGRESS_VERIFYING_STRUCTURE": {
            "en": "Verifying file structure...",
            "zh-Hans": "正在验证文件结构...",
        },
        "GAME_PROGRESS_PACKING": {
            "en": "Packaging game...",
            "zh-Hans": "正在打包游戏...",
        },
        "GAME_PROGRESS_MODIFYING": {
            "en": "AI is modifying game code...",
            "zh-Hans": "AI 正在修改游戏代码...",
        },
        "GAME_PROGRESS_AI_PREFIX": {
            "en": "AI: {text}",
            "zh-Hans": "AI：{text}",
        },
        "GAME_PROGRESS_EMPTY": {
            "en": "",
            "zh-Hans": "",
        },
        "GAME_PROGRESS_UNDERSTANDING": {
            "en": "AI is understanding your request...",
            "zh-Hans": "AI 正在理解你的需求...",
        },
        "GAME_PROGRESS_LOADING": {
            "en": "Loading game...",
            "zh-Hans": "加载游戏...",
        },
        "GAME_PROGRESS_DOWNLOADING": {
            "en": "Downloading game files...",
            "zh-Hans": "正在下载游戏文件...",
        },
        "GAME_DOWNLOAD_FAILED": {
            "en": "Game download failed",
            "zh-Hans": "游戏下载失败",
        },
        "GAME_PATCH_DOWNLOAD_FALLBACK": {
            "en": "Update failed. Downloading again...",
            "zh-Hans": "更新失败，正在重新下载...",
        },
        "GAME_UPDATED_FILE_COUNT": {
            "en": "Game updated ({count} files)",
            "zh-Hans": "游戏已更新（{count} 个文件）",
        },
        # ── AI Widget keys ────────────────────────────────────────────
        "WIDGET_TEMPLATE_MISSING": {
            "en": "Error: widget template directory does not exist. Contact the administrator.",
            "zh-Hans": "错误：小组件模板目录不存在，请联系管理员。",
        },
        "WIDGET_NOT_FOUND_DETAIL": {
            "en": "Error: widget {widget_id} not found",
            "zh-Hans": "错误：找不到小组件 {widget_id}",
        },
        "WIDGET_NOT_FOUND": {
            "en": "Widget not found",
            "zh-Hans": "未找到该小组件",
        },
        "WIDGET_ZIP_NOT_FOUND": {
            "en": "Widget package not found",
            "zh-Hans": "未找到小组件压缩包",
        },
        "WIDGET_NOT_OWNED": {
            "en": "You do not own this widget",
            "zh-Hans": "你不是这个小组件的所有者",
        },
        "WIDGET_SEED_NOT_DELETABLE": {
            "en": "Built-in widget templates cannot be deleted",
            "zh-Hans": "内置小组件模板不可删除",
        },
        "WIDGET_CODE_GENERATION_ERROR": {
            "en": "Widget code generation failed: {e}",
            "zh-Hans": "小组件代码生成出错：{e}",
        },
        "WIDGET_CREATE_DONE": {
            "en": "Widget '{title}' is ready. Widget ID: {widget_id}",
            "zh-Hans": "小组件「{title}」创建完成！ID: {widget_id}",
        },
        "WIDGET_CREATE_INCOMPLETE": {
            "en": "Widget '{title}' may be incomplete; some files were saved. Widget ID: {widget_id}",
            "zh-Hans": "小组件「{title}」可能不完整，但已保存可用部分。ID: {widget_id}",
        },
        "WIDGET_NO_CHANGES": {
            "en": "No file changes were detected. The modification request may not be specific enough.",
            "zh-Hans": "没有检测到文件变更，可能修改指令不够具体。",
        },
        "WIDGET_MODIFY_DONE": {
            "en": "Widget updated. Modified {count} files: {files}",
            "zh-Hans": "小组件已修改，更新了 {count} 个文件：{files}",
        },
        "WIDGET_PROGRESS_PREPARING_TEMPLATE": {
            "en": "Preparing widget template...",
            "zh-Hans": "正在准备小组件模板...",
        },
        "WIDGET_PROGRESS_GENERATING_CODE": {
            "en": "AI is generating widget code...",
            "zh-Hans": "AI 正在生成小组件代码...",
        },
        "WIDGET_PROGRESS_MODIFYING": {
            "en": "AI is modifying widget code...",
            "zh-Hans": "AI 正在修改小组件代码...",
        },
        "WIDGET_PROGRESS_ANALYZING": {
            "en": "AI is analyzing the widget request...",
            "zh-Hans": "AI 正在分析小组件需求...",
        },
        "WIDGET_PROGRESS_WRITING": {
            "en": "Writing {path}",
            "zh-Hans": "正在编写 {path}",
        },
        "WIDGET_PROGRESS_CODE_DONE": {
            "en": "Widget code generation complete ({count} files written)",
            "zh-Hans": "小组件代码生成完成 (写入 {count} 个文件)",
        },
        "WIDGET_PROGRESS_ANALYSIS_DONE": {
            "en": "AI analysis complete",
            "zh-Hans": "AI 分析完成",
        },
        "WIDGET_PROGRESS_TIMEOUT": {
            "en": "AI took too long, aborting widget generation",
            "zh-Hans": "AI 思考超时，已放弃本次小组件生成",
        },
        "RATE_LIMIT_GAME_CREATE": {
            "en": "You've created too many games recently. Please wait an hour before trying again.",
            "zh-Hans": "近期创建游戏次数过多，请等待一小时后重试。",
        },
        "RATE_LIMIT_WIDGET_CREATE": {
            "en": "You've created too many widgets recently. Please wait an hour before trying again.",
            "zh-Hans": "近期创建小组件次数过多，请等待一小时后重试。",
        },
        "WIDGET_PROGRESS_PACKING": {
            "en": "Packaging widget...",
            "zh-Hans": "正在打包小组件...",
        },
        "WIDGET_OPERATION_ERROR": {
            "en": "Widget operation failed: {e}",
            "zh-Hans": "小组件操作出错：{e}",
        },
        "WIDGET_NOT_REMIXABLE": {
            "en": "This widget is view-only and cannot be remixed.",
            "zh-Hans": "该小组件设置为仅展示，无法二创。",
        },
        "WIDGET_BUILTIN_NOT_REMIXABLE": {
            "en": "Built-in widgets cannot be remixed.",
            "zh-Hans": "内置小组件无法二创。",
        },
        "WIDGET_CANNOT_REMIX_OWN": {
            "en": "You cannot remix your own widget. Tap the pencil icon to edit it directly.",
            "zh-Hans": "无法二创自己的小组件，请直接点击铅笔图标编辑。",
        },
        "WIDGET_VISIBILITY_INVALID": {
            "en": "Invalid visibility value.",
            "zh-Hans": "可见性参数无效。",
        },
        "WIDGET_BUILTIN_VISIBILITY_LOCKED": {
            "en": "Built-in widget visibility cannot be changed.",
            "zh-Hans": "内置小组件的可见性不可修改。",
        },
        "WIDGET_FAVORITE_OWN_NOT_ALLOWED": {
            "en": "You already created this widget; it is automatically in My Widgets.",
            "zh-Hans": "你已是该小组件作者，已自动出现在「我的」中。",
        },
        "WIDGET_SOURCE_MISSING_DIR": {
            "en": "Widget source directory is missing on the server.",
            "zh-Hans": "服务端缺少小组件源目录。",
        },
        "GAME_CREATED_TITLE": {
            "en": "Game '{title}' is ready",
            "zh-Hans": "游戏「{title}」创建完成",
        },
        "GAME_INCOMPLETE_PROJECT": {
            "en": "Game files are incomplete. Missing project.godot",
            "zh-Hans": "游戏文件不完整，缺少 project.godot",
        },
        "NETWORK_REQUEST_FAILED": {
            "en": "Network request failed",
            "zh-Hans": "网络请求失败",
        },
        "LOGIN_FAILED_STATUS": {
            "en": "Login failed ({status})",
            "zh-Hans": "登录失败 ({status})",
        },
        "REGISTER_FAILED_STATUS": {
            "en": "Registration failed ({status})",
            "zh-Hans": "注册失败 ({status})",
        },
        "CANNOT_CONNECT_SERVER_ERROR": {
            "en": "Cannot connect to server: {error}",
            "zh-Hans": "无法连接服务器: {error}",
        },
        "GAME_FIX_PROMPT": {
            "en": (
                "The following GDScript files have syntax errors. Fix them:\n\n{errors}\n\n"
                "Read the files with errors and fix all issues. Only change the broken parts; do not change game logic.\n"
                "Use relative paths such as scenes/main.gd. Do not use absolute paths."
            ),
            "zh-Hans": (
                "The following GDScript files have syntax errors. Fix them:\n\n{errors}\n\n"
                "Read the files with errors and fix all issues. Only change the broken parts; do not change game logic.\n"
                "Use relative paths such as scenes/main.gd. Do not use absolute paths."
            ),
        },
        "TASK_TITLE_EMPTY": {
            "en": "Task title cannot be empty.",
            "zh-Hans": "任务标题不能为空。",
        },
        "TASK_PRIORITY_INVALID": {
            "en": "Priority must be between 1 and 5; received {priority}.",
            "zh-Hans": "优先级必须在 1-5 之间，收到的值为 {priority}。",
        },
        "TASK_DATE_INVALID": {
            "en": "Invalid date format. Use YYYY-MM-DD; received '{date}'.",
            "zh-Hans": "日期格式不对，请用 YYYY-MM-DD 格式。收到的值为 '{date}'。",
        },
        "EVENT_TITLE_EMPTY": {
            "en": "Event title cannot be empty.",
            "zh-Hans": "事件标题不能为空。",
        },
        "EVENT_END_BEFORE_START": {
            "en": "End time must be later than start time.",
            "zh-Hans": "结束时间必须晚于开始时间。",
        },
        "GAME_CREATE_PROMPT_TEMPLATE": {
            "en": "Tell me what game you want to create. For example: make a space shooter controlled by body movement.",
            "zh-Hans": "告诉我你想做什么游戏，我来帮你创建！\n例如：做一个太空射击游戏，用身体控制飞船",
        },
    }
)

_LEGACY_TO_KEY = {
    "日程提醒": "CALENDAR_REMINDER",
    "任务已过期": "TASK_OVERDUE",
    "任务即将到期": "TASK_DUE_SOON",
    "Device not found": "DEVICE_NOT_FOUND_HTTP",
    "Task not found": "TASK_NOT_FOUND",
    "Event not found": "EVENT_NOT_FOUND",
    "Not a participant": "NOT_EVENT_PARTICIPANT",
    "Notification not found": "NOTIFICATION_NOT_FOUND",
    "Game not found": "GAME_NOT_FOUND",
    "Game zip not found": "GAME_ZIP_NOT_FOUND",
    "Marked as read": "MARKED_AS_READ",
    "Response recorded": "RESPONSE_RECORDED",
    "Marked {count} notifications as read": "MARKED_NOTIFICATIONS_AS_READ",
    "AI Agent 未初始化": "AI_AGENT_NOT_INITIALIZED",
}


def t(lang: str, key: str, **kwargs: object) -> str:
    """Return localized string for key.

    lang: language tag, e.g. "zh-Hans", "en", "en-US"
    key: string key from _STRINGS
    kwargs: format arguments
    """
    key = _LEGACY_TO_KEY.get(key, key)
    msg = MESSAGES.get(key) or MESSAGES.get(key.upper())
    if not msg:
        return key.format(**kwargs) if kwargs else key
    tmpl = msg["en"] if normalize_locale(lang) == "en" else msg["zh-Hans"]
    return tmpl.format(**kwargs) if kwargs else tmpl


def _(key: str, **kwargs: object) -> str:
    """Translate using the current request locale."""
    return t(get_locale(), key, **kwargs)
