# scripts/result_card.gd — Fast path result card + 续 1 命挑战 UI
#
# 两个静态方法：
#
#     ResultCard.show(self, score, best, on_retry, on_home, cfg)
#         游戏结束时的结算卡：分数滚动 + Best diff 红/绿字 + stats 列表 +
#         大橙色 "再来一次" + 小 "回到首页"。滑入动画 ~0.3s。
#
#     ResultCard.show_challenge(self, "3 秒内连接 3 个！", 3.0, on_timeout)
#         lives=0 那一刻显示的"续 1 命"挑战横幅 + 倒计时圈。
#         strategy 持续跑 gameplay，达成条件后 strategy 调返回 Node 的
#         queue_free() 复活；倒计时到点了 → on_timeout 触发。
#
# 配合骨架 `_template/scenes/main.gd` 的 modal_overlay group 契约：本 helper
# 创建的 CanvasLayer 自动 add_to_group("modal_overlay")，骨架检测到该 group
# 有节点时跳过全局 tap-to-restart，让按钮顺利接到点击事件。
#
# class_name = ResultCard，slow path 没此文件 → 类不存在 → 完美隔离。

extends RefCounted
class_name ResultCard


# ─── 颜色常量 ────────────────────────────────────────────────────────────────
const _MODAL_GROUP: String = "modal_overlay"
const _ORANGE: Color = Color(1.0, 0.55, 0.10)     # 主按钮
const _RED: Color = Color(1.0, 0.35, 0.30)        # 落后纪录
const _GREEN: Color = Color(0.30, 0.95, 0.40)     # 新纪录
const _GOLD: Color = Color(1.0, 0.85, 0.20)       # 平纪录 / Score
const _BG: Color = Color(0.08, 0.10, 0.14, 0.95)  # 卡片底
const _STAT: Color = Color(0.85, 0.88, 0.92)      # 中性 stat 文字
const _OUTLINE: Color = Color(0, 0, 0, 0.85)


# ─── show ─────────────────────────────────────────────────────────────────
# 显示结算卡。caller 已经做完 i18n，cfg 里全是终端文案。
# cfg keys（全部可选，缺省走英文 fallback）：
#   score_label      —— score 数字前的标签，例 "Score" / "分数"
#   new_record       —— 破纪录字幕，例 "New Record!" / "新纪录！"
#   tied             —— 平纪录字幕，例 "Tied Record" / "平纪录"
#   behind_record    —— %d format 模板，例 "%d to beat record" / "差 %d 分破纪录！"
#   retry            —— 主按钮文案，例 "Try Again" / "再来一次"
#   home             —— 副按钮文案，例 "Home" / "回到首页"
#   stats            —— Array[String]，例 ["Combo Max: x4", "Quick Hits: 8"]
# 返回 CanvasLayer 节点，caller 可手动 queue_free，按钮回调里自动 free。
static func show(host: Node2D, score: int, best: int,
				 on_retry: Callable, on_home: Callable,
				 cfg: Dictionary = {}) -> Node:
	if not is_instance_valid(host):
		return null
	var tree := host.get_tree()
	if tree == null:
		return null

	# CanvasLayer 让 UI 独立于 2D 世界 transform（防止被 Juice.screen_shake 牵着抖）
	var layer := CanvasLayer.new()
	layer.layer = 100
	layer.name = "ResultCardLayer"
	layer.add_to_group(_MODAL_GROUP)
	host.add_child(layer)

	var screen: Vector2 = host.get_viewport_rect().size

	# ─ 卡片背景 ─────────────
	var card_size: Vector2 = Vector2(
		min(640.0, screen.x - 40.0),
		min(360.0, screen.y - 40.0),
	)
	var card_pos_final: Vector2 = (screen - card_size) * 0.5

	var bg := ColorRect.new()
	bg.color = _BG
	bg.size = card_size
	bg.position = Vector2(card_pos_final.x, screen.y + 40.0)  # 起始位置在屏幕外
	bg.mouse_filter = Control.MOUSE_FILTER_STOP  # 卡片本身吃掉点击
	layer.add_child(bg)

	# ─ 内容容器 ─────────────
	var box := VBoxContainer.new()
	box.size = card_size
	box.position = Vector2.ZERO
	box.alignment = BoxContainer.ALIGNMENT_CENTER
	box.add_theme_constant_override("separation", 12)
	bg.add_child(box)

	# 顶部 padding（用空 Control 占位，VBox 不直接支持 padding）
	var pad_top := Control.new()
	pad_top.custom_minimum_size = Vector2(0, 16)
	box.add_child(pad_top)

	# Score 大字（开局 0，0.6s tween 到 score）
	var score_label_text: String = cfg.get("score_label", "Score")
	var score_lbl := Label.new()
	score_lbl.text = "%s 0" % score_label_text
	score_lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	score_lbl.add_theme_font_size_override("font_size", 64)
	score_lbl.add_theme_color_override("font_color", _GOLD)
	score_lbl.add_theme_color_override("font_outline_color", _OUTLINE)
	score_lbl.add_theme_constant_override("outline_size", 5)
	score_lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	box.add_child(score_lbl)

	# Subtitle：根据 score vs best 选三档之一
	var subtitle_text: String
	var subtitle_color: Color
	if score > best:
		subtitle_text = cfg.get("new_record", "New Record!")
		subtitle_color = _GREEN
	elif score == best and best > 0:
		subtitle_text = cfg.get("tied", "Tied Record")
		subtitle_color = _GOLD
	else:
		var diff: int = best - score
		var fmt: String = cfg.get("behind_record", "%d to beat record")
		# 防御 caller 传了不含 %d 的 fmt
		if "%d" in fmt:
			subtitle_text = fmt % diff
		else:
			subtitle_text = "%s %d" % [fmt, diff]
		subtitle_color = _RED

	var subtitle := Label.new()
	subtitle.text = subtitle_text
	subtitle.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	subtitle.add_theme_font_size_override("font_size", 24)
	subtitle.add_theme_color_override("font_color", subtitle_color)
	subtitle.add_theme_color_override("font_outline_color", _OUTLINE)
	subtitle.add_theme_constant_override("outline_size", 3)
	subtitle.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	box.add_child(subtitle)

	# Stats（每条一行小字）
	var stats: Array = cfg.get("stats", [])
	for stat_str in stats:
		var st := Label.new()
		st.text = str(stat_str)
		st.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		st.add_theme_font_size_override("font_size", 18)
		st.add_theme_color_override("font_color", _STAT)
		st.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		box.add_child(st)

	# 中部 spacer
	var spacer := Control.new()
	spacer.size_flags_vertical = Control.SIZE_EXPAND_FILL
	box.add_child(spacer)

	# 大橙色 retry 按钮
	var retry_btn := Button.new()
	retry_btn.text = cfg.get("retry", "Try Again")
	retry_btn.custom_minimum_size = Vector2(card_size.x * 0.8, 64)
	retry_btn.size_flags_horizontal = Control.SIZE_SHRINK_CENTER
	retry_btn.add_theme_font_size_override("font_size", 28)
	_style_button(retry_btn, _ORANGE)
	box.add_child(retry_btn)

	# Home 小按钮
	var home_btn := Button.new()
	home_btn.text = cfg.get("home", "Home")
	home_btn.flat = true
	home_btn.add_theme_font_size_override("font_size", 16)
	home_btn.add_theme_color_override("font_color", _STAT)
	home_btn.size_flags_horizontal = Control.SIZE_SHRINK_CENTER
	box.add_child(home_btn)

	# 底部 padding
	var pad_bot := Control.new()
	pad_bot.custom_minimum_size = Vector2(0, 12)
	box.add_child(pad_bot)

	# ─ 滑入动画 ──────────────
	var slide_tween := host.create_tween()
	slide_tween.tween_property(bg, "position:y", card_pos_final.y, 0.28)\
		.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)

	# ─ 分数滚动（滑入开始后 0.1s 启动，~0.6s 完成）─
	var score_tween := host.create_tween()
	score_tween.tween_interval(0.1)
	score_tween.tween_method(
		func(v): score_lbl.text = "%s %d" % [score_label_text, int(v)],
		0.0, float(score), 0.6,
	).set_ease(Tween.EASE_OUT)

	# ─ 按钮回调 ──────────────
	retry_btn.pressed.connect(func():
		layer.queue_free()
		if on_retry.is_valid():
			on_retry.call()
	)
	home_btn.pressed.connect(func():
		layer.queue_free()
		if on_home.is_valid():
			on_home.call()
	)

	return layer


# ─── show_challenge ──────────────────────────────────────────────────────
# lives=0 那一刻显示挑战横幅。strategy 持续跑 gameplay，达成挑战条件后
# 调返回 Node 的 queue_free() 复活；倒计时归零 → on_timeout 触发 + 自动 free。
#
# UI 布局：屏幕顶部居中横幅（dark BG 圆角 + 倒计时秒数大字 + 挑战文案小字）
# 不全屏遮挡，让玩家继续看到下方 gameplay 拼最后一搏。
static func show_challenge(host: Node2D, text: String, duration: float,
							on_timeout: Callable) -> Node:
	if not is_instance_valid(host) or text.is_empty():
		return null
	var tree := host.get_tree()
	if tree == null:
		return null

	var layer := CanvasLayer.new()
	layer.layer = 90  # 比 ResultCard 低，挑战通常先发，结算卡可叠加上去
	layer.name = "ChallengeLayer"
	layer.add_to_group(_MODAL_GROUP)
	host.add_child(layer)

	var screen: Vector2 = host.get_viewport_rect().size

	var banner_size: Vector2 = Vector2(min(560.0, screen.x - 40.0), 90.0)
	var banner_pos: Vector2 = Vector2((screen.x - banner_size.x) * 0.5, 24.0)

	var banner := ColorRect.new()
	banner.color = Color(0.10, 0.10, 0.14, 0.92)
	banner.size = banner_size
	banner.position = banner_pos
	banner.mouse_filter = Control.MOUSE_FILTER_IGNORE  # 不阻塞 gameplay tap
	layer.add_child(banner)

	# 内部 HBox：[倒计时大字 | 挑战文案]
	var hbox := HBoxContainer.new()
	hbox.size = banner_size
	hbox.add_theme_constant_override("separation", 18)
	banner.add_child(hbox)

	# 左 padding
	var pad_l := Control.new()
	pad_l.custom_minimum_size = Vector2(16, 0)
	hbox.add_child(pad_l)

	# 倒计时秒数（大字）
	var count_lbl := Label.new()
	count_lbl.text = str(int(ceil(duration)))
	count_lbl.add_theme_font_size_override("font_size", 56)
	count_lbl.add_theme_color_override("font_color", _ORANGE)
	count_lbl.add_theme_color_override("font_outline_color", _OUTLINE)
	count_lbl.add_theme_constant_override("outline_size", 5)
	count_lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	count_lbl.custom_minimum_size = Vector2(72, 0)
	hbox.add_child(count_lbl)

	# 挑战文案
	var text_lbl := Label.new()
	text_lbl.text = text
	text_lbl.add_theme_font_size_override("font_size", 22)
	text_lbl.add_theme_color_override("font_color", Color.WHITE)
	text_lbl.add_theme_color_override("font_outline_color", _OUTLINE)
	text_lbl.add_theme_constant_override("outline_size", 3)
	text_lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	text_lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	text_lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hbox.add_child(text_lbl)

	# ─ 倒计时动画：每秒更新数字，duration 后触发 on_timeout ──
	# tween_method 平滑驱动一个 float，回调里 ceil → int 显示
	var count_tween := host.create_tween()
	count_tween.tween_method(
		func(v):
			if not is_instance_valid(count_lbl):
				return
			count_lbl.text = str(max(0, int(ceil(v)))),
		duration, 0.0, duration,
	)
	# 时间到 → 自动 free + on_timeout（如果 layer 还在场上没被 strategy 复活释放）
	count_tween.tween_callback(func():
		if not is_instance_valid(layer):
			return
		layer.queue_free()
		if on_timeout.is_valid():
			on_timeout.call()
	)

	return layer


# ─── private helpers ─────────────────────────────────────────────────────
static func _style_button(btn: Button, bg_color: Color) -> void:
	var normal := StyleBoxFlat.new()
	normal.bg_color = bg_color
	normal.corner_radius_top_left = 12
	normal.corner_radius_top_right = 12
	normal.corner_radius_bottom_left = 12
	normal.corner_radius_bottom_right = 12
	btn.add_theme_stylebox_override("normal", normal)
	var hover := normal.duplicate() as StyleBoxFlat
	hover.bg_color = bg_color.lightened(0.08)
	btn.add_theme_stylebox_override("hover", hover)
	var pressed := normal.duplicate() as StyleBoxFlat
	pressed.bg_color = bg_color.darkened(0.12)
	btn.add_theme_stylebox_override("pressed", pressed)
	btn.add_theme_color_override("font_color", Color.WHITE)
	btn.add_theme_color_override("font_outline_color", _OUTLINE)
	btn.add_theme_constant_override("outline_size", 3)
