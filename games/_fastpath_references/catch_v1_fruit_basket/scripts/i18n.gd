extends Node

## Localization runtime for AI-generated games.
## This file is fixed template infrastructure and should not be modified by AI.

signal locale_changed(locale: String)

const SUPPORTED_LOCALES := ["en", "zh-Hans"]

var locale: String = "en"
var strings: Dictionary = {}
var _reload_pending: bool = false


func _ready() -> void:
	_load_strings()
	_load_locale_file()
	print("[I18n] _ready done. locale=", locale, " strings=", strings.size())

	# I18n is a process-level autoload: its `_ready` fires exactly once, when
	# the FIRST game launches. Subsequent game launches on the same engine
	# process only swap the scene tree, which means the cached `strings` dict
	# remains stuck on the first game's translations. We listen for scene
	# tree changes and reload strings + user_locale.json from disk so the
	# second game's `_tr("title")` picks up its own i18n_strings.gd.
	var t := get_tree()
	if t != null:
		t.tree_changed.connect(_on_tree_changed)


func set_locale(value: String) -> void:
	# Reload strings alongside every set_locale call — cheap insurance in
	# case tree_changed didn't fire (e.g. for a same-scene locale switch).
	_load_strings()
	var normalized := normalize_locale(value)
	print("[I18n] set_locale(", value, ") → ", normalized, " (was ", locale, ") strings=", strings.size())
	if locale == normalized:
		return
	locale = normalized
	locale_changed.emit(locale)


func get_locale() -> String:
	return locale


func normalize_locale(value: String) -> String:
	var lang := value.strip_edges().to_lower()
	if lang.is_empty():
		return "en"
	if lang.begins_with("en"):
		return "en"
	if lang.contains("zh"):
		return "zh-Hans"
	return "en"


## Named `translate` (not `tr`) to avoid colliding with Godot's built-in
## `Object.tr(StringName, StringName)`, which otherwise breaks parser type
## checking when this runtime is referenced through a `Node` typed variable.
func translate(key: String, params: Dictionary = {}) -> String:
	var entry: Variant = strings.get(key, null)
	if typeof(entry) != TYPE_DICTIONARY:
		return _format_params(key, params)

	var text: String = entry.get(locale, entry.get("en", key))
	return _format_params(text, params)


func _load_strings() -> void:
	# `CACHE_MODE_IGNORE` forces ResourceLoader to re-read the file from disk.
	# Without this, a second game loaded on the same process would get the
	# first game's `i18n_strings.gd` from Godot's resource cache.
	var script := ResourceLoader.load(
		"res://scripts/i18n_strings.gd",
		"",
		ResourceLoader.CACHE_MODE_IGNORE
	)
	if script == null:
		strings = {}
		return
	var string_holder = script.new()
	if "STRINGS" in string_holder:
		strings = string_holder.STRINGS
	else:
		strings = {}


func _load_locale_file() -> void:
	var path := "res://user_locale.json"
	if not FileAccess.file_exists(path):
		print("[I18n] ", path, " not found — keeping locale ", locale)
		return
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		print("[I18n] failed to open ", path)
		return
	var text := file.get_as_text()
	print("[I18n] read ", path, ": ", text)
	var parsed: Variant = JSON.parse_string(text)
	if typeof(parsed) == TYPE_DICTIONARY:
		set_locale(str(parsed.get("locale", "")))


func _on_tree_changed() -> void:
	# Debounce: tree_changed fires on every add/remove node during scene
	# construction. We coalesce to a single deferred reload per run-loop.
	if _reload_pending:
		return
	_reload_pending = true
	call_deferred("_do_deferred_reload")


func _do_deferred_reload() -> void:
	_reload_pending = false
	_load_strings()
	_load_locale_file()
	print("[I18n] deferred reload after scene change. locale=", locale, " strings=", strings.size())


func _format_params(text: String, params: Dictionary) -> String:
	var result := text
	for key in params.keys():
		result = result.replace("{" + str(key) + "}", str(params[key]))
	return result
