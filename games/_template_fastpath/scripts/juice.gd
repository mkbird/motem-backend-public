# scripts/juice.gd — Fast path juice helpers
#
# Hitstop / time dilation / screen shake 三个原子工具 + 一个 impact 复合工具。
# 由 fast path strategy 的 AI ZONE 代码在命中 / 失败 / combo 触发时调用：
#
#     Juice.impact(self, 6.0)            # 命中复合反馈：冻 60ms + 抖 200ms
#     Juice.hitstop(self, 0.1)           # 关键瞬间冻帧
#     Juice.time_dilation(self, 0.3, 0.5)  # near-miss / combo blast 慢动作
#     Juice.screen_shake(self, 8.0, 0.3)   # 爆炸 / 失败震动
#
# class_name = Juice 让全局可见，不需要 preload 也不挂 autoload。slow path
# 没有这个文件 → Juice 类不存在 → SDK 写的代码自然不会引用 → 完美隔离。
#
# 实现选择：
# - 用 Engine.time_scale = 0 实现冻帧。理由：get_tree().paused = true 会停
#   InputBridge / AudioManager / 任何 PROCESS_MODE_INHERIT 的 autoload，
#   音乐戛然而止 + bridge 不再推数据 → bgm 听感破坏 + 体感丢帧。time_scale
#   只缩 delta，autoload 的 _process 该跑还跑，AudioStreamPlayer 不受影响。
# - tree.create_timer(..., ignore_time_scale=true)：第 4 参数让 timer 走 real
#   time，否则 time_scale = 0 时 timer 永远不会 timeout，hitstop 会卡死整个
#   场景。这是 Godot 4 提供的关键 escape hatch。
# - screen_shake 偏移 host (main.gd, Node2D) 自己的 position。skeleton main.gd
#   从不动自己的 position（已 grep 确认），shake 后 restore 到原值 = ZERO 安全。
# - 静态 _time_fx_active guard：hitstop 重入会把 time_scale 设回错误的"prev"
#   （第二次进入时 prev 已经是 0），最简单的解：一时刻最多 1 个时间相关 FX。

extends RefCounted
class_name Juice


# 同时刻最多一个 time-scale 类 FX（hitstop / time_dilation 共用此锁）。
# screen_shake 不动 time_scale，可与其它 FX 并发。
static var _time_fx_active: bool = false


# 冻帧 —— 击中 / 失败 / combo 突破时调用。典型值 0.06-0.15s。
# 调用方约定 `await` 可选；fire-and-forget 也行（strategy 不需要等冻完）。
static func hitstop(host: Node, duration: float = 0.1) -> void:
	if _time_fx_active:
		return
	if not is_instance_valid(host):
		return
	var tree := host.get_tree()
	if tree == null:
		return
	_time_fx_active = true
	var prev: float = Engine.time_scale
	Engine.time_scale = 0.0
	# 第 4 参数 ignore_time_scale=true：timer 用 real time，time_scale=0
	# 时仍能 timeout。否则整场景永远冻住。
	var timer := tree.create_timer(duration, true, false, true)
	await timer.timeout
	Engine.time_scale = prev
	_time_fx_active = false


# 慢动作 —— near-miss / combo blast / boss 出场。典型 scale=0.25 dur=0.5s。
# duration 是 real time（受 ignore_time_scale 保护），所以 scale=0.1 时
# 玩家观感是 0.5s 慢镜头，不是 5s。
static func time_dilation(host: Node, scale: float = 0.3, duration: float = 0.5) -> void:
	if _time_fx_active:
		return
	if not is_instance_valid(host):
		return
	var tree := host.get_tree()
	if tree == null:
		return
	_time_fx_active = true
	Engine.time_scale = scale
	var timer := tree.create_timer(duration, true, false, true)
	await timer.timeout
	Engine.time_scale = 1.0
	_time_fx_active = false


# 屏幕震动 —— host 通常是 main.gd 自己（Node2D 根节点），震动通过 position
# 偏移实现。intensity 单位 px，duration 单位 real-time 秒（用 process_frame
# 信号驱动，自然受 time_scale 影响 —— 这是有意为之：搭配 hitstop 时希望震动
# "等到 time 恢复"才开始震）。
static func screen_shake(host: Node2D, intensity: float = 8.0, duration: float = 0.3) -> void:
	if not is_instance_valid(host):
		return
	var tree := host.get_tree()
	if tree == null:
		return
	var orig: Vector2 = host.position
	var elapsed: float = 0.0
	while elapsed < duration and is_instance_valid(host):
		var t_norm: float = elapsed / duration
		var falloff: float = 1.0 - t_norm
		host.position = orig + Vector2(
			randf_range(-intensity, intensity) * falloff,
			randf_range(-intensity, intensity) * falloff,
		)
		await tree.process_frame
		elapsed += tree.root.get_process_delta_time()
	if is_instance_valid(host):
		host.position = orig


# 复合反馈 —— 一次性触发 hitstop + screen shake，strategy AI ZONE 调一行
# 就有完整的"打击感"。命中 drop / 切水果 / 拍中 mole 都用它，不要单独
# 调 hitstop + screen_shake（容易顺序写反，先 shake 再 stop 感觉很怪）。
static func impact(host: Node2D, intensity: float = 6.0,
				   stop_s: float = 0.06, shake_s: float = 0.2) -> void:
	# 先 await hitstop 冻一下，再 fire-and-forget shake。顺序很关键：
	# 冻帧让玩家"看清"打中的瞬间，然后震动从那个时刻才开始 —— 经典街机打击感。
	if stop_s > 0.0:
		await Juice.hitstop(host, stop_s)
	if shake_s > 0.0:
		Juice.screen_shake(host, intensity, shake_s)  # 不 await，让 strategy 继续
