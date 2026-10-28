# scripts/particles.gd — Fast path particle / floating text helpers
#
# 三个静态方法，由 fast path strategy 的 AI ZONE 代码在命中 / 失败 / combo
# 触发时调用：
#
#     Particles.splatter(self, item_pos, fruit_color, 10)
#         切水果 / 击中障碍时的"爆汁"粒子
#
#     Particles.floater(self, item_pos, "+5", Color(1, 0.85, 0.1))
#         "+1 / NEAR MISS / QUICK" 浮字（rise + fade）
#
#     Particles.combo_blast(self, "TRIPLE!", Color(1, 0.85, 0.1))
#         屏幕中央大字 + 全屏白闪，combo / Fever 触发时用
#
# class_name = Particles 全局可见，不挂 autoload（同 Juice 设计：slow path 没
# 此文件 → 类不存在 → SDK 写代码不引用 → 完美隔离）。
#
# 实现选择：
# - 用 ColorRect / Label + Tween + create_timer 销毁，不用 CPUParticles2D：
#   texture=null 时的渲染行为在 Godot 4.x 不同 patch 版本不稳，ColorRect
#   走 Control 自带的 modulate / position tween 路径，行为可预测。
# - 不用 inner class（_template/CLAUDE.md 明确：crash iOS embedded mode）。
#   每次调用就 new 一组节点 + 启动 tween + 挂自动 queue_free 定时器。
# - Label 不指定 font，走 project.godot 配的 gui 主题（default_theme.tres
#   已含 CJK 字体），中文 combo 文案能正确渲染。
# - tween cleanup 用 create_timer(...).timeout.connect(node.queue_free) 而
#   不是 await tween.finished —— 后者会阻塞循环创建多个粒子的并发性，
#   timer 路径让所有 N 个粒子真并行 spawn。

extends RefCounted
class_name Particles


# ─── 默认颜色 ────────────────────────────────────────────────────────────────
const _GOLD: Color = Color(1.0, 0.85, 0.20)        # combo_blast / floater 高分默认色
const _OUTLINE: Color = Color(0.0, 0.0, 0.0, 0.85) # 文字描边色（暗色背景上提可读性）


# ─── splatter ─────────────────────────────────────────────────────────────
# N 颗 6×6 ColorRect 从 pos 向外飞溅，抛物线 + alpha fade，到时自动销毁。
# 切水果 / 击中 obstacle 时调用。color 应该取自被击物（西瓜 → 红、香蕉 → 黄）。
# intensity 调整发散速度，count 调整粒子数。
static func splatter(host: Node2D, pos: Vector2, color: Color = Color.WHITE,
					 count: int = 8, intensity: float = 1.0) -> void:
	if not is_instance_valid(host):
		return
	var tree := host.get_tree()
	if tree == null:
		return
	for i in range(count):
		var dot := ColorRect.new()
		dot.size = Vector2(6, 6)
		dot.color = color
		dot.position = pos - Vector2(3, 3)
		dot.mouse_filter = Control.MOUSE_FILTER_IGNORE
		host.add_child(dot)
		var angle: float = randf() * TAU
		var speed: float = randf_range(140.0, 240.0) * intensity
		var initial_vel: Vector2 = Vector2.from_angle(angle) * speed
		var lifetime: float = randf_range(0.5, 0.9)
		# 抛物线末端：起始速度方向 + 重力下拉
		var end_pos: Vector2 = pos + initial_vel * lifetime + Vector2(0, 320 * lifetime * lifetime)
		var tween := host.create_tween()
		tween.set_parallel(true)
		tween.tween_property(dot, "position", end_pos - Vector2(3, 3), lifetime)
		# alpha fade 在生命周期后 40% 段开始，前 40% 保持鲜艳
		tween.tween_property(dot, "color:a", 0.0, lifetime * 0.6).set_delay(lifetime * 0.4)
		tree.create_timer(lifetime + 0.05).timeout.connect(dot.queue_free)


# ─── floater ─────────────────────────────────────────────────────────────
# pos 处弹出文字，向上飘 rise px 同时 fade out。"+1" / "NEAR MISS" / "QUICK!"
# color 默认金色（高分感）；rise 默认 60px 适配 956×440 横屏。
static func floater(host: Node2D, pos: Vector2, text: String,
					color: Color = _GOLD, font_size: int = 28,
					rise: float = 60.0, lifetime: float = 0.8) -> void:
	if not is_instance_valid(host) or text.is_empty():
		return
	var tree := host.get_tree()
	if tree == null:
		return
	var label := Label.new()
	label.text = text
	label.modulate = color
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_outline_color", _OUTLINE)
	label.add_theme_constant_override("outline_size", 4)
	label.position = pos
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	host.add_child(label)
	var tween := host.create_tween()
	tween.set_parallel(true)
	tween.tween_property(label, "position:y", pos.y - rise, lifetime)
	tween.tween_property(label, "modulate:a", 0.0, lifetime * 0.55).set_delay(lifetime * 0.45)
	tree.create_timer(lifetime + 0.05).timeout.connect(label.queue_free)


# ─── combo_blast ─────────────────────────────────────────────────────────
# 屏幕中央大字 + 全屏白闪。combo 突破 / Fever 触发 / Frenzy 开始时用。
# duration 不含 punch 动画时间（~0.22s）和最终 fade（~0.3s），实际总时长
# 约 duration + 0.5s。
static func combo_blast(host: Node2D, text: String, color: Color = _GOLD,
						duration: float = 0.5, font_size: int = 96) -> void:
	if not is_instance_valid(host) or text.is_empty():
		return
	var tree := host.get_tree()
	if tree == null:
		return
	var screen: Vector2 = host.get_viewport_rect().size

	# ─ 全屏白闪 ────────────
	var flash := ColorRect.new()
	flash.color = Color(1, 1, 1, 0.55)
	flash.size = screen
	flash.mouse_filter = Control.MOUSE_FILTER_IGNORE
	host.add_child(flash)
	var ft := host.create_tween()
	ft.tween_property(flash, "color:a", 0.0, 0.18)
	tree.create_timer(0.2).timeout.connect(flash.queue_free)

	# ─ 居中大字 punch ─────
	var label := Label.new()
	label.text = text
	label.modulate = color
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_outline_color", _OUTLINE)
	label.add_theme_constant_override("outline_size", 6)
	# label 容器宽度跟屏幕，高度固定 200px。center alignment 把文字对齐到容器中心。
	var label_size: Vector2 = Vector2(screen.x, 200.0)
	label.size = label_size
	label.position = Vector2(0.0, (screen.y - label_size.y) * 0.5)
	label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	# scale 中心点设为 label 中心，否则 punch 时会偏向左上
	label.pivot_offset = label_size * 0.5
	label.scale = Vector2(0.3, 0.3)
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	host.add_child(label)

	var lt := host.create_tween()
	# Punch：scale 从 0.3 → 1.25 用 BACK 缓动（有"砸下来"的弹性感）
	lt.tween_property(label, "scale", Vector2(1.25, 1.25), 0.12)\
		.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	# 回弹到 1.0
	lt.tween_property(label, "scale", Vector2(1.0, 1.0), 0.10)
	# 停留
	var hold: float = max(0.0, duration - 0.4)
	if hold > 0.0:
		lt.tween_interval(hold)
	# fade out
	lt.tween_property(label, "modulate:a", 0.0, 0.3)
	tree.create_timer(duration + 0.5).timeout.connect(label.queue_free)
