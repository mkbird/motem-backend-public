# scripts/chemistry.gd — Fast path systems-emergence engine
#
# 5×5 物品互动矩阵 + 13 种 outcome。物品带 tag（FIRE/WATER/GRASS/METAL/
# EXPLOSIVE/NEUTRAL）；碰撞时 strategy 调 `Chemistry.react(host, pos, ta, tb)`，
# Chemistry 负责：
#   1. 查表得到 outcome 名
#   2. 触发标准视觉效果（粒子 / 浮字 / 强反应加 combo_blast + Juice.impact）
#   3. 返回 score_mul / damage_mul 让 strategy 显式应用到自己的 state
#
# 设计：
# - Chemistry 不直接动 score / lives —— 各 archetype 的 state shape 不一样
#   （catch 的 game.drops 跟 dodger 的 game.hazards 完全不同），让 strategy
#   自己乘倍率比 Chemistry 包揽更清晰。
# - 对称矩阵：FIRE+WATER 跟 WATER+FIRE 一样，内部用排序后的 "lo_hi" 字符串
#   key 去重。
# - NEUTRAL 永远返 no_op：让 strategy 可以渐进式上 tag，没标 tag 的老 entity
#   也不出错（碰撞 → no_op → 走原 score/damage 不变路径）。
# - class_name = Chemistry 全局可见，不挂 autoload（同 Juice/Particles 隔离原则）。
#
# 矩阵（来源：docs/game-fastpath-v2-plan.md 的 Chemistry 表）：
#
#         FIRE       WATER      GRASS      METAL       EXPLOSIVE
# FIRE    bigger_fire extinguish spread_fire heat_metal chain_explode
# WATER              merge_pool grow_plant rust       wet_dud
# GRASS                         no_op      no_op      wrap_bomb
# METAL                                    clink_combo shrapnel
# EXPLOS                                               mega_boom
#
# Outcome 数据：
#   score_mul       —— 这次碰撞 score += base * score_mul
#   damage_mul      —— 这次碰撞 lives -= base_damage * damage_mul (0 = 取消)
#   particle_color  —— 默认粒子颜色（strategy 可覆盖）
#   particle_count  —— spawn 粒子数量（强反应更多）
#   floater_label   —— 浮字（英文短促，arcade 风格）
#   loud            —— 强反应？true → 额外 combo_blast + Juice.impact

extends RefCounted
class_name Chemistry

# Preload addon siblings —— class_name 跨文件 resolution 仅在 Editor 模式
# 成立，iOS headless runtime 必须显式 preload。Juice / Particles 在 chemistry.gd
# 内部被 react() 调用做反馈，缺这两行 iOS 上 parse error 整个游戏白屏。
const _Juice = preload("res://scripts/juice.gd")
const _Particles = preload("res://scripts/particles.gd")


enum Tag {
	NEUTRAL = 0,
	FIRE = 1,
	WATER = 2,
	GRASS = 3,
	METAL = 4,
	EXPLOSIVE = 5,
}


# ─── 矩阵 ────────────────────────────────────────────────────────────────────
# Key 用 "lo_hi" 字符串（lo / hi 是排序后的 Tag int），保证对称查询同一条目。
const _MATRIX: Dictionary = {
	# FIRE + ?
	"1_1": "bigger_fire",
	"1_2": "extinguish",
	"1_3": "spread_fire",
	"1_4": "heat_metal",
	"1_5": "chain_explode",
	# WATER + ?
	"2_2": "merge_pool",
	"2_3": "grow_plant",
	"2_4": "rust",
	"2_5": "wet_dud",
	# GRASS + ?
	"3_3": "no_op",
	"3_4": "no_op",
	"3_5": "wrap_bomb",
	# METAL + ?
	"4_4": "clink_combo",
	"4_5": "shrapnel",
	# EXPLOSIVE + ?
	"5_5": "mega_boom",
}


# ─── Outcome 数据表 ──────────────────────────────────────────────────────────
const _OUTCOMES: Dictionary = {
	"bigger_fire":   {"score_mul": 1.5, "damage_mul": 1.0, "particle_color": Color(1.0, 0.45, 0.05), "particle_count": 12, "floater_label": "BIGGER!", "loud": false},
	"extinguish":    {"score_mul": 1.2, "damage_mul": 0.0, "particle_color": Color(0.75, 0.88, 1.0), "particle_count": 10, "floater_label": "OUT!",    "loud": false},
	"spread_fire":   {"score_mul": 1.3, "damage_mul": 1.2, "particle_color": Color(1.0, 0.50, 0.10), "particle_count": 14, "floater_label": "SPREAD!", "loud": false},
	"heat_metal":    {"score_mul": 1.0, "damage_mul": 2.0, "particle_color": Color(1.0, 0.30, 0.10), "particle_count": 10, "floater_label": "HOT!",    "loud": false},
	"chain_explode": {"score_mul": 3.0, "damage_mul": 1.5, "particle_color": Color(1.0, 0.70, 0.05), "particle_count": 24, "floater_label": "CHAIN!",  "loud": true},
	"merge_pool":    {"score_mul": 1.2, "damage_mul": 0.0, "particle_color": Color(0.40, 0.70, 1.0), "particle_count": 8,  "floater_label": "MERGE!",  "loud": false},
	"grow_plant":    {"score_mul": 2.0, "damage_mul": 0.0, "particle_color": Color(0.35, 0.95, 0.40), "particle_count": 14, "floater_label": "GROW!",   "loud": false},
	"rust":          {"score_mul": 0.5, "damage_mul": 0.5, "particle_color": Color(0.70, 0.40, 0.20), "particle_count": 8,  "floater_label": "RUST",    "loud": false},
	"wet_dud":       {"score_mul": 1.0, "damage_mul": 0.0, "particle_color": Color(0.50, 0.70, 1.0), "particle_count": 10, "floater_label": "DUD!",    "loud": false},
	"wrap_bomb":     {"score_mul": 1.5, "damage_mul": 0.0, "particle_color": Color(0.40, 0.80, 0.30), "particle_count": 10, "floater_label": "WRAP!",   "loud": false},
	"clink_combo":   {"score_mul": 2.0, "damage_mul": 0.0, "particle_color": Color(1.0, 0.85, 0.25), "particle_count": 16, "floater_label": "CLINK!",  "loud": true},
	"shrapnel":      {"score_mul": 1.0, "damage_mul": 1.5, "particle_color": Color(0.60, 0.60, 0.60), "particle_count": 14, "floater_label": "SHARDS!", "loud": false},
	"mega_boom":     {"score_mul": 5.0, "damage_mul": 2.0, "particle_color": Color(1.0, 0.60, 0.10), "particle_count": 32, "floater_label": "BOOM!",   "loud": true},
	"no_op":         {"score_mul": 1.0, "damage_mul": 1.0, "particle_color": Color.WHITE,            "particle_count": 0,  "floater_label": "",        "loud": false},
}


# ─── 主入口 ────────────────────────────────────────────────────────────────
# 碰撞回调里调一行：
#   var fx = Chemistry.react(self, pos, item.tag, player.tag)
#   score += int(base_score * fx.score_mul)
#   lives -= int(base_damage * fx.damage_mul)
#
# 返回 Dictionary：{outcome, score_mul, damage_mul, particle_color, loud}
# host 必须是 Node2D（用于 spawn 粒子 + Juice.impact）。
static func react(host: Node2D, pos: Vector2,
				  a_tag: int, b_tag: int) -> Dictionary:
	var outcome_name: String = lookup(a_tag, b_tag)
	var data: Dictionary = _OUTCOMES.get(outcome_name, _OUTCOMES["no_op"])
	if not is_instance_valid(host):
		return data
	# 视觉效果
	var pcount: int = data["particle_count"]
	if pcount > 0:
		_Particles.splatter(host, pos, data["particle_color"], pcount)
	var flbl: String = data["floater_label"]
	if not flbl.is_empty():
		_Particles.floater(host, pos, flbl, data["particle_color"])
	# 强反应：屏幕级反馈（combo_blast 大字 + 冻帧 + 震动）
	if data.get("loud", false):
		_Particles.combo_blast(host, flbl, data["particle_color"], 0.4)
		_Juice.impact(host, 9.0, 0.08, 0.25)
	return data


# ─── 纯查表（无副作用，测试 / 预判用）───────────────────────────────────────
static func lookup(a_tag: int, b_tag: int) -> String:
	# NEUTRAL 永远 no_op：让 strategy 渐进式上 tag，老 entity 不出错
	if a_tag == Tag.NEUTRAL or b_tag == Tag.NEUTRAL:
		return "no_op"
	var lo: int = min(a_tag, b_tag)
	var hi: int = max(a_tag, b_tag)
	var key: String = "%d_%d" % [lo, hi]
	return _MATRIX.get(key, "no_op")
