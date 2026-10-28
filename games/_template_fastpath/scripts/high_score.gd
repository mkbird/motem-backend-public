# scripts/high_score.gd — Fast path 高分纪录持久化 + 实时破纪录反馈
#
# 每个 game_id 一个 user:// 文件，存最高分。strategy 在 _init_game 加载、在
# _update_gameplay 末尾调 tick() —— tick 内部检测破纪录瞬间、触发庆祝反馈
# (combo_blast + Juice.impact + time_dilation + 庆祝音)、节流写盘（1s 一次）。
#
# 调用方式（任一 v2 strategy）：
#   _init_game:
#       game.best_at_start = HighScore.load_best()
#       game.best_score = game.best_at_start
#       game.broke_record = false
#       game.last_save_t = -100.0
#
#   _update_gameplay 末尾:
#       HighScore.tick(self, score, game)
#
#   _draw_gameplay HUD:
#       if int(game.best_score) > 0:
#           draw_string(..., "Best: %d" % int(game.best_score), ...)

extends RefCounted
class_name HighScore

# Preload sibling addon helpers（同 chemistry.gd 设计原则）
const _Particles = preload("res://scripts/particles.gd")
const _Juice = preload("res://scripts/juice.gd")

const _SAVE_THROTTLE: float = 1.0   # 写盘节流（秒）


# ─── game_id 解析 ────────────────────────────────────────────────────────────
# 从 res://meta.json 读 game_id，作 user:// 文件名 key。每个游戏 zip 自带
# meta.json，game_id 在 compile 时由 fastpath/finalize 填进去。
static func _get_game_id() -> String:
	if not FileAccess.file_exists("res://meta.json"):
		return "unknown"
	var f := FileAccess.open("res://meta.json", FileAccess.READ)
	if f == null:
		return "unknown"
	var content: String = f.get_as_text()
	f.close()
	var parsed: Variant = JSON.parse_string(content)
	if typeof(parsed) == TYPE_DICTIONARY and parsed.has("game_id"):
		return String(parsed["game_id"])
	return "unknown"


static func _path_for_game() -> String:
	return "user://hs_%s.json" % _get_game_id()


# ─── 加载 / 保存 ────────────────────────────────────────────────────────────
static func load_best() -> int:
	var path := _path_for_game()
	if not FileAccess.file_exists(path):
		return 0
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null:
		return 0
	var content: String = f.get_as_text()
	f.close()
	var parsed: Variant = JSON.parse_string(content)
	if typeof(parsed) == TYPE_DICTIONARY and parsed.has("best"):
		return int(parsed["best"])
	return 0


static func save_best(score: int) -> void:
	var f := FileAccess.open(_path_for_game(), FileAccess.WRITE)
	if f == null:
		return
	f.store_string(JSON.stringify({"best": score}))
	f.close()


# ─── tick ────────────────────────────────────────────────────────────────────
# strategy 每帧（_update_gameplay 末尾）调一次。
# st 是 strategy 的 game 字典（by-reference），需要在 _init_game 初始化以下键：
#   best_at_start: int   —— 本局加载到的盘上 best（破纪录庆祝的"基线"）
#   best_score: int      —— 本局实时最高分（≥ best_at_start）
#   broke_record: bool   —— 本局是否已触发过破纪录庆祝（一次性 flag）
#   last_save_t: float   —— 上次写盘时刻（Time.get_ticks_msec / 1000）
static func tick(host: Node2D, current_score: int, st: Dictionary) -> void:
	if current_score <= int(st.get("best_score", 0)):
		return
	st["best_score"] = current_score
	# 第一次破纪录：只在 best_at_start > 0（有旧纪录）才庆祝
	if not bool(st.get("broke_record", false)) and int(st.get("best_at_start", 0)) > 0:
		st["broke_record"] = true
		_Particles.combo_blast(host, "NEW RECORD!", Color(1.0, 0.85, 0.20), 0.9, 96)
		_Juice.impact(host, 14.0, 0.15, 0.45)
		_Juice.time_dilation(host, 0.5, 0.4)
		# 庆祝音 —— 用 pickup 兜底，没有 AudioManager 也不抛错
		var am: Node = host.get_node_or_null("/root/AudioManager")
		if am != null and am.has_method("play"):
			am.play("pickup")
	# 节流写盘：score 涨了但距离上次写不到 1s 就先不写
	var now: float = Time.get_ticks_msec() / 1000.0
	if now - float(st.get("last_save_t", -100.0)) > _SAVE_THROTTLE:
		save_best(current_score)
		st["last_save_t"] = now
