"""Fast Path 真 LLM 端到端 smoke 测试。

跑一批典型用户输入过 Router + Customizer + Compiler，验证：
1. Router 在「清晰接物」prompt 上命中率 ≥ 85%
2. Router 在明显「非接物」prompt 上 archetype=null（不误判）
3. Customizer 输出合规 GameRecipe（catalog 命中 / params 合理 / CJK 正常）
4. 同一类型的不同用户输入产出**有差异**的 recipe（反"千篇一律"）
5. 真实 token / 时延数据

跑法：
    conda activate robot
    python scripts/fastpath_smoke.py

需要 .env 里有 ROBOT_DASHSCOPE_API_KEY。
产物：每个 prompt 跑出一个游戏到 /tmp/fastpath_smoke/<ts>/，方便后续人工 inspect。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import os

from robot.games.fastpath.compiler import RecipeCompiler
from robot.games.fastpath.customizer import Customizer
from robot.games.fastpath.llm_client import (
    make_anthropic_native_client,
    make_default_client,
    make_openrouter_client,
)
from robot.games.fastpath.orchestrator import FastPathOrchestrator
from robot.games.fastpath.router import Router


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _BACKEND_ROOT / "games" / "_template"
_TEMPLATE_FASTPATH_DIR = _BACKEND_ROOT / "games" / "_template_fastpath"
_TEMPLATE_SLOWPATH_DIR = _BACKEND_ROOT / "games" / "_template_slowpath"


# LLM 后端选择：
#   openrouter（默认）— OpenAI-compat /v1/chat/completions + Haiku 4.5 via OpenRouter
#                       （Bedrock 默认；准确率 10/10；没 cache 但实测最稳）
#   anthropic_native — anthropic SDK 直连 anthropic.com（需 sk-ant-* key）
#                      唯一能拿到 prompt caching 的路径
#   dashscope        — qwen-plus（更快更便宜，没 cache）
_BACKEND = os.environ.get("FASTPATH_SMOKE_BACKEND", "openrouter").strip().lower()
_ROUTER_MODEL = os.environ.get("FASTPATH_ROUTER_MODEL", "anthropic/claude-haiku-4.5")
_CUSTOMIZER_MODEL = os.environ.get("FASTPATH_CUSTOMIZER_MODEL", "anthropic/claude-haiku-4.5")


# (label, user_text, expected_archetype) — expected=None 表示应该 fallback
PROMPTS: list[tuple[str, str, str | None]] = [
    # ── 清晰接物（应全部命中 catch_v2） ──
    ("catch_zh_monkey",   "做一个猴子接香蕉的游戏", "catch_v2"),
    ("catch_zh_kid",      "小朋友拿着篮子接掉下来的水果", "catch_v2"),
    ("catch_en_stars",    "Catch falling stars in outer space", "catch_v2"),
    ("catch_zh_dog_bao",  "狗接包子", "catch_v2"),
    ("catch_zh_panda",    "熊猫接竹子，下面有炸弹要躲开", "catch_v2"),
    # ── 清晰躲避（应全部命中 dodger_v2） ──
    ("dodge_zh_meteor",   "操控飞船躲开陨石", "dodger_v2"),
    ("dodge_zh_cat",      "猫躲开下雨的鱼骨头", "dodger_v2"),
    ("dodge_en_dodge",    "Dodge falling rocks while running", "dodger_v2"),
    # ── 清晰拍打（应全部命中 whack_v2） ──
    ("whack_zh_mole",     "打地鼠游戏", "whack_v2"),
    ("whack_zh_monster",  "拍小怪兽，别拍可爱小猫", "whack_v2"),
    ("whack_en_tap",      "Tap the targets that pop up, avoid the bombs", "whack_v2"),
    # ── 清晰挥砍（应全部命中 slicer_v2） ──
    ("slicer_zh_fruit",   "切水果游戏，挥手切，别切炸弹", "slicer_v2"),
    ("slicer_zh_sword",   "挥剑斩飞过来的星星", "slicer_v2"),
    ("slicer_en_ninja",   "Fruit Ninja style — slash with hand", "slicer_v2"),
    # ── 边界 / 模糊 ──
    ("border_mixed",      "操控飞船躲子弹同时收集能量", None),               # 混合意图，宁可不路由
    # ── 明显非任何 archetype（应 fallback） ──
    ("non_shoot",         "用激光枪射击飞过来的外星人", None),
    ("non_rpg",           "回合制 RPG，有装备和技能树", None),
    ("non_match3",        "三消游戏，消除相同颜色的方块", None),
]


def _short(s: str, n: int = 50) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


async def main() -> None:
    print("=" * 70)
    print("FAST PATH SMOKE TEST — real LLM end-to-end")
    print("=" * 70)

    out_root = Path("/tmp/fastpath_smoke") / time.strftime("%Y%m%d_%H%M%S")
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"output dir: {out_root}")
    print(f"backend: {_BACKEND}  router_model={_ROUTER_MODEL}  customizer_model={_CUSTOMIZER_MODEL}\n")

    if _BACKEND == "anthropic_native":
        # BYOK 直连 anthropic.com — 需要 ANTHROPIC_API_KEY (sk-ant-*) 单独充值
        router_model = _ROUTER_MODEL.replace("anthropic/", "")
        customizer_model = _CUSTOMIZER_MODEL.replace("anthropic/", "")
        router_llm = make_anthropic_native_client(model=router_model)
        customizer_llm = make_anthropic_native_client(model=customizer_model)
    elif _BACKEND == "openrouter":
        router_llm = make_openrouter_client(model=_ROUTER_MODEL)
        customizer_llm = make_openrouter_client(model=_CUSTOMIZER_MODEL)
    else:
        router_llm = make_default_client()
        customizer_llm = make_default_client()

    orchestrator = FastPathOrchestrator(
        router=Router(router_llm),
        customizer=Customizer(customizer_llm),
        compiler=RecipeCompiler(
            _TEMPLATE_DIR,
            addon_dirs=(_TEMPLATE_FASTPATH_DIR, _TEMPLATE_SLOWPATH_DIR),
        ),
        confidence_threshold=0.7,
    )

    results: list[dict] = []

    for label, prompt, expected in PROMPTS:
        print(f"── {label} ─────────────────────────────────────")
        print(f"  prompt: {_short(prompt, 60)}")
        print(f"  expected: archetype={expected!r}")
        t0 = time.monotonic()
        try:
            outcome = await orchestrator.try_create(
                user_text=prompt,
                input_mode="body_pose",
                target_dir=out_root / label,
                request_id=f"smoke-{label}",
                user_id="smoke-user",
            )
        except Exception as e:  # pragma: no cover
            print(f"  ❌ EXCEPTION: {e}")
            results.append({"label": label, "error": str(e)})
            continue
        wall_ms = int((time.monotonic() - t0) * 1000)

        is_correct = (outcome.archetype_id == expected) if expected else (not outcome.hit)
        mark = "✅" if is_correct else "❌"
        print(
            f"  {mark} hit={outcome.hit} arch={outcome.archetype_id} "
            f"conf={outcome.confidence:.2f} fallback={outcome.fallback_reason}"
        )
        if outcome.hit and outcome.recipe is not None:
            r = outcome.recipe
            print(
                f"     title={r.meta.title_i18n['en']!r} / {r.meta.title_i18n['zh-Hans']!r} "
                f"icon={r.meta.icon}"
            )
            # Phase 2 W1.5：sprite 字段是 emoji 字符，不是 catalog ID
            print(
                f"     theme={r.theme.palette}/{r.theme.background_hint} "
                f"player={r.entities.player.emoji or r.entities.player.sprite_hint} "
                f"drop={r.entities.drop.emoji or r.entities.drop.sprite_hint} "
                f"obstacle={r.entities.obstacle.emoji or r.entities.obstacle.sprite_hint}"
            )
            print(
                f"     params: spawn={r.params.spawn_rate_per_sec} "
                f"fall=[{r.params.fall_speed_min},{r.params.fall_speed_max}] "
                f"win={r.params.win_score} lives={r.params.max_lives}"
            )
        print(
            f"     tokens: router={outcome.router_tokens_in}/{outcome.router_tokens_out} "
            f"customizer={outcome.customizer_tokens_in}/{outcome.customizer_tokens_out} "
            f"cache_read={outcome.customizer_cache_read_input_tokens}"
        )
        print(
            f"     latency: router={outcome.router_duration_ms}ms "
            f"customizer={outcome.customizer_duration_ms}ms "
            f"compile={outcome.compiler_duration_ms}ms "
            f"wall={wall_ms}ms"
        )

        results.append({
            "label": label,
            "prompt": prompt,
            "expected": expected,
            "correct": is_correct,
            "hit": outcome.hit,
            "archetype": outcome.archetype_id,
            "confidence": outcome.confidence,
            "fallback_reason": outcome.fallback_reason,
            "router_tokens_in": outcome.router_tokens_in,
            "router_tokens_out": outcome.router_tokens_out,
            "customizer_tokens_in": outcome.customizer_tokens_in,
            "customizer_tokens_out": outcome.customizer_tokens_out,
            "customizer_cache_read": outcome.customizer_cache_read_input_tokens,
            "router_ms": outcome.router_duration_ms,
            "customizer_ms": outcome.customizer_duration_ms,
            "compile_ms": outcome.compiler_duration_ms,
            "wall_ms": wall_ms,
            "title_en": outcome.recipe.meta.title_i18n["en"] if outcome.recipe else None,
            "title_zh": outcome.recipe.meta.title_i18n["zh-Hans"] if outcome.recipe else None,
            "player_emoji": (outcome.recipe.entities.player.emoji or outcome.recipe.entities.player.sprite_hint) if outcome.recipe else None,
            "drop_emoji": (outcome.recipe.entities.drop.emoji or outcome.recipe.entities.drop.sprite_hint) if outcome.recipe else None,
            "obstacle_emoji": (outcome.recipe.entities.obstacle.emoji or outcome.recipe.entities.obstacle.sprite_hint) if outcome.recipe else None,
            "palette": outcome.recipe.theme.palette if outcome.recipe else None,
            "background": outcome.recipe.theme.background_hint if outcome.recipe else None,
        })

    # ─── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = len(results)
    correct = sum(1 for r in results if r.get("correct"))
    catch_prompts = [r for r in results if r.get("expected") == "catch_v2"]
    catch_hits = sum(1 for r in catch_prompts if r.get("hit"))
    non_catch_prompts = [r for r in results if r.get("expected") is None]
    non_catch_correct = sum(1 for r in non_catch_prompts if not r.get("hit"))

    print(f"correctness: {correct}/{total}")
    if catch_prompts:
        print(f"catch_v1 hit rate (expected catch): {catch_hits}/{len(catch_prompts)}")
    if non_catch_prompts:
        print(f"non-catch correctly fell through: {non_catch_correct}/{len(non_catch_prompts)}")

    # 多样性：catch_v1 命中里的 drop_sprite / palette / title 去重率
    catch_hit_results = [r for r in results if r.get("hit")]
    if catch_hit_results:
        drops = [r["drop_emoji"] for r in catch_hit_results]
        palettes = [r["palette"] for r in catch_hit_results]
        titles_en = [r["title_en"] for r in catch_hit_results]
        print(f"diversity (over {len(catch_hit_results)} catch hits):")
        print(f"  drop_emoji unique: {len(set(drops))}/{len(drops)}  values={drops}")
        print(f"  palette unique:    {len(set(palettes))}/{len(palettes)}  values={palettes}")
        print(f"  title_en unique:   {len(set(titles_en))}/{len(titles_en)}")

    # Aggregate cost-related stats
    fast_hits = [r for r in results if r.get("hit")]
    if fast_hits:
        avg_total_tokens = sum(
            r["router_tokens_in"] + r["router_tokens_out"]
            + r["customizer_tokens_in"] + r["customizer_tokens_out"]
            for r in fast_hits
        ) / len(fast_hits)
        avg_wall = sum(r["wall_ms"] for r in fast_hits) / len(fast_hits)
        avg_cache_read = sum(r["customizer_cache_read"] for r in fast_hits) / len(fast_hits)
        print(f"avg tokens / fast hit: {avg_total_tokens:.0f}")
        print(f"avg wall-clock / fast hit: {avg_wall:.0f}ms")
        print(f"avg customizer cache_read tokens: {avg_cache_read:.0f}")

    # Dump full results JSON
    (out_root / "_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nfull results: {out_root / '_results.json'}")
    print(f"compiled games: {out_root}/<label>/")

    # ─── P2.3：v0.2 directors / tags / hidden_tech audit ──────────────────────
    await _run_v02_audit(orchestrator, out_root)


# ─── P2.3：v0.2 字段命中率验证 ──────────────────────────────────────────────

# 每条 prompt 包含中文/英文触发词，强烈暗示某个 v0.2 字段应该被 LLM 选中。
# expected_directors：必须包含的 director（subset 即可，LLM 可加其他）
# expected_hidden_tech：必须等于此值（None = 不检查）
# expected_tags：dict[entity_field_name -> expected_tag]，匹配命中算 hit
_V02_PROMPTS: list[dict] = [
    # ── Director 命中（10 个）──
    {"label": "v02_tense_catch", "prompt": "做一个紧张刺激的接苹果游戏",
     "archetype": "catch_v2", "directors": {"tense"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_relaxing_catch", "prompt": "猴子接香蕉，要轻松解压",
     "archetype": "catch_v2", "directors": {"relaxing"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_chaotic_catch", "prompt": "狗狗接包子，魔性混乱的那种",
     "archetype": "catch_v2", "directors": {"chaotic"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_gravity_dodge", "prompt": "重力反转的飞船躲陨石",
     "archetype": "dodger_v2", "directors": {"gravity_flip"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_slowmo_slice", "prompt": "慢动作切水果，看清每个切片",
     "archetype": "slicer_v2", "directors": {"slowmo"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_magnet_catch", "prompt": "磁铁吸金币的小游戏",
     "archetype": "catch_v2", "directors": {"magnet"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_dark_whack", "prompt": "黑暗中打地鼠，屏幕一会儿闪烁一下",
     "archetype": "whack_v2", "directors": {"dark"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_wind_catch", "prompt": "顺着风的方向接秋天的落叶",
     "archetype": "catch_v2", "directors": {"wind"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_boss_dodge", "prompt": "躲陨石，每隔一分钟会出大 boss",
     "archetype": "dodger_v2", "directors": {"boss_round"}, "hidden_tech": None, "tags": {}},
    {"label": "v02_hardcore_dodge", "prompt": "硬核挑战版的躲陨石，要操作精准",
     "archetype": "dodger_v2", "directors": set(), "hidden_tech": True, "tags": {}},

    # ── Tag 命中（5 个）──
    # 所有 strategy 的 customizer_schema 都复用 catch_v1 的 entities 字段名
    # （player / drop / obstacle），即使语义上 dodger 的 obstacle 叫 hazard、
    # slicer 的 drop 叫 fruit。test 用 schema 实际字段名做断言。
    {"label": "v02_tag_fire_slice", "prompt": "切火球，小心炸弹",
     "archetype": "slicer_v2", "directors": set(), "hidden_tech": None,
     "tags": {"drop": "fire"}},  # slicer 里"水果"是 drop 字段
    {"label": "v02_tag_water_catch", "prompt": "接水滴，躲熔岩",
     "archetype": "catch_v2", "directors": set(), "hidden_tech": None,
     "tags": {"drop": "water", "obstacle": "fire"}},
    {"label": "v02_tag_metal_whack", "prompt": "拍金币（target），别拍炸弹",
     "archetype": "whack_v2", "directors": set(), "hidden_tech": None,
     "tags": {"drop": "metal"}},  # whack 里"target"是 drop 字段
    {"label": "v02_tag_grass_catch", "prompt": "猴子接树叶，避开石头",
     "archetype": "catch_v2", "directors": set(), "hidden_tech": None,
     "tags": {"drop": "grass"}},
    {"label": "v02_tag_explo_dodge", "prompt": "躲下落的炸弹和烟花",
     "archetype": "dodger_v2", "directors": set(), "hidden_tech": None,
     "tags": {"obstacle": "explosive"}},  # dodger 里"hazard"是 obstacle 字段
]


async def _run_v02_audit(orchestrator, out_root: Path) -> None:
    """跑 15 个 v0.2 prompt，统计 director / hidden_tech / tag 命中率。
    用户要的命中率门槛是 80%；低于此值表示 customizer prompt / cheatsheet 需要 tune。
    """
    print("\n" + "=" * 70)
    print("P2.3 — v0.2 SMOKE AUDIT (directors / tags / hidden_tech)")
    print("=" * 70)

    v02_results: list[dict] = []
    for spec in _V02_PROMPTS:
        label = spec["label"]
        print(f"── {label} ─────────────────────────────────────")
        print(f"  prompt: {_short(spec['prompt'], 60)}")
        exp_dirs = sorted(spec["directors"])
        exp_tags = spec["tags"]
        print(f"  expect: archetype={spec['archetype']} directors={exp_dirs} "
              f"hidden_tech={spec['hidden_tech']} tags={exp_tags}")
        try:
            outcome = await orchestrator.try_create(
                user_text=spec["prompt"],
                input_mode="body_pose",
                target_dir=out_root / label,
                request_id=f"v02-{label}",
                user_id="smoke-user",
            )
        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            v02_results.append({"label": label, "error": str(e)})
            continue

        if not outcome.hit or outcome.recipe is None:
            print(f"  ❌ NO HIT (archetype expected={spec['archetype']!r}, got={outcome.archetype_id!r})")
            v02_results.append({
                "label": label,
                "archetype_ok": False,
                "directors_ok": False,
                "hidden_tech_ok": False,
                "tag_hits": 0,
                "tag_total": len(exp_tags),
            })
            continue

        r = outcome.recipe
        got_dirs = {d.value for d in r.directors}
        archetype_ok = (outcome.archetype_id == spec["archetype"])
        # directors：期望的全部命中即算 hit（LLM 可多选其他 modifier，不扣分）
        directors_ok = spec["directors"].issubset(got_dirs)
        # hidden_tech：None 跳过；否则严格 ==
        if spec["hidden_tech"] is None:
            hidden_tech_ok = True
        else:
            hidden_tech_ok = (r.hidden_tech_enabled == spec["hidden_tech"])
        # tag：检查 expected 中每个 entity 字段
        ent_dump = r.entities.model_dump()
        tag_hits = 0
        for ent_name, expected_tag in exp_tags.items():
            ent = ent_dump.get(ent_name)
            if isinstance(ent, dict) and ent.get("tag") == expected_tag:
                tag_hits += 1

        marks = []
        marks.append("✅ARCH" if archetype_ok else "❌ARCH")
        marks.append("✅DIR" if directors_ok else "❌DIR")
        marks.append("✅HT" if hidden_tech_ok else "❌HT")
        if exp_tags:
            marks.append(f"{'✅' if tag_hits == len(exp_tags) else '❌'}TAG({tag_hits}/{len(exp_tags)})")
        print(f"  {' '.join(marks)}")
        print(f"  got: directors={sorted(got_dirs)} "
              f"hidden_tech={r.hidden_tech_enabled} "
              f"entity_tags={ {k: v.get('tag') for k, v in ent_dump.items() if isinstance(v, dict)} }")

        v02_results.append({
            "label": label,
            "archetype_ok": archetype_ok,
            "directors_ok": directors_ok,
            "hidden_tech_ok": hidden_tech_ok,
            "tag_hits": tag_hits,
            "tag_total": len(exp_tags),
            "got_directors": sorted(got_dirs),
            "got_hidden_tech": r.hidden_tech_enabled,
        })

    # ─── v0.2 Summary ─────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("v0.2 AUDIT SUMMARY")
    print("─" * 70)
    total = len(v02_results)
    arch_ok = sum(1 for r in v02_results if r.get("archetype_ok"))
    dir_ok = sum(1 for r in v02_results if r.get("directors_ok"))
    ht_ok = sum(1 for r in v02_results if r.get("hidden_tech_ok"))
    tag_attempted = sum(r.get("tag_total", 0) for r in v02_results)
    tag_hit = sum(r.get("tag_hits", 0) for r in v02_results)

    def _pct(n, d):
        return f"{n}/{d} ({100*n/d:.0f}%)" if d else f"{n}/{d}"

    print(f"archetype:        {_pct(arch_ok, total)}")
    print(f"directors:        {_pct(dir_ok, total)}")
    print(f"hidden_tech:      {_pct(ht_ok, total)}")
    print(f"entity tag:       {_pct(tag_hit, tag_attempted)}")
    overall_pass = arch_ok + dir_ok + ht_ok + tag_hit
    overall_total = total * 3 + tag_attempted  # 3 axes per prompt + each tag
    print(f"\noverall:          {_pct(overall_pass, overall_total)}")
    target = 0.80
    if overall_total and (overall_pass / overall_total) >= target:
        print(f"✅ PASS — overall ≥ {int(target*100)}%, ready for Phase 3")
    else:
        print(f"❌ MISS — overall < {int(target*100)}%, tune customizer prompt before Phase 3")

    (out_root / "_v02_results.json").write_text(
        json.dumps(v02_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
