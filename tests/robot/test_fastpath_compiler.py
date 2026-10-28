"""Fast Path Compiler 单测 — Phase 1 端到端。

验证：
1. 硬编码 recipe → 完整 Godot 工程目录
2. main.gd 不含 pass # TODO(AI) 占位
3. main.gd 含 3 个必填 hook 实现
4. i18n_strings.gd 含 title 双语
5. meta.json 字段齐全 + provenance="fastpath"
6. 两份不同 recipe 产生**有差异的** GDScript（多样性回归）
7. archetype_id 未知 → CompileError
8. 目标目录已存在 → CompileError
9. sprite_hint 不命中 catalog → 退化到默认色，不抛错
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robot.games.fastpath.catalog import AssetCatalog
from robot.games.fastpath.compiler import CompileError, RecipeCompiler
from robot.games.fastpath.schemas import (
    Audio,
    EntitySpec,
    Entities,
    GameRecipe,
    Meta,
    Params,
    Theme,
)


_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_TEMPLATE_DIR = _BACKEND_ROOT / "games" / "_template"


def _make_recipe(
    *,
    archetype_id: str = "catch_v2",
    title_en: str = "Banana Frenzy",
    title_zh: str = "香蕉狂热",
    player_hint: str = "monkey",
    drop_hint: str = "banana",
    obstacle_hint: str = "bomb",
    palette: str = "warm",
    background: str = "forest",
    spawn_rate: float = 1.8,
    bgm: str = "bgm_casual",
) -> GameRecipe:
    return GameRecipe(
        archetype_id=archetype_id,
        confidence=0.95,
        meta=Meta(
            title_i18n={"en": title_en, "zh-Hans": title_zh},
            description_i18n={"en": "Catch the falling items!", "zh-Hans": "接住下落的物品！"},
            icon="🍌",
        ),
        theme=Theme(palette=palette, background_hint=background),
        entities=Entities(
            player=EntitySpec(sprite_hint=player_hint, control="face_x"),
            drop=EntitySpec(sprite_hint=drop_hint, score_value=1),
            obstacle=EntitySpec(sprite_hint=obstacle_hint, lives_cost=1),
        ),
        params=Params(spawn_rate_per_sec=spawn_rate),
        audio=Audio(bgm=bgm),
    )


def test_compile_produces_complete_godot_project(tmp_path: Path) -> None:
    compiler = RecipeCompiler(_TEMPLATE_DIR, AssetCatalog.default())
    recipe = _make_recipe()
    target = tmp_path / "game_test_1"

    result = compiler.compile(recipe, target)

    assert result.game_dir == target
    assert result.archetype_id == "catch_v2"
    for required in ("scenes/main.gd", "scripts/i18n_strings.gd", "meta.json"):
        assert required in result.files_written
    # 基础工程文件由 _template 拷过来
    for fname in ("project.godot", "scenes/main.tscn", "scripts/audio_manager.gd",
                  "scripts/game_manager.gd", "scripts/i18n.gd", "scripts/input_bridge.gd"):
        assert (target / fname).exists(), f"missing {fname}"


def test_compile_writes_input_mode_into_meta(tmp_path: Path) -> None:
    """meta.json 必须带 input_mode，且与传入值一致 —— 防止 fast path 游戏的
    meta 与 DB（ai_games.input_mode）分叉导致端上体感判定失效（历史 P0）。"""
    import json

    compiler = RecipeCompiler(_TEMPLATE_DIR, AssetCatalog.default())

    # 显式 body_pose（archetype 实际场景）
    target_bp = tmp_path / "game_mode_bp"
    compiler.compile(_make_recipe(), target_bp, input_mode="body_pose")
    meta_bp = json.loads((target_bp / "meta.json").read_text(encoding="utf-8"))
    assert meta_bp.get("input_mode") == "body_pose"

    # 显式 touch
    target_touch = tmp_path / "game_mode_touch"
    compiler.compile(_make_recipe(), target_touch, input_mode="touch")
    meta_touch = json.loads((target_touch / "meta.json").read_text(encoding="utf-8"))
    assert meta_touch.get("input_mode") == "touch"

    # 缺省参数向后兼容 → touch，但 key 必须存在（不能再缺失）
    target_def = tmp_path / "game_mode_default"
    compiler.compile(_make_recipe(), target_def)
    meta_def = json.loads((target_def / "meta.json").read_text(encoding="utf-8"))
    assert "input_mode" in meta_def
    assert meta_def["input_mode"] == "touch"


def test_compile_strips_ai_zone_placeholders(tmp_path: Path) -> None:
    compiler = RecipeCompiler(_TEMPLATE_DIR, AssetCatalog.default())
    recipe = _make_recipe()
    target = tmp_path / "game_strip"

    compiler.compile(recipe, target)
    main_gd = (target / "scenes" / "main.gd").read_text(encoding="utf-8")

    assert "pass  # TODO(AI)" not in main_gd, "AI placeholder pass leaked into output"
    # 三个必填 hook 都有实现
    assert "func _init_game() -> void:" in main_gd
    assert "func _update_gameplay(delta: float) -> void:" in main_gd
    assert "func _draw_gameplay() -> void:" in main_gd
    # AI ZONE 上方的 plumbing 必须保留
    assert "func _ready() -> void:" in main_gd
    assert "func get_body_target(margin: float = 40.0)" in main_gd
    # Phase 2 sprite-based rendering：必须用 texture，不能只靠 draw_circle
    assert "_safe_load_texture(\"res://assets/player.png\")" in main_gd
    assert "_safe_load_texture(\"res://assets/drop.png\")" in main_gd
    assert "_safe_load_texture(\"res://assets/obstacle.png\")" in main_gd
    assert "draw_texture_rect" in main_gd
    # 但 draw_circle fallback 也要保留（sprite 缺失退化路径）
    assert "draw_circle" in main_gd


def test_compile_copies_sprite_assets(tmp_path: Path) -> None:
    """Phase 1 legacy：sprite_hint-only recipe 走 catalog 预渲染拷 PNG。"""
    compiler = RecipeCompiler(_TEMPLATE_DIR, AssetCatalog.default())
    recipe = _make_recipe(player_hint="monkey", drop_hint="banana", obstacle_hint="bomb")
    target = tmp_path / "game_sprites_legacy"

    result = compiler.compile(recipe, target)

    for sprite_path in ("assets/player.png", "assets/drop.png", "assets/obstacle.png"):
        full = target / sprite_path
        assert full.exists(), f"sprite missing: {sprite_path}"
        assert full.stat().st_size > 0
        assert sprite_path in result.files_written


def test_compile_renders_emoji_assets(tmp_path: Path) -> None:
    """Phase 2 W1.5：recipe.entities.*.emoji 走 PIL 现场渲染，不查 catalog。"""
    from robot.games.fastpath.schemas import (
        Audio, EntitySpec, Entities, GameRecipe, Meta, Params, Theme,
    )

    # 故意填一个 catalog 完全没有的 emoji（鲨鱼 / 寿司 / 火），证明不走 catalog
    recipe = GameRecipe(
        archetype_id="catch_v2",
        confidence=0.95,
        meta=Meta(
            title_i18n={"en": "Shark Sushi", "zh-Hans": "鲨鱼寿司"},
            description_i18n={"en": "Catch sushi, dodge fire!", "zh-Hans": "接寿司，躲火！"},
            icon="🍣",
        ),
        theme=Theme(palette="cool", background_hint="underwater"),
        entities=Entities(
            player=EntitySpec(emoji="🦈", control="face_x"),
            drop=EntitySpec(emoji="🍣", score_value=1),
            obstacle=EntitySpec(emoji="🔥", lives_cost=1),
        ),
        params=Params(),
        audio=Audio(),
    )
    target = tmp_path / "game_sushi"

    compiler = RecipeCompiler(_TEMPLATE_DIR)
    result = compiler.compile(recipe, target)

    # 三张 sprite 都被渲染到位
    for sprite_path in ("assets/player.png", "assets/drop.png", "assets/obstacle.png"):
        full = target / sprite_path
        assert full.exists(), f"emoji sprite missing: {sprite_path}"
        assert full.stat().st_size > 500, f"emoji PNG too small: {sprite_path}"
        assert sprite_path in result.files_written


def test_compile_writes_i18n_with_localized_title(tmp_path: Path) -> None:
    compiler = RecipeCompiler(_TEMPLATE_DIR)
    recipe = _make_recipe(title_en="Stardust Catcher", title_zh="拾星者")
    target = tmp_path / "game_i18n"

    compiler.compile(recipe, target)
    i18n_gd = (target / "scripts" / "i18n_strings.gd").read_text(encoding="utf-8")

    assert "Stardust Catcher" in i18n_gd
    assert "拾星者" in i18n_gd
    # 骨架必须保留的 keys
    assert '"start_hint"' in i18n_gd
    assert '"score"' in i18n_gd


def test_compile_writes_meta_json(tmp_path: Path) -> None:
    compiler = RecipeCompiler(_TEMPLATE_DIR)
    recipe = _make_recipe(title_en="Fruit Pop", title_zh="水果接接乐")
    target = tmp_path / "game_meta"

    compiler.compile(recipe, target)
    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))

    assert meta["archetype_id"] == "catch_v2"
    assert meta["provenance"] == "fastpath"
    # schema_version 跟 SCHEMA_VERSION 常量同步，避免硬编码字符串
    from robot.games.fastpath.schemas import SCHEMA_VERSION
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["title"] == "Fruit Pop"
    assert meta["title_i18n"]["zh-Hans"] == "水果接接乐"
    assert meta["icon"] == "🍌"
    assert meta["recipe"]["archetype_id"] == "catch_v2"  # 完整 recipe 快照
    assert "created_at" in meta


def test_two_different_recipes_produce_different_gdscript(tmp_path: Path) -> None:
    """多样性回归：两份不同 recipe 必须输出可区分的 GDScript。
    防止 Customizer / Compiler 退化到模板默认值（"千篇一律"风险）。
    """
    compiler = RecipeCompiler(_TEMPLATE_DIR)

    recipe_a = _make_recipe(
        title_en="Banana Frenzy", title_zh="香蕉狂热",
        player_hint="monkey", drop_hint="banana", obstacle_hint="bomb",
        palette="warm", background="forest", spawn_rate=1.8, bgm="bgm_casual",
    )
    recipe_b = _make_recipe(
        title_en="Stardust Catcher", title_zh="拾星者",
        player_hint="astronaut", drop_hint="star", obstacle_hint="rock",
        palette="neon", background="space", spawn_rate=2.5, bgm="bgm_arcade",
    )

    compiler.compile(recipe_a, tmp_path / "game_a")
    compiler.compile(recipe_b, tmp_path / "game_b")

    main_a = (tmp_path / "game_a" / "scenes" / "main.gd").read_text(encoding="utf-8")
    main_b = (tmp_path / "game_b" / "scenes" / "main.gd").read_text(encoding="utf-8")

    # GDScript 必须不同：颜色 / bgm / spawn_interval 都从 recipe 渲染
    assert main_a != main_b
    # bgm
    assert 'bgm_track = "bgm_casual"' in main_a
    assert 'bgm_track = "bgm_arcade"' in main_b
    # 颜色来自 catalog，monkey vs astronaut 必然不同 RGB
    # 直接比较两份 main.gd 的 _draw_gameplay 段是否字节级不等
    # （上面 != 已经覆盖，但显式 assert 颜色差异更明确）
    monkey_rgb = AssetCatalog.default().match_character("monkey").color
    astro_rgb = AssetCatalog.default().match_character("astronaut").color
    assert monkey_rgb != astro_rgb

    # i18n 文件也要不同
    i18n_a = (tmp_path / "game_a" / "scripts" / "i18n_strings.gd").read_text(encoding="utf-8")
    i18n_b = (tmp_path / "game_b" / "scripts" / "i18n_strings.gd").read_text(encoding="utf-8")
    assert i18n_a != i18n_b
    assert "香蕉狂热" in i18n_a
    assert "拾星者" in i18n_b


def test_compile_unknown_archetype_raises(tmp_path: Path) -> None:
    compiler = RecipeCompiler(_TEMPLATE_DIR)
    # 绕过 Pydantic 的 schema 校验（archetype_id 是 free-form str），
    # 让 Compiler 自己捕获不在 STRATEGIES 里的情况
    recipe = _make_recipe()
    recipe.archetype_id = "no_such_archetype"  # type: ignore[misc]

    with pytest.raises(CompileError, match="unknown archetype_id"):
        compiler.compile(recipe, tmp_path / "game_unknown")


def test_compile_existing_target_raises(tmp_path: Path) -> None:
    compiler = RecipeCompiler(_TEMPLATE_DIR)
    target = tmp_path / "game_dup"
    target.mkdir()

    with pytest.raises(CompileError, match="already exists"):
        compiler.compile(_make_recipe(), target)


def test_unknown_sprite_hint_falls_back_to_default(tmp_path: Path) -> None:
    """sprite_hint 不命中 catalog → 退化到 catalog 默认色，**不抛错**。"""
    compiler = RecipeCompiler(_TEMPLATE_DIR)
    recipe = _make_recipe(
        player_hint="garbage_xyz",
        drop_hint="some_unknown_thing",
        obstacle_hint="nonexistent",
    )

    result = compiler.compile(recipe, tmp_path / "game_fallback")
    assert result.game_dir.exists()
    main_gd = (result.game_dir / "scenes" / "main.gd").read_text(encoding="utf-8")
    # 仍然渲染出合法 GDScript
    assert "draw_circle" in main_gd
    assert "Color(" in main_gd


def test_missing_template_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(CompileError, match="template_dir not found"):
        RecipeCompiler(tmp_path / "no_template_here")


def test_compile_whack_v1_end_to_end(tmp_path: Path) -> None:
    """Phase 3 W1：whack_v1 archetype 端到端编译 + _on_tap dispatch 钩子。"""
    from robot.games.fastpath.schemas import (
        Audio, EntitySpec, Entities, GameRecipe, Meta, Params, Theme,
    )

    recipe = GameRecipe(
        archetype_id="whack_v2",
        confidence=0.93,
        meta=Meta(
            title_i18n={"en": "Mole Mash", "zh-Hans": "打地鼠"},
            description_i18n={"en": "Tap moles, dodge bombs!", "zh-Hans": "拍中地鼠，躲开炸弹！"},
            icon="🐹",
        ),
        theme=Theme(palette="warm", background_hint="forest"),
        entities=Entities(
            drop=EntitySpec(emoji="🐹", score_value=1),
            obstacle=EntitySpec(emoji="💣", lives_cost=1),
        ),
        params=Params(win_score=25),
        audio=Audio(bgm="bgm_playful"),
    )
    target = tmp_path / "game_whack"

    compiler = RecipeCompiler(_TEMPLATE_DIR)
    result = compiler.compile(recipe, target)

    assert result.archetype_id == "whack_v2"
    main_gd = (target / "scenes" / "main.gd").read_text(encoding="utf-8")
    # Whack-specific markers
    assert "func _on_tap(pos: Vector2) -> void:" in main_gd, \
        "whack must define _on_tap (dispatched from plumbing _handle_gameplay_tap)"
    assert "game.targets = []" in main_gd
    assert "_spawn_target()" in main_gd
    # Skeleton plumbing's dispatch hook must be present
    assert 'has_method("_on_tap")' in main_gd
    # whack 不渲 player（只 target + decoy）
    assert (target / "assets" / "drop.png").exists()
    assert (target / "assets" / "obstacle.png").exists()
    assert not (target / "assets" / "player.png").exists(), \
        "whack 不该渲染 player sprite"


def test_compile_slicer_v2_end_to_end(tmp_path: Path) -> None:
    """P3.1：slicer_v2 archetype 端到端编译（几何 combo 识别版）。"""
    from robot.games.fastpath.schemas import (
        Audio, EntitySpec, Entities, GameRecipe, Meta, Params, Theme,
    )

    recipe = GameRecipe(
        archetype_id="slicer_v2",
        confidence=0.92,
        meta=Meta(
            title_i18n={"en": "Fruit Slash", "zh-Hans": "切水果"},
            description_i18n={"en": "Slice fruits, dodge bombs!", "zh-Hans": "挥手切水果，躲开炸弹！"},
            icon="🍉",
        ),
        theme=Theme(palette="vibrant", background_hint="beach"),
        entities=Entities(
            drop=EntitySpec(emoji="🍉", score_value=1),
            obstacle=EntitySpec(emoji="💣", lives_cost=1),
        ),
        params=Params(win_score=25),
        audio=Audio(bgm="bgm_action"),
    )
    target = tmp_path / "game_slicer"

    # slicer_v2 依赖 _template_fastpath/scripts/ 下的 helper（Juice / Particles），
    # 编译时必须把 fastpath addon 一起 overlay 上去。
    _ADDON = _BACKEND_ROOT / "games" / "_template_fastpath"
    compiler = RecipeCompiler(_TEMPLATE_DIR, addon_dirs=(_ADDON,))
    result = compiler.compile(recipe, target)

    assert result.archetype_id == "slicer_v2"
    main_gd = (target / "scenes" / "main.gd").read_text(encoding="utf-8")
    # 仍保留 v1 共同的底层 plumbing
    assert "game.trail = []" in main_gd
    assert "_resolve_cursor()" in main_gd
    assert "bridge.hand_position" in main_gd
    assert "MIN_SLICE_SPEED" in main_gd
    assert "_seg_point_dist(" in main_gd
    assert "draw_line(" in main_gd
    # P3.1 几何识别器专属：4 个 combo detector + DP 简化 + slash 状态机
    assert "_simplify_dp(" in main_gd, "v2 must implement Douglas-Peucker"
    assert "_detect_loop(" in main_gd
    assert "_detect_infinity(" in main_gd
    assert "_detect_zorro(" in main_gd
    assert "_segments_intersect(" in main_gd, "INFINITY 需 self-intersection 算法"
    assert "_resolve_slash_combo" in main_gd
    assert "slash_active" in main_gd
    assert "SLASH_TIMEOUT" in main_gd
    # 4 个 combo 标签
    for label in ("ZORRO!", "INFINITY!", "LOOP!", "TRIPLE!"):
        assert label in main_gd, f"missing combo label {label}"
    # v2 调用 fastpath helper（juice / particles）
    assert "Particles.splatter" in main_gd
    assert "Particles.combo_blast" in main_gd
    assert "Juice." in main_gd, "v2 must use Juice helpers for impact"
    # Slicer renders fruit + bomb, no player sprite
    assert (target / "assets" / "drop.png").exists()
    assert (target / "assets" / "obstacle.png").exists()
    assert not (target / "assets" / "player.png").exists()
    # fastpath addon helpers 拷进游戏目录
    assert (target / "scripts" / "juice.gd").exists()
    assert (target / "scripts" / "particles.gd").exists()
    assert (target / "scripts" / "result_card.gd").exists()
    assert (target / "scripts" / "chemistry.gd").exists()
    assert (target / "scripts" / "director.gd").exists()
    bridge_gd = (target / "scripts" / "input_bridge.gd").read_text(encoding="utf-8")
    assert "hand_position: Vector2" in bridge_gd


def test_compile_dodger_v1_end_to_end(tmp_path: Path) -> None:
    """Phase 2 W2：dodger_v1 archetype 端到端编译。"""
    from robot.games.fastpath.schemas import (
        Audio, EntitySpec, Entities, GameRecipe, Meta, Params, Theme,
    )

    recipe = GameRecipe(
        archetype_id="dodger_v2",
        confidence=0.92,
        meta=Meta(
            title_i18n={"en": "Meteor Dodge", "zh-Hans": "陨石躲避"},
            description_i18n={"en": "Pilot through the storm!", "zh-Hans": "穿越陨石风暴！"},
            icon="☄️",
        ),
        theme=Theme(palette="neon", background_hint="space"),
        entities=Entities(
            player=EntitySpec(emoji="🚀", control="face_x"),
            obstacle=EntitySpec(emoji="☄️", lives_cost=1),
        ),
        params=Params(win_score=60),
        audio=Audio(bgm="bgm_action"),
    )
    target = tmp_path / "game_dodger"

    compiler = RecipeCompiler(_TEMPLATE_DIR)
    result = compiler.compile(recipe, target)

    assert result.archetype_id == "dodger_v2"
    main_gd = (target / "scenes" / "main.gd").read_text(encoding="utf-8")
    # Dodger-specific markers
    assert "game.hazards = []" in main_gd
    assert "_spawn_hazard()" in main_gd
    assert "score = int(float(game.elapsed))" in main_gd  # 时间累积评分
    # Dodger 不应有 catch 的 drop 逻辑
    assert "game.drops" not in main_gd
    assert "kept_drops" not in main_gd

    # 只渲染 player + obstacle 两张 sprite，不渲染 drop
    assert (target / "assets" / "player.png").exists()
    assert (target / "assets" / "obstacle.png").exists()
    assert not (target / "assets" / "drop.png").exists(), \
        "dodger 不该渲染 drop sprite"

    # meta.json 的 archetype_id 字段
    import json as _json
    meta = _json.loads((target / "meta.json").read_text(encoding="utf-8"))
    assert meta["archetype_id"] == "dodger_v2"


# ── 方案 C：base + addon 分层拷贝 ────────────────────────────────────────────


def test_compiler_addon_dirs_overlay_after_base(tmp_path: Path):
    """fast path 的 addon 目录在 base 拷完后叠加，与 base 同名文件被 addon
    覆盖；addon 新增的文件出现在最终游戏目录。"""
    # 1) 用 tmp base 复刻最小骨架
    fake_base = tmp_path / "_template"
    (fake_base / "scenes").mkdir(parents=True)
    (fake_base / "scripts").mkdir(parents=True)
    (fake_base / "scenes" / "main.gd").write_text(
        "extends Node2D\n"
        "# REQUIRED: called once when player taps to start\n"
        "func _init_game(): pass\n",
        encoding="utf-8",
    )
    (fake_base / "scenes" / "main.tscn").write_text(
        '[gd_scene format=3]', encoding="utf-8"
    )
    (fake_base / "scripts" / "i18n_strings.gd").write_text(
        "extends Node\nconst STRINGS := {}\n", encoding="utf-8"
    )
    (fake_base / "scripts" / "audio_manager.gd").write_text(
        "# base AudioManager\n", encoding="utf-8"
    )
    (fake_base / "project.godot").write_text(
        '[application]\nconfig/name="Base"\n', encoding="utf-8"
    )

    # 2) addon：覆盖 audio_manager.gd + 新增 juice.gd
    fake_addon = tmp_path / "_template_fastpath"
    (fake_addon / "scripts").mkdir(parents=True)
    (fake_addon / "scripts" / "audio_manager.gd").write_text(
        "# fastpath addon AudioManager with juice\n", encoding="utf-8"
    )
    (fake_addon / "scripts" / "juice.gd").write_text(
        "# fastpath-only juice helper\n", encoding="utf-8"
    )

    recipe = _make_recipe()  # default catch_v1
    target = tmp_path / "game_addon"

    compiler = RecipeCompiler(fake_base, addon_dirs=(fake_addon,))
    compiler.compile(recipe, target)

    # base 文件保留
    assert (target / "project.godot").read_text(encoding="utf-8").startswith("[application]")
    # addon 覆盖了 base 的同名文件
    am = (target / "scripts" / "audio_manager.gd").read_text(encoding="utf-8")
    assert "fastpath addon" in am, f"addon 未覆盖 base: {am!r}"
    # addon 新增文件出现
    assert (target / "scripts" / "juice.gd").exists()


def test_compiler_missing_addon_silently_skipped(tmp_path: Path):
    """addon dir 不存在时 compile 不抛错，照常完成。"""
    # 用真实 _TEMPLATE_DIR 但 addon 指向不存在路径
    target = tmp_path / "game_no_addon"
    compiler = RecipeCompiler(
        _TEMPLATE_DIR,
        addon_dirs=(tmp_path / "no_such_addon",),
    )
    result = compiler.compile(_make_recipe(), target)
    assert result.archetype_id == "catch_v2"
    assert (target / "scenes" / "main.gd").exists()


# ─── Phase 2.1：schema v0.2（directors / ItemTag / hidden_tech）─────────────


def test_schema_v0_2_directors_field():
    """GameRecipe.directors 接受 0-3 个 Director enum 值，超过 3 个抛 ValidationError。"""
    from pydantic import ValidationError as PydValidationError
    from robot.games.fastpath.schemas import Director, GameRecipe

    # 0 个：合法
    r0 = GameRecipe(archetype_id="catch_v2", directors=[])
    assert r0.directors == []

    # 3 个：合法，且 enum 值正确
    r3 = GameRecipe(
        archetype_id="catch_v2",
        directors=["tense", "chaotic", "slowmo"],
    )
    assert r3.directors == [Director.TENSE, Director.CHAOTIC, Director.SLOWMO]

    # 4 个：超过 max_length，被拒
    with pytest.raises(PydValidationError):
        GameRecipe(archetype_id="catch_v2", directors=["a", "b", "c", "d"])

    # 非法 enum 值：被拒
    with pytest.raises(PydValidationError):
        GameRecipe(archetype_id="catch_v2", directors=["not_a_director"])


def test_schema_v0_2_item_tag_field():
    """EntitySpec.tag 默认 NEUTRAL，接受 6 个合法 ItemTag 值之一。"""
    from pydantic import ValidationError as PydValidationError
    from robot.games.fastpath.schemas import EntitySpec, ItemTag

    # 默认 NEUTRAL，老 recipe 不需要改
    e_default = EntitySpec(emoji="🐶")
    assert e_default.tag == ItemTag.NEUTRAL

    # 显式赋 6 种 tag 全部接受
    for tag_val in ["neutral", "fire", "water", "grass", "metal", "explosive"]:
        e = EntitySpec(emoji="🔥", tag=tag_val)
        assert e.tag.value == tag_val

    # 非法 tag：被拒
    with pytest.raises(PydValidationError):
        EntitySpec(emoji="🔥", tag="lava")


def test_schema_v0_2_hidden_tech_field():
    """GameRecipe.hidden_tech_enabled 默认 False，接受 bool。"""
    from robot.games.fastpath.schemas import GameRecipe

    assert GameRecipe(archetype_id="catch_v2").hidden_tech_enabled is False
    assert GameRecipe(archetype_id="catch_v2", hidden_tech_enabled=True).hidden_tech_enabled is True


def test_schema_v0_2_backward_compat_old_recipe(tmp_path: Path):
    """v0.1 风格的 recipe（不传 directors / tag / hidden_tech）应该和 v0.2 行为
    等价：默认值确保 chemistry 走 no_op、Director 全空、hidden_tech 关闭。
    """
    from robot.games.fastpath.schemas import GameRecipe

    r = _make_recipe()  # 不传任何 v0.2 字段
    assert r.directors == []
    assert r.hidden_tech_enabled is False
    assert r.entities.player.tag.value == "neutral"
    assert r.entities.drop.tag.value == "neutral"
    assert r.entities.obstacle.tag.value == "neutral"

    # 老 recipe 应该照样能 compile（不抛错、不缺文件）
    compiler = RecipeCompiler(_TEMPLATE_DIR, AssetCatalog.default())
    compiler.compile(r, tmp_path / "g_v01_compat")
    assert (tmp_path / "g_v01_compat" / "scenes" / "main.gd").exists()
