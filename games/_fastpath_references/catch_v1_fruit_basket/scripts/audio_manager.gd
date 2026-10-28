extends Node

## Audio runtime for AI-generated games.
## This file is fixed template infrastructure and should NOT be modified by AI.
##
## API:
##   AudioManager.play(name: String) -> void          # fire-and-forget SFX
##   AudioManager.play_bgm(name: String) -> void      # start looping BGM
##   AudioManager.stop_bgm() -> void
##
## Mute / unmute is controlled by the iOS host app, which writes the `muted`
## property directly via the SwiftGodot SceneTree (same pattern as how iOS
## drives `InputBridge.body_lean_x`). The setter handles BGM pause/resume
## automatically — game code does not need to call `set_muted` itself.
##
## Audio files are looked up at "res://audio/<name>.ogg". Missing files log
## a warning and silently no-op so a partial game zip can still launch.

const SFX_PLAYER_COUNT: int = 8     ## simultaneous SFX channels (poolsize)
const SFX_VOLUME_DB: float = -3.0   ## per-SFX volume，比原始 ogg 略降避免刺耳
const BGM_VOLUME_DB: float = -12.0  ## BGM 默认压低，不抢 SFX 风头

@export var muted: bool = false:
	set(value):
		if muted == value:
			return
		muted = value
		_apply_mute()

var _sfx_pool: Array = []           ## Array[AudioStreamPlayer]
var _sfx_next: int = 0
var _bgm_player: AudioStreamPlayer = null
var _bgm_current: String = ""


func _ready() -> void:
	# SFX channel pool. Single shared bus.
	for i in range(SFX_PLAYER_COUNT):
		var p := AudioStreamPlayer.new()
		p.bus = "Master"
		p.volume_db = SFX_VOLUME_DB
		add_child(p)
		_sfx_pool.append(p)

	# Dedicated BGM player.
	_bgm_player = AudioStreamPlayer.new()
	_bgm_player.bus = "Master"
	_bgm_player.volume_db = BGM_VOLUME_DB
	add_child(_bgm_player)
	print("[AudioManager] _ready: ", SFX_PLAYER_COUNT, " sfx + 1 bgm players, muted=", muted, " sfx_db=", SFX_VOLUME_DB, " bgm_db=", BGM_VOLUME_DB)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

func play(name: String) -> void:
	if muted:
		print("[AudioManager] play(", name, ") skipped: muted")
		return
	if name.is_empty():
		return
	var stream := _load_stream(name)
	if stream == null:
		push_warning("[AudioManager] missing SFX '%s' (audio/%s.ogg not found)" % [name, name])
		print("[AudioManager] play(", name, ") FAILED: stream is null")
		return
	# Round-robin through the pool so concurrent SFX don't clip each other.
	var p: AudioStreamPlayer = _sfx_pool[_sfx_next]
	_sfx_next = (_sfx_next + 1) % _sfx_pool.size()
	p.stream = stream
	p.play()
	print("[AudioManager] play(", name, ") ✓ slot=", _sfx_next, " bus=", p.bus, " vol=", p.volume_db)


func play_bgm(name: String) -> void:
	if name.is_empty():
		return
	if _bgm_current == name and _bgm_player.playing:
		return  # already playing
	_bgm_current = name
	if muted:
		print("[AudioManager] play_bgm(", name, ") queued: muted")
		return
	var stream := _load_stream(name)
	if stream == null:
		push_warning("[AudioManager] missing BGM '%s' (audio/%s.ogg not found)" % [name, name])
		print("[AudioManager] play_bgm(", name, ") FAILED: stream is null")
		return
	# Mark stream as looping if the resource didn't already.
	if stream is AudioStreamOggVorbis:
		stream.loop = true
	_bgm_player.stream = stream
	_bgm_player.play()
	print("[AudioManager] play_bgm(", name, ") ✓ playing=", _bgm_player.playing, " bus=", _bgm_player.bus)


func stop_bgm() -> void:
	_bgm_current = ""
	if _bgm_player != null:
		_bgm_player.stop()


## 立刻打断所有正在播放的 SFX（不影响 BGM）。退出游戏时调，避免最后一发
## 子弹声等残留。游戏逻辑被暂停后游戏不会再调 play()，所以一次调用就够。
##
## 不再用 `if p.playing` 守卫：play() 是异步的，刚 dispatch 还没真正起声时
## `playing == false`，跳过 stop 会让那枚 SFX 紧跟在 stop 之后播出来。
## 无差别 stop() 对没在播的 player 是 no-op，安全。
func stop_all_sfx() -> void:
	for p in _sfx_pool:
		if p != null:
			p.stop()


## 一键停所有声音（BGM + SFX）。
func stop_all() -> void:
	stop_bgm()
	stop_all_sfx()


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------

func _apply_mute() -> void:
	if _bgm_player == null:
		return
	if muted:
		_bgm_player.stream_paused = true
	else:
		# Resume BGM if one was queued but not currently playing.
		if _bgm_current != "" and not _bgm_player.playing:
			play_bgm(_bgm_current)
		else:
			_bgm_player.stream_paused = false


func _load_stream(name: String) -> AudioStream:
	# Godot 4 normally requires `.import` companion files (editor-generated)
	# for .ogg / .wav assets, otherwise ResourceLoader can't find them. Since
	# our games ship loose audio files with no import metadata, we use the
	# `load_from_file()` static APIs that bypass the import system entirely
	# and decode the raw bytes from disk.
	var ogg_path: String = "res://audio/" + name + ".ogg"
	if FileAccess.file_exists(ogg_path):
		var stream := AudioStreamOggVorbis.load_from_file(ogg_path)
		if stream != null:
			return stream
		print("[AudioManager] _load_stream(", name, "): ", ogg_path, " load_from_file failed")
	else:
		print("[AudioManager] _load_stream(", name, "): ", ogg_path, " does NOT exist")
	return null
