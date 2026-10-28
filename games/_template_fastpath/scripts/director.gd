# scripts/director.gd — Fast path LLM-as-game-director runtime
#
# 跟 customizer 输出的 recipe.directors: list[str] 字段对接：strategy 在
# _init_game() 开头调 Director.setup(recipe.directors)，之后随时查 is_active()。
#
# 10 个 modifier 全部覆盖：
#
#   tense / relaxing / chaotic / puzzle
#       —— 难度 / 玩法风格调整。通过 difficulty_mods() 返回 {spawn_mul,
#          speed_mul, lives_delta}，strategy 应用到自己的 spawn timer / 速度 /
#          初始 lives。chemistry 启用情况通过 chemistry_enabled() 查。
#
#   gravity_flip
#       —— 重力反转。gravity_mul() 返 -1，strategy 在 spawn 时 vy *= mul。
#
#   slowmo
#       —— 全局慢动作。setup() 直接 Engine.time_scale = 0.7（puzzle 优先级
#          更高，会盖成 0.5）。strategy 无需配合。
#
#   magnet
#       —— 玩家吸引 drop。magnet_force(drop_pos, player_pos) 返回 Vector2
#          吸引力，strategy 在 _update_gameplay 里加到 drop 的速度向量。
#
#   wind
#       —— 周期性侧风。wind_force(elapsed) 返回水平加速度 vx 偏移（默认 60
#          px/s²，每 5 秒切换方向）。strategy 加到 drop.vx。
#
#   dark
#       —— 周期性黑屏闪烁。dark_overlay_alpha(elapsed) 返回 0-1 暗度（5 秒
#          周期，0.5 秒最黑）。strategy 在 _draw_gameplay 末尾 draw_rect
#          画蒙版。
#
#   boss_round
#       —— 每 20 秒一次 boss 事件窗口。boss_window(elapsed) 返回 true 表示
#          strategy 应该触发 boss-style spawn（具体怎么触发由 strategy 自己定义）。
#
# 设计：
# - 不用 token 替换 _template/scenes/main.gd —— 骨架保持干净，所有 director
#   逻辑走 runtime 查询。
# - 不用 enum，用 string —— 跟 customizer 的 recipe.directors: list[str] 字段
#   直接对接，compile 时无需 enum int 映射。
# - 跨进程残留处理：Godot 在 SwiftGodotKit 里是进程级单例（切游戏不重启进程），
#   static var _active 会跨游戏残留。setup() 第一行 _active.clear()，保证每局
#   重置；Engine.time_scale 在 setup() 里显式恢复 1.0（除非启用 slowmo/puzzle）。
# - PUZZLE > SLOWMO 优先级：两者都改 Engine.time_scale，PUZZLE 更慢（0.5 vs 0.7），
#   显式 if/elif 链避免冲突。
# - class_name = Director 全局可见，不挂 autoload（同 Juice/Particles/Chemistry/
#   ResultCard 隔离原则：slow path 没此文件 → 类不存在 → SDK 写代码不引用）。

extends RefCounted
class_name Director


# 已生效的 director 名 → true 字典（lower-case key）
static var _active: Dictionary = {}


# ─── 入口 ────────────────────────────────────────────────────────────────────
# strategy 在 _init_game() 开头调一次。flags 是 string Array，例如
# ["slowmo", "dark", "magnet"]。setup 会清空 _active、注册新 flags、立即应用
# 一次性副作用（Engine.time_scale 等）。
static func setup(flags: Array) -> void:
	_active.clear()
	for f in flags:
		var name: String = str(f).strip_edges().to_lower()
		if not name.is_empty():
			_active[name] = true
	# 一次性副作用 —— Engine.time_scale 立即设定，避免每帧查询。
	# PUZZLE 优先级最高（0.5），SLOWMO 次之（0.7），都没有则恢复 1.0。
	if is_active("puzzle"):
		Engine.time_scale = 0.5
	elif is_active("slowmo"):
		Engine.time_scale = 0.7
	else:
		Engine.time_scale = 1.0


static func is_active(name: String) -> bool:
	return _active.get(name.to_lower(), false)


# 清空（强烈推荐 game over 时调，防止 Engine.time_scale 跨游戏残留）。
static func clear() -> void:
	_active.clear()
	Engine.time_scale = 1.0


# ─── 难度 modifier 组合 ─────────────────────────────────────────────────────
# 返回 {"spawn_mul": float, "speed_mul": float, "lives_delta": int}
# strategy 在 _init_game / spawn 里乘上对应字段。
# TENSE / RELAXING / CHAOTIC 可叠加（乘子相乘 / lives_delta 相加）。
static func difficulty_mods() -> Dictionary:
	var spawn_mul: float = 1.0
	var speed_mul: float = 1.0
	var lives_delta: int = 0
	if is_active("tense"):
		spawn_mul *= 1.4
		speed_mul *= 1.2
		lives_delta -= 1
	if is_active("relaxing"):
		spawn_mul *= 0.7
		speed_mul *= 0.7
		lives_delta += 1
	if is_active("chaotic"):
		spawn_mul *= 1.3
	return {
		"spawn_mul": spawn_mul,
		"speed_mul": speed_mul,
		"lives_delta": lives_delta,
	}


# CHAOTIC 或 PUZZLE 启用时 chemistry 生效。strategy 在碰撞回调里查这个，
# true → 调 Chemistry.react(...)。
static func chemistry_enabled() -> bool:
	return is_active("chaotic") or is_active("puzzle")


# ─── 物理 modifier ──────────────────────────────────────────────────────────
# GRAVITY_FLIP: strategy 在 spawn 时 `velocity.y *= Director.gravity_mul()`。
static func gravity_mul() -> float:
	return -1.0 if is_active("gravity_flip") else 1.0


# MAGNET: 玩家吸引 drop。strategy 在 _update_gameplay 里：
#   var f = Director.magnet_force(drop_pos, player_pos)
#   drop.vx += f.x * delta
#   drop.vy += f.y * delta
# strength 默认 80（中等吸力）。距离越远，吸力越弱（线性衰减到 0 @ 400px）。
static func magnet_force(drop_pos: Vector2, player_pos: Vector2,
						 strength: float = 80.0) -> Vector2:
	if not is_active("magnet"):
		return Vector2.ZERO
	var diff: Vector2 = player_pos - drop_pos
	var dist: float = max(diff.length(), 1.0)
	var falloff: float = 1.0 - clampf(dist / 400.0, 0.0, 1.0)
	return diff.normalized() * strength * falloff


# WIND: 周期性侧风。strategy 在 _update_gameplay 里 `drop.vx += Director.wind_force(elapsed) * delta`。
# 每 5 秒切换一次方向，base 是峰值水平加速度（px/s²）。
static func wind_force(elapsed: float, base: float = 60.0) -> float:
	if not is_active("wind"):
		return 0.0
	var period: float = 5.0
	var phase: float = fmod(elapsed, period * 2.0)
	return base if phase < period else -base


# ─── 视觉 modifier ─────────────────────────────────────────────────────────
# DARK: 周期性黑屏闪烁。strategy 在 _draw_gameplay 末尾画蒙版：
#   var a = Director.dark_overlay_alpha(elapsed)
#   if a > 0.0:
#       draw_rect(Rect2(Vector2.ZERO, screen_size), Color(0, 0, 0, a))
# 5 秒周期，前 0.5 秒最黑（alpha=0.85），其余时间清亮。
static func dark_overlay_alpha(elapsed: float) -> float:
	if not is_active("dark"):
		return 0.0
	var t: float = fmod(elapsed, 5.0)
	if t < 0.5:
		return 0.85
	return 0.0


# ─── 节奏 modifier ──────────────────────────────────────────────────────────
# BOSS_ROUND: 每 period 秒一次窗口。strategy 调 `if Director.boss_window(elapsed): _trigger_boss()`。
# window 是窗口宽度（秒），strategy 应该用一个 flag 避免一个窗口内多次触发。
static func boss_window(elapsed: float, period: float = 20.0, window: float = 0.5) -> bool:
	if not is_active("boss_round"):
		return false
	var t: float = fmod(elapsed, period)
	return t < window
