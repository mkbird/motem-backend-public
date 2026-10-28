# AI Game Project (Skeleton hook mode)

## ⚠️ Operating mode: SKELETON HOOKS

`scenes/main.gd` is a 300-line **skeleton** that already implements the outer
shell of any 2D game. **DO NOT rewrite main.gd from scratch.** Your job is to
fill the gameplay logic in the "AI IMPLEMENTATION ZONE" at the bottom by
implementing 3 required hooks and optionally overriding 5 default hooks.

The skeleton already handles:

- State machine (START → PLAYING → GAME_OVER)
- Touch routing (tap-to-start / tap-to-restart / mid-game tap callback)
- HUD draw (with top-center safe zone)
- Background sprite with `z_index=-100` (null-safe)
- Font loading (CJK-compatible)
- i18n routing
- Body-pose / face-tracking helper (`get_body_target`)
- BGM auto-start when `bgm_track` is set

## Runtime environment

- **Target platform**: iOS (iPhone/iPad), embedded in SwiftUI via
  SwiftGodotKit. The same game may also be previewed on macOS (Motem
  Studio), so author for iOS and let the macOS host letterbox automatically.
- **Renderer**: Forward Mobile (`renderer/rendering_method="mobile"` in
  `project.godot`). DO NOT change it.
- **Design resolution**: landscape **1194×834** logical pixels. All HUD
  positions, font sizes, and layout calculations target this canvas.
  `stretch_mode=canvas_items` + `aspect=keep` handle macOS preview windows
  of any size automatically — DO NOT branch by platform or resolution.
- **HUD safe zone**: the host app draws overlay buttons in the top-left
  (exit ✕) and top-right (modify ✎) corners, ~50×50 px each. Keep ALL HUD
  elements (score, lives, text, debug overlays) ≥130 px horizontally and
  ≥10 px vertically away from those corners — use top-center or offset
  inward.
- **Input**: NO keyboard, NO mouse. Touch is routed via the
  `_handle_gameplay_tap(pos)` hook; body pose / face tracking via the
  pre-bound `bridge` node. See "Input bridge reference" below.

## Required hooks (you MUST implement these 3)

| Hook | Purpose |
|---|---|
| `_init_game()` | Set up game-specific state (called on tap-to-start and retry). Set `bgm_track` here if BGM is desired. |
| `_update_gameplay(delta)` | Per-frame logic during PLAYING — move objects, spawn enemies, check collisions, increment `score`, decrement `lives`. |
| `_draw_gameplay()` | Render game objects. Background and HUD are drawn by the skeleton. |

## Optional override hooks

| Hook | Default | Override when |
|---|---|---|
| `_check_game_over() -> bool` | `lives <= 0` | time-up / board-full / win-condition games |
| `_handle_gameplay_tap(pos)` | no-op | tap-to-shoot, puzzle pick, rhythm |
| `_get_hud_text() -> String` | `"Score: X   Lives: Y"` | combo / time / accuracy / level display |
| `_draw_start_screen()` | title + tap hint | difficulty / character / level select |
| `_draw_game_over_screen()` | game over + score + retry | win/lose split, leaderboard |

**⚠️ How to override**: GDScript does NOT allow two functions with the same
name (no `override` keyword). To customize a default, **Edit the existing
function body in the plumbing section** above the AI ZONE — find the
`func _get_hud_text() -> ...` etc. that is already declared and replace its
body. Do NOT add a duplicate `func _get_hud_text()` in the AI ZONE — it
will cause `Parse Error: Function "..." has the same name as a previously
declared function` and the script won't load (white screen, no audio).

## Pre-bound state (use directly)

- `score: int`, `lives: int` — common game state
- `anim_timer: float` — use `sin(anim_timer * speed)` for pulsing animations
- `game: Dictionary` — your free-form game-specific scratchpad
- `screen_size: Vector2`, `HUD_SAFE_LEFT/RIGHT/TOP` — layout helpers
- `bridge: Node` — InputBridge (body_lean_x/y, face_position, hand_gesture, ...)
- `game_font: Font` — pre-loaded CJK font for `draw_string`
- `bgm_track: String` — set in `_init_game()` to auto-play a BGM track

## Pre-bound helpers (use these instead of raw APIs)

- `_tr("key", {params})` — i18n lookup (USE for all player-facing UI text)
- `_safe_load_texture("res://...")` — null-safe texture loader
- `get_body_target(margin)` — body-pose / face → target Vector2 (margin-clamped)

## Input bridge reference

Body pose / face tracking data is exposed via the pre-bound `bridge` node.
The skeleton ships a helper for the common case:

```gdscript
# Returns the screen position the body/face is currently pointing at,
# clamped within margins. Use for position-control games (dodger /
# shooter / lane runner).
var target := get_body_target(40.0)
game.player_pos = game.player_pos.lerp(target, 12.0 * delta)
```

Raw bridge properties (use only if `get_body_target` is not enough):

- `bridge.body_lean_x / body_lean_y: float` — `-1.0` to `1.0`
- `bridge.face_position: Vector2` — `(0.0, 0.0)` to `(1.0, 1.0)` normalised
- `bridge.hand_gesture: String` — one of `"none" / "fist" / "thumbsUp" /
  "peace" / "ok" / "heart" / "wave"`
- `bridge.is_hands_up: bool`, `bridge.is_ducking: bool`,
  `bridge.is_reaching_out: bool`
- `bridge.body_detected: bool`, `bridge.face_detected: bool`

**Touch input** is already wired by the skeleton:

- Tap on START screen → skeleton calls `_init_game()` then enters PLAYING
- Tap on GAME_OVER screen → re-enters PLAYING (calls `_init_game()` again)
- Tap during PLAYING → skeleton calls your `_handle_gameplay_tap(pos: Vector2)`
  (override the hook for tap-to-shoot / piece select / rhythm tap)

You should NOT override `_input(event)` directly.

## Asset structure

- `assets/background.png` — full-screen background. **REQUIRED in every
  game**. Host pre-creates a placeholder PNG; use a `Sprite2D` (or
  `TextureRect`) loading `res://assets/background.png` covering the entire
  viewport.

  **CRITICAL**: set `bg.z_index = -100` (or any negative value) BEFORE
  `add_child(bg)`. Godot 2D paints the parent `Node2D._draw()` FIRST, then
  paints child `Sprite2D` nodes ON TOP. Without negative `z_index` the
  background covers all `_draw()` content (player, enemies, bullets, HUD)
  — the screen becomes a solid placeholder colour. `move_child(bg, 0)`
  does NOT fix this; only `z_index` does.

- For all OTHER on-screen visual elements (player, enemies, bullets, blocks,
  tiles, notes, obstacles, items, etc.) — pick descriptive filenames
  matching your game's theme (e.g. `spaceship.png` / `alien.png` /
  `laser.png` for a shooter; `wall.png` / `exit.png` for a maze;
  `block.png` / `spike.png` for a platformer). You MUST:

  1. Create the PNG file yourself (a tiny solid-colour rectangle written
     via a small pre-build helper script is fine — but the file must
     actually exist at `assets/<name>.png`).
  2. Load via `Sprite2D` + `load("res://assets/<name>.png")` (use
     `_safe_load_texture` if reading inside `_draw`).
  3. Register in `meta.json` (see output format).

- Game element images MUST live in `assets/`. Reserve `_draw()` /
  `draw_rect` / `draw_circle` / `draw_line` for HUD chrome only (score
  numbers, lives, debug overlays).

- Additional user-uploaded files may live in `assets/` (modify prompts
  list them under "## User-uploaded assets"); prefer
  `load("res://assets/<filename>")` for those too.

**All file operations** (Read/Write/Edit) MUST use relative paths from
cwd (e.g. `scenes/main.gd`). NEVER use absolute paths. NEVER access files
outside the cwd directory.

## Files you may modify

| File | Allowed changes |
|---|---|
| `scenes/main.gd` | **Only inside the "AI IMPLEMENTATION ZONE" at the bottom.** Do NOT touch plumbing above. |
| `scripts/i18n_strings.gd` | Add game-specific keys (with `en` + `zh-Hans`). Pre-seeded: `title`, `start_hint`, `retry_hint`, `game_over`, `score`, `lives` — overwrite `title` value at minimum. |

## Files you must NOT modify

- `project.godot`, `scripts/i18n.gd`, `scripts/input_bridge.gd`,
  `scripts/game_manager.gd`, `scripts/audio_manager.gd`
- The plumbing section of `scenes/main.gd` (above the
  "AI IMPLEMENTATION ZONE" comment)

## Coding constraints

- Use Godot 4.6 GDScript syntax only — no Godot 3 syntax, no C#, no `.cs`
  files
- All scripts must `extends Node2D` or appropriate base class
- All rendering must be 2D (`Node2D`, `Sprite2D`, `CharacterBody2D`,
  `Area2D`, etc.)
- For visual game elements (background, player, enemies, bullets, items):
  MUST use `Sprite2D` nodes loading `res://assets/<role>.png`. Reserve
  `draw_rect` / `draw_circle` / `draw_line` / `_draw()` for HUD only —
  never for the main gameplay sprites.
- `draw_rect(rect, color)` for filled rectangles;
  `draw_rect(rect, color, false, width)` for outlines. **Never pass
  `width` to a filled `draw_rect`** — it has no effect and prints
  warnings.
- `draw_colored_polygon()` 2nd arg MUST be a single `Color`, NOT
  `PackedColorArray` — wrong type crashes the script silently.
- NO inner classes (`class Foo:` inside script) — they crash iOS embedded
  mode. Use `Dictionary` instead.
- NO keyboard input (`Input.is_key_pressed`, `KEY_*`, etc.) — iOS has no
  keyboard.
- NO `Input.get_axis` / `Input.is_action_pressed` for movement — use
  `bridge.*` instead.
- NEVER split a single function call across multiple lines inside a lambda
  (e.g. filter/map) — keep lambdas on one line.
- Use `_tr(key, params)` for EVERY player-facing string. Put every
  player-facing string in `scripts/i18n_strings.gd` with both `en` and
  `zh-Hans` values. Do NOT call `I18n.tr(...)` directly — it collides
  with Godot's built-in `Object.tr()` static signature.
- Internal code, variable names, node names, comments, and logs stay in
  English. Only player-visible UI is bilingual.
- Use `_safe_load_texture("res://...")` for any texture loaded inside
  `_draw()` — never bare `load()` (returns `null` if the file is mid-
  import, which crashes Sprite2D).
- Use `_audio_play("<sfx_id>")` for SFX; pick IDs only from the
  catalog below. Do NOT instantiate `AudioStreamPlayer` directly.
- NO Bash tool — sandbox blocks it.
- NO TodoWrite — write files directly via Edit / Write.

### GDScript type inference gotcha

```gdscript
var spd := 50.0 + game.wave * 10.0       # ❌ Parse Error: Cannot infer type
var spd: float = 50.0 + game.wave * 10.0 # ✅ explicit type
var hp: int = e["hp"]                    # ✅ ditto for Dictionary access
```

`game.X` and `Dictionary["key"]` return `Variant` — `:=` cannot infer a
concrete type from that. **Always type-annotate when the RHS touches
`game.` or `dict["..."]`.**

{audio_section}

## Workflow

1. Read `scenes/main.gd` (~300 lines) to understand the skeleton. DO NOT
   touch the plumbing section above the "AI IMPLEMENTATION ZONE" comment.
2. Edit `scripts/i18n_strings.gd` to set `title` (and add any game-specific
   keys you need).
3. Edit `scenes/main.gd` only inside the "AI IMPLEMENTATION ZONE":
   - Fill `_init_game`, `_update_gameplay`, `_draw_gameplay`
   - Override 1-3 of the optional hooks if your game needs different
     behaviour

Typical implementation size: **2-5K characters total** for the three
hooks + optional overrides. If you find yourself writing > 8K chars, you
are probably re-implementing something the skeleton already does —
re-read the skeleton.
