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
	if not bgm_track.is_empty():
		AudioManager.play_bgm(bgm_track)


func _enter_game_over() -> void:
	state = GameState.GAME_OVER
	AudioManager.stop_bgm()
	AudioManager.play("level_fail")


# ═══════════════════════════════════════════════════════════════════════════════
#  DEFAULT IMPLEMENTATIONS (AI may override any of these by re-declaring them
#  inside the "AI IMPLEMENTATION ZONE" below)
# ═══════════════════════════════════════════════════════════════════════════════

# Decides when PLAYING ends. Default: lives <= 0. Override for time-based games,
# board-full games, win-condition games, etc.
func _check_game_over() -> bool:
	return lives <= 0


# Tap during PLAYING. Default: no-op. Override for tap-to-shoot, puzzle piece
# select, rhythm tap, etc. NOTE: tap-to-start / tap-to-restart are handled
# automatically by the state machine; only PLAYING-state taps come here.
func _handle_gameplay_tap(_pos: Vector2) -> void:
	pass


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
			tx = bridge.face_position.x * screen_size.x
			ty = bridge.face_position.y * screen_size.y
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
#  ║   - Use AudioManager.play(<sfx_id>) for SFX (IDs from system prompt)     ║
#  ║   - Do NOT call I18n.tr(...) directly; use _tr(...)                      ║
#  ║   - draw_colored_polygon's 2nd arg must be a single Color (not array)    ║
#  ║   - No inner classes (use Dictionary instead)                            ║
#  ╚══════════════════════════════════════════════════════════════════════════╝
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  AI ZONE — catch_v1 archetype (generated by RecipeCompiler).
#  Player controls a catcher at the bottom via body-pose / face X.
#  Drops give score; obstacles cost a life. Win at score 40.
#
#  Rendering: Sprite textures from `res://assets/{player,drop,obstacle}.png`
#  (Compiler copies them in from the fast-path catalog). If a texture is null
#  (asset missing), falls back to draw_circle with the catalog color.
# ═══════════════════════════════════════════════════════════════════════════════

func _init_game() -> void:
	game.catcher_x = screen_size.x * 0.5
	game.catcher_y = screen_size.y - 100.0
	game.drops = []
	game.obstacles = []
	game.spawn_timer = 0.6
	score = 0
	lives = 3
	bgm_track = "bgm_action"
	# Load sprites once; null-safe (_safe_load_texture returns null on miss)
	game.player_tex = _safe_load_texture("res://assets/player.png")
	game.drop_tex = _safe_load_texture("res://assets/drop.png")
	game.obstacle_tex = _safe_load_texture("res://assets/obstacle.png")


func _update_gameplay(delta: float) -> void:
	anim_timer += delta

	# Catcher follows body / face X with lerp
	var target: Vector2 = get_body_target(60.0)
	var cur_x: float = float(game.catcher_x)
	game.catcher_x = lerpf(cur_x, target.x, 12.0 * delta)

	# Spawn loop
	var st: float = float(game.spawn_timer) - delta
	if st <= 0.0:
		st = 0.417
		_spawn_falling_item()
	game.spawn_timer = st

	# Move falling items
	for d in game.drops:
		d["y"] = float(d["y"]) + float(d["v"]) * delta
	for o in game.obstacles:
		o["y"] = float(o["y"]) + float(o["v"]) * delta

	# Collision + cleanup
	var cx: float = float(game.catcher_x)
	var cy: float = float(game.catcher_y)
	var rad: float = 42.0
	var rad_sq: float = rad * rad

	var kept_drops: Array = []
	for d in game.drops:
		var dx_d: float = float(d["x"]) - cx
		var dy_d: float = float(d["y"]) - cy
		if dx_d * dx_d + dy_d * dy_d < rad_sq:
			score += 1
			_play_sfx("pickup")
		elif float(d["y"]) < screen_size.y + 80.0:
			kept_drops.append(d)
	game.drops = kept_drops

	var kept_obs: Array = []
	for o in game.obstacles:
		var dx_o: float = float(o["x"]) - cx
		var dy_o: float = float(o["y"]) - cy
		if dx_o * dx_o + dy_o * dy_o < rad_sq:
			lives -= 1
			_play_sfx("hit")
		elif float(o["y"]) < screen_size.y + 80.0:
			kept_obs.append(o)
	game.obstacles = kept_obs

	# Win-by-score
	if score >= 40:
		_play_sfx("pickup")
		state = GameState.GAME_OVER


func _draw_gameplay() -> void:
	var cx: float = float(game.catcher_x)
	var cy: float = float(game.catcher_y)
	var rad: float = 42.0
	var player_size: float = rad * 2.4

	# Catcher (player) — pulsing scale for liveliness
	var pulse: float = 1.0 + 0.06 * sin(anim_timer * 6.0)
	_draw_sprite_or_circle(game.player_tex, Vector2(cx, cy), player_size * pulse, rad, Color(0.880, 0.900, 0.950))

	# Drops (collectibles)
	for d in game.drops:
		var dx_f: float = float(d["x"])
		var dy_f: float = float(d["y"])
		_draw_sprite_or_circle(game.drop_tex, Vector2(dx_f, dy_f), 64.0, 22.0, Color(0.980, 0.850, 0.300))

	# Obstacles (hazards)
	for o in game.obstacles:
		var ox: float = float(o["x"])
		var oy: float = float(o["y"])
		_draw_sprite_or_circle(game.obstacle_tex, Vector2(ox, oy), 64.0, 22.0, Color(0.900, 0.850, 0.250))


# ─── Helpers (catch_v1) ──────────────────────────────────────────────────────

func _spawn_falling_item() -> void:
	var sx: float = randf_range(60.0, screen_size.x - 60.0)
	var v: float = randf_range(260.0, 520.0)
	if randf() < 0.300:
		game.obstacles.append({"x": sx, "y": -30.0, "v": v})
	else:
		game.drops.append({"x": sx, "y": -30.0, "v": v})


func _play_sfx(name: String) -> void:
	var am: Node = get_node_or_null("/root/AudioManager")
	if am != null and am.has_method("play"):
		am.play(name)


# Draw a centered texture at `pos` sized `sprite_size`. If texture is null
# (sprite asset missing), fall back to a colored circle of radius `circle_rad`.
func _draw_sprite_or_circle(tex: Texture2D, pos: Vector2, sprite_size: float, circle_rad: float, fallback: Color) -> void:
	if tex != null:
		var half: float = sprite_size * 0.5
		draw_texture_rect(tex, Rect2(pos.x - half, pos.y - half, sprite_size, sprite_size), false)
	else:
		draw_circle(pos, circle_rad, fallback)
