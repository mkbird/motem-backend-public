extends Node2D

# ═══════════════════════════════════════════════════════════════════════════════
#  SKELETON main.gd — full game-shell plumbing pre-baked.
#  AI only implements the THREE required hooks (and optionally overrides 5 more)
#  at the bottom of this file. Search "AI IMPLEMENTATION ZONE" to find them.
# ═══════════════════════════════════════════════════════════════════════════════

# ── Pre-bound references (filled in _ready) ──
var game_font: Font
var bridge: Node                 # /root/InputBridge (body_lean_x/y, face_position, hand_gesture, body_detected, face_detected)
var i18n: Node                   # /root/I18n
var bg_sprite: Sprite2D          # background image; z_index=-100 (always behind _draw)
var _audio_mgr: Node             # /root/AudioManager — looked up by path, NOT by global identifier
                                 # (iOS embedded Godot doesn't resolve autoload names at parse time)

# ── Screen (read at runtime; do NOT hardcode) ──
var screen_size: Vector2

# ── HUD safe zones (host overlays 50×50 buttons at top-left/top-right) ──
const HUD_SAFE_LEFT: float  = 130.0
const HUD_SAFE_RIGHT: float = 130.0
const HUD_SAFE_TOP: float   = 10.0

# ── Game state machine ──
enum GameState { START, PLAYING, GAME_OVER }
var state: int = GameState.START

# ── Common pre-declared game state (AI may use directly) ──
var score: int = 0               # incremented by AI in _update_gameplay
var lives: int = 3               # decremented when player gets hit; 0 → GAME_OVER (by default)
var anim_timer: float = 0.0      # +=delta every frame; use sin(anim_timer * N) for pulsing
var game: Dictionary = {}        # AI's free-form scratchpad for game-specific state

# ── BGM track for the current scene (AI sets in _init_game if desired) ──
var bgm_track: String = ""       # e.g. "bgm_action"; empty = no BGM


# ═══════════════════════════════════════════════════════════════════════════════
#  PLUMBING (do not modify — pre-bound for AI's hooks)
# ═══════════════════════════════════════════════════════════════════════════════

func _ready() -> void:
	game_font = _load_game_font()
	bridge = get_node_or_null("/root/InputBridge")
	i18n = get_node_or_null("/root/I18n")
	_audio_mgr = get_node_or_null("/root/AudioManager")
	screen_size = get_viewport_rect().size

	# Background sprite — fullscreen behind everything. z_index=-100 ensures
	# it draws BELOW the parent Node2D's _draw() output (Godot 2D paints
	# parent _draw FIRST, then children; z_index overrides this ordering).
	bg_sprite = Sprite2D.new()
	bg_sprite.texture = _safe_load_texture("res://assets/background.png")
	bg_sprite.centered = false
	bg_sprite.position = Vector2.ZERO
	bg_sprite.z_index = -100
	if bg_sprite.texture != null:
		var tex_size: Vector2 = bg_sprite.texture.get_size()
		if tex_size.x > 0.0 and tex_size.y > 0.0:
			bg_sprite.scale = Vector2(screen_size.x / tex_size.x, screen_size.y / tex_size.y)
	add_child(bg_sprite)


func _process(delta: float) -> void:
	anim_timer += delta
	if state == GameState.PLAYING:
		_update_gameplay(delta)
		if _check_game_over():
			_enter_game_over()
	queue_redraw()


func _input(event: InputEvent) -> void:
	if not (event is InputEventScreenTouch and event.pressed):
		return
	# Modal overlay 契约：fast path 的 ResultCard / Challenge 等 UI 在弹出时
	# 把自己加入 "modal_overlay" group，骨架检测到任一节点在此 group 时跳过
	# 全局 tap-to-restart / tap-to-start，避免按钮还没接到事件就被骨架劫持
	# 触发 _enter_playing()。slow path 不创建此 group，行为完全不变。
	if get_tree().get_first_node_in_group("modal_overlay") != null:
		return
	var pos: Vector2 = (event as InputEventScreenTouch).position
	match state:
		GameState.START:
			_enter_playing()
		GameState.PLAYING:
			_handle_gameplay_tap(pos)
		GameState.GAME_OVER:
			_enter_playing()


func _draw() -> void:
	match state:
		GameState.START:
			_draw_start_screen()
		GameState.PLAYING:
			_draw_gameplay()
			_draw_hud()
		GameState.GAME_OVER:
			_draw_gameplay()
			_draw_hud()
			_draw_game_over_screen()


# ── State transitions ──
func _enter_playing() -> void:
	# Reset common state
	score = 0
	lives = 3
	game.clear()
	# AI's per-game setup
	_init_game()
	state = GameState.PLAYING
	if bridge != null:
		bridge.gameplay_active = true
	if not bgm_track.is_empty():
		_audio_play_bgm(bgm_track)


func _enter_game_over() -> void:
	state = GameState.GAME_OVER
	if bridge != null:
		bridge.gameplay_active = false
	_audio_stop_bgm()
	_audio_play("level_fail")


# /root/AudioManager indirection — iOS embedded Godot does NOT register
# autoload names as global identifiers, so a bare `AudioManager.play("x")`
# parse-errors with "Identifier not declared in the current scope". Going
# through the node by path is the portable form; strategy code should call
# these helpers, not `AudioManager.*` directly.
func _audio_play(sfx_id: String) -> void:
	if _audio_mgr != null and _audio_mgr.has_method("play"):
		_audio_mgr.call("play", sfx_id)

func _audio_play_bgm(track_id: String) -> void:
	if _audio_mgr != null and _audio_mgr.has_method("play_bgm"):
		_audio_mgr.call("play_bgm", track_id)

func _audio_stop_bgm() -> void:
	if _audio_mgr != null and _audio_mgr.has_method("stop_bgm"):
		_audio_mgr.call("stop_bgm")


# ═══════════════════════════════════════════════════════════════════════════════
#  DEFAULT IMPLEMENTATIONS (AI may override any of these by re-declaring them
#  inside the "AI IMPLEMENTATION ZONE" below)
# ═══════════════════════════════════════════════════════════════════════════════

# Decides when PLAYING ends. Default: lives <= 0. Override for time-based games,
# board-full games, win-condition games, etc.
func _check_game_over() -> bool:
	return lives <= 0


# Tap during PLAYING. Default: dispatch to `_on_tap(pos)` if AI ZONE defined
# one; else no-op. This dispatch avoids the GDScript "no override" pitfall:
# AI ZONE can safely declare `func _on_tap(pos: Vector2)` without colliding
# with this plumbing function. NOTE: tap-to-start / tap-to-restart are
# handled automatically by the state machine; only PLAYING-state taps here.
func _handle_gameplay_tap(_pos: Vector2) -> void:
	if has_method("_on_tap"):
		call("_on_tap", _pos)


# HUD text on top-center (away from corner safe zones). Default shows
# "Score / Lives". Override for combo / time / accuracy / level / etc.
func _get_hud_text() -> String:
	return _tr("score", {"score": score}) + "   " + _tr("lives", {"lives": lives})


# Start screen. Default: title + pulse-animated "tap to start" hint.
# Override for difficulty select / character pick / level menu.
func _draw_start_screen() -> void:
	var title: String = _tr("title")
	var ts: int = 56
	var tw: Vector2 = game_font.get_string_size(title, HORIZONTAL_ALIGNMENT_LEFT, -1, ts)
	var ty: float = screen_size.y * 0.36
	var pulse: float = 0.85 + sin(anim_timer * 1.8) * 0.15
	draw_string(game_font, Vector2((screen_size.x - tw.x) * 0.5, ty), title,
		HORIZONTAL_ALIGNMENT_LEFT, -1, ts, Color(1.0, 1.0, 1.0, pulse))
	var hint: String = _tr("start_hint")
	var hs: int = 30
	var hw: Vector2 = game_font.get_string_size(hint, HORIZONTAL_ALIGNMENT_LEFT, -1, hs)
	var blink: float = 0.55 + sin(anim_timer * 3.0) * 0.4
	draw_string(game_font, Vector2((screen_size.x - hw.x) * 0.5, screen_size.y * 0.6), hint,
		HORIZONTAL_ALIGNMENT_LEFT, -1, hs, Color(1.0, 1.0, 1.0, blink))


# Game over screen. Default: dim + "Game Over" + final score + retry hint.
# Override for win/lose split / multi-tier leaderboard etc.
func _draw_game_over_screen() -> void:
	draw_rect(Rect2(0.0, 0.0, screen_size.x, screen_size.y), Color(0.0, 0.0, 0.0, 0.55))
	var title: String = _tr("game_over")
	var ts: int = 60
	var tw: Vector2 = game_font.get_string_size(title, HORIZONTAL_ALIGNMENT_LEFT, -1, ts)
	draw_string(game_font, Vector2((screen_size.x - tw.x) * 0.5, screen_size.y * 0.38), title,
		HORIZONTAL_ALIGNMENT_LEFT, -1, ts, Color(1.0, 0.85, 0.3))
	var fs: String = _tr("score", {"score": score})
	var fz: int = 36
	var fw: Vector2 = game_font.get_string_size(fs, HORIZONTAL_ALIGNMENT_LEFT, -1, fz)
	draw_string(game_font, Vector2((screen_size.x - fw.x) * 0.5, screen_size.y * 0.52), fs,
		HORIZONTAL_ALIGNMENT_LEFT, -1, fz, Color(1.0, 1.0, 1.0))
	var hint: String = _tr("retry_hint")
	var hz: int = 28
	var hw: Vector2 = game_font.get_string_size(hint, HORIZONTAL_ALIGNMENT_LEFT, -1, hz)
	var blink: float = 0.55 + sin(anim_timer * 3.0) * 0.4
	draw_string(game_font, Vector2((screen_size.x - hw.x) * 0.5, screen_size.y * 0.66), hint,
		HORIZONTAL_ALIGNMENT_LEFT, -1, hz, Color(1.0, 1.0, 1.0, blink))


# HUD (during PLAYING and GAME_OVER). Default draws text from _get_hud_text() at
# top-center (HUD_SAFE_TOP). Override only if you need a graphical HUD layout.
func _draw_hud() -> void:
	var txt: String = _get_hud_text()
	if txt.is_empty():
		return
	var hs: int = 26
	var tw: Vector2 = game_font.get_string_size(txt, HORIZONTAL_ALIGNMENT_LEFT, -1, hs)
	var x: float = (screen_size.x - tw.x) * 0.5
	# HUD shadow for legibility on any background color
	draw_string(game_font, Vector2(x + 1, HUD_SAFE_TOP + 31), txt,
		HORIZONTAL_ALIGNMENT_LEFT, -1, hs, Color(0.0, 0.0, 0.0, 0.7))
	draw_string(game_font, Vector2(x, HUD_SAFE_TOP + 30), txt,
		HORIZONTAL_ALIGNMENT_LEFT, -1, hs, Color(1.0, 1.0, 1.0))


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS (use these instead of raw Godot APIs — they're iOS-mobile-safe)
# ═══════════════════════════════════════════════════════════════════════════════

# Null-safe texture loader. iOS Godot may return null if asset missing / not
# imported. ALWAYS use this instead of bare `load(...)` for textures.
func _safe_load_texture(path: String) -> Texture2D:
	var res = load(path)
	if res is Texture2D:
		return res
	return null


# i18n wrapper. AI MUST use this for every player-facing string.
func _tr(key: String, params: Dictionary = {}) -> String:
	if i18n != null and i18n.has_method("translate"):
		return i18n.translate(key, params)
	return key


# Body-pose / face-position → target screen position (with margin clamping).
# Returns the position the player object should be tracking this frame.
# For position-control games (dodger / shooter / runner-with-lanes), call this
# in _update_gameplay and lerp toward the result:
#
#   var target := get_body_target(40.0)
#   game.player_pos = game.player_pos.lerp(target, 12.0 * delta)
func get_body_target(margin: float = 40.0) -> Vector2:
	var cx: float = screen_size.x * 0.5
	var cy: float = screen_size.y * 0.5
	var tx: float = cx
	var ty: float = cy
	if bridge != null:
		if bridge.body_detected:
			tx = cx + bridge.body_lean_x * cx * 1.15
			ty = cy - bridge.body_lean_y * cy * 1.15
		elif bridge.face_detected:
			# Face position naturally spans only ~0.35-0.65 (head can't physically
			# rotate enough to reach face_position 0 or 1 while staying in camera
			# frame). Scale 2× around center 0.5 so user can reach screen edges.
			# Convention copied from successful old game ai_1777535277_165fa2 (火球掌控).
			var fx: float = (bridge.face_position.x - 0.5) * 2.0 + 0.5
			var fy: float = (bridge.face_position.y - 0.5) * 2.0 + 0.5
			tx = fx * screen_size.x
			ty = fy * screen_size.y
	tx = clampf(tx, margin, screen_size.x - margin)
	ty = clampf(ty, margin, screen_size.y - margin)
	return Vector2(tx, ty)


# CJK-compatible font loader. DO NOT MODIFY. iOS host injects
# NotoSansSC-Regular.otf into fonts/ before launch.
func _load_game_font() -> Font:
	var theme_res = load("res://fonts/default_theme.tres")
	if theme_res and theme_res is Theme:
		var f = (theme_res as Theme).default_font
		if f:
			return f
	var ff = FontFile.new()
	var font_bytes = FileAccess.get_file_as_bytes("res://fonts/NotoSansSC-Regular.otf")
	if font_bytes.size() > 0:
		ff.data = font_bytes
		return ff
	return ThemeDB.fallback_font


# ═══════════════════════════════════════════════════════════════════════════════
#  ╔══════════════════════════════════════════════════════════════════════════╗
#  ║                    🎮 AI IMPLEMENTATION ZONE  🎮                          ║
#  ║                                                                          ║
#  ║  Fill in the THREE required hooks below. The skeleton handles            ║
#  ║  everything else: tap-to-start, state transitions, HUD, background,      ║
#  ║  start/game-over screens, i18n, font, body-pose mapping.                 ║
#  ║                                                                          ║
#  ║  Optional override: re-declare any of                                    ║
#  ║    _check_game_over, _handle_gameplay_tap, _get_hud_text,                ║
#  ║    _draw_start_screen, _draw_game_over_screen, _draw_hud                 ║
#  ║  here. Same `func name(...)` signature; GDScript will use yours.         ║
#  ║                                                                          ║
#  ║  Already enforced for you:                                               ║
#  ║   ✓ background.png loaded with z_index=-100 and null-safe                ║
#  ║   ✓ font / i18n / bridge / screen_size pre-bound                         ║
#  ║   ✓ HUD safe zones built into _draw_hud                                  ║
#  ║   ✓ bgm_track auto-plays via AudioManager when entering PLAYING          ║
#  ║                                                                          ║
#  ║  Rules:                                                                  ║
#  ║   - Use _tr("key", {params}) for ALL player-facing UI text               ║
#  ║   - Use _safe_load_texture("res://...") for any extra textures           ║
#  ║   - Use _audio_play(<sfx_id>) for SFX (IDs from system prompt)           ║
#  ║   - Do NOT call I18n.tr(...) directly; use _tr(...)                      ║
#  ║   - draw_colored_polygon's 2nd arg must be a single Color (not array)    ║
#  ║   - No inner classes (use Dictionary instead)                            ║
#  ╚══════════════════════════════════════════════════════════════════════════╝
# ═══════════════════════════════════════════════════════════════════════════════


# REQUIRED: called once when player taps to start (and again on retry).
# Initialise game-specific state in `game` Dictionary, e.g.:
#   game.player_pos = Vector2(screen_size.x * 0.5, screen_size.y * 0.85)
#   game.enemies = []
#   game.spawn_timer = 0.0
#   bgm_track = "bgm_action"     # optional: BGM track to loop during PLAYING
func _init_game() -> void:
	pass  # TODO(AI)


# REQUIRED: per-frame logic. Called only during PLAYING.
# Increment `score` here on player gain; decrement `lives` on damage.
# Spawn / move / collide objects via `game.*`.
func _update_gameplay(delta: float) -> void:
	pass  # TODO(AI)


# REQUIRED: render game objects. Called during PLAYING and GAME_OVER.
# Use draw_circle / draw_rect / draw_colored_polygon / draw_string for visuals.
# Background and HUD are handled by the skeleton.
func _draw_gameplay() -> void:
	pass  # TODO(AI)
