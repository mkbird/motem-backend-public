# AI Game Project (Skeleton hook mode)

## Architecture

`scenes/main.gd` is a 300-line **skeleton** that already implements the outer
shell of any 2D game:

- State machine (START → PLAYING → GAME_OVER)
- Touch routing (tap-to-start / tap-to-restart / mid-game tap callback)
- HUD draw (with top-center safe zone)
- Background sprite with `z_index=-100` (null-safe)
- Font loading (CJK-compatible)
- i18n routing
- Body-pose / face-tracking helper (`get_body_target`)
- BGM auto-start when `bgm_track` is set

You fill in the gameplay logic via 3 required hooks + 5 optional overrides
in the "AI IMPLEMENTATION ZONE" at the bottom of `scenes/main.gd`.

## Required hooks

| Hook | Purpose |
|---|---|
| `_init_game()` | Set up game-specific state (called on tap-to-start and retry) |
| `_update_gameplay(delta)` | Per-frame logic during PLAYING |
| `_draw_gameplay()` | Render game objects (background + HUD handled by skeleton) |

## Optional override hooks

| Hook | Default | Override when |
|---|---|---|
| `_check_game_over() -> bool` | `lives <= 0` | time-up / board-full / win |
| `_handle_gameplay_tap(pos)` | no-op | tap-to-shoot, puzzle pick, rhythm |
| `_get_hud_text() -> String` | "Score: X   Lives: Y" | combo / time / accuracy |
| `_draw_start_screen()` | title + tap hint | difficulty / character select |
| `_draw_game_over_screen()` | game over + score + retry | win/lose split, leaderboard |

**⚠️ How to override**: GDScript does NOT allow two functions with the same
name (no `override` keyword). To customize a default, **Edit the existing
function body in the plumbing section** above the AI ZONE — find the
`func _get_hud_text() -> ...` etc. that's already declared and replace its
body. Do NOT add a duplicate `func _get_hud_text()` in the AI ZONE — it will
cause `Parse Error: Function "..." has the same name as a previously declared
function` and the script won't load (white screen, no audio).

## GDScript type inference gotcha

```gdscript
var spd := 50.0 + game.wave * 10.0       # ❌ Parse Error: Cannot infer type
var spd: float = 50.0 + game.wave * 10.0 # ✅ explicit type
var hp: int = e["hp"]                    # ✅ ditto for Dictionary access
```

`game.X` and `Dictionary["key"]` return `Variant` — `:=` can't infer a
concrete type from that. **Always type-annotate when RHS touches `game.` or
`dict["..."]`**.

## Pre-bound state (use directly)

- `score: int`, `lives: int` — common game state
- `anim_timer: float` — use `sin(anim_timer * speed)` for pulsing
- `game: Dictionary` — your free-form game-specific scratchpad
- `screen_size: Vector2`, `HUD_SAFE_LEFT/RIGHT/TOP` — layout helpers
- `bridge: Node` — InputBridge (body_lean_x/y, face_position, hand_gesture, ...)
- `game_font: Font` — pre-loaded CJK font for `draw_string`
- `bgm_track: String` — set in `_init_game()` to auto-play a BGM track

## Pre-bound helpers (use these instead of raw APIs)

- `_tr("key", {params})` — i18n lookup (USE for all player-facing UI text)
- `_safe_load_texture("res://...")` — null-safe texture loader
- `get_body_target(margin)` — body-pose / face → target Vector2 (margin-clamped)

## Files you may modify

| File | Allowed changes |
|---|---|
| `scenes/main.gd` | **Only inside the "AI IMPLEMENTATION ZONE" at the bottom.** Do NOT touch plumbing. |
| `scripts/i18n_strings.gd` | Add game-specific keys (with `en` + `zh-Hans`). Pre-seeded: `title`, `start_hint`, `retry_hint`, `game_over`, `score`, `lives` — overwrite `title` value at minimum. |

## Files you must NOT modify

- `project.godot`, `scripts/i18n.gd`, `scripts/input_bridge.gd`, `scripts/game_manager.gd`, `scripts/audio_manager.gd`
- The plumbing section of `scenes/main.gd` (above the "AI IMPLEMENTATION ZONE" comment)

## Coding constraints

- Use Godot 4.6 GDScript syntax only
- `draw_colored_polygon()` 2nd arg must be a single `Color` (not `PackedColorArray`)
- No inner classes (`class Foo:` inside script) — crash iOS embedded mode
- No keyboard input (`Input.is_key_pressed`, KEY_*, etc.)
- Use `_tr()` for player-facing strings; never `I18n.tr(...)` directly
- Use `_safe_load_texture()` for any extra texture; never bare `load()`
- Use `AudioManager.play(<sfx_id>)` / `play_bgm(<bgm_id>)` for sounds; never instantiate `AudioStreamPlayer` directly. `<sfx_id>` / `<bgm_id>` are **placeholders for the actual ID string**, e.g. `AudioManager.play("hit")` — never pass the literal `"name"` / `"sfx_id"` / category words as IDs.
- Sound IDs must come byte-for-byte from the catalog in the system prompt
- No Bash tool (sandbox blocks it); no TodoWrite (write files directly via Edit)
