# 游戏创建 Fast Path 实施方案

## 背景

### 当前痛点

`create_game` 走 Claude Code SDK 多轮 agent loop 生成 Godot 工程，慢且贵：

- **成本**：单次 create $0.10-0.30（OpenRouter 真实账单，Sonnet/Haiku 混跑）
- **时延**：wall-clock 60-180s，超时上限 900s
- **本质问题**：让大模型现场写 GDScript，对绝大多数用户请求是大材小用——80% 都落在少数几个固定玩法骨架上，差异只在主题、参数、文案

### 已有的优化（保留，不冲突）

- `docs/modify-game-cost-optimization.md`：解决 modify 路径的 session resume cache miss（治 modify）
- `_template/` skeleton hook 模式：AI 只填 3 个必填 hook + 0-5 个可选 override（治"AI 写多少代码"）
- `streaming.py` 的 `_extract_game_meta()` + `_load_official_templates()`：已经能用 Haiku 抽 title/icon/template_id（**当前方案的天然接入点**）
- `_templates/_registry.json`：注册表入口已存在，但目录是空的

### 本方案要解决什么

**只治 `create_game` 路径**：让服务端在用户输入文本时自动识别玩法意图，命中预制 archetype 时跳过整个 Claude Code SDK 走确定性拼装；不命中时无缝 fallback 到现有 SDK。**iOS UI 完全不变**，用户继续输入文本，由服务端隐式分流。

不在本方案范围：

- ❌ Modify 路径切换（modify 继续走现有 SDK + session resume）
- ❌ Microgame 聚合层 / Lv0 主厨推荐 / 付费墙等产品形态改动
- ❌ iOS UI 改造

## 目标指标

| 指标 | 现状 | Phase 3 完成后目标 |
|---|---|---|
| Fast path 命中率（覆盖 4 archetype） | 0% | 70-80% |
| Fast path 单次成本 | — | ~$0.02（含 Router + Customizer 两次 LLM） |
| Fast path 单次时延 (p50) | — | < 10s |
| Slow path（fallback SDK）单次成本 | $0.10-0.30 | 不变（≤20% 流量） |
| Create 加权平均成本 | $0.10-0.30 | $0.03-0.05（**3-6× 降本**） |
| Create 加权平均 p50 时延 | 60-180s | 15-25s（**4-6× 提速**） |

## 总体架构

### 设计原则：Archetype 是骨架，LLM 是个性化层

**❌ 错误理解**：命中 archetype → 直接拼装 → 0 LLM → 所有"接物游戏"长得一样
**✅ 正确设计**：命中 archetype 决定**玩法骨架**，LLM 在 archetype 框架内决定**所有个性化字段**（标题/双语描述/sprite 选择/配色/难度参数/音效），保证"接香蕉"和"接金币"是视觉、文案、手感都不同的两款游戏。

### 新链路示意

```
用户输入文本
   │
   ▼
[Router]              ← 第 1 次 LLM 调用：Haiku 极轻量分类
                         只输出 {archetype_id, confidence}
   │
   ▼
[置信度网关]
   │
   ├─ confidence ≥ 阈值
   │     │
   │     ▼
   │  [Customizer]    ← 第 2 次 LLM 调用：Haiku/Sonnet 单轮 structured output
   │     │              输入：archetype schema + 用户原文 + asset/audio catalog
   │     │              输出：完整 GameRecipe（含所有个性化字段）
   │     ▼
   │  [Compiler]      ← 纯 Python，0 LLM
   │     │              GameRecipe → Godot 工程目录（填 hook + 选 asset + 写 i18n）
   │     ▼
   │  game_ready      （fast path 总耗 ~10s，~$0.02）
   │
   └─ confidence < 阈值 → [现有 SDK 链路] → game_ready（slow path，不动）
```

### 为什么拆成两次 LLM 调用

- **Router 便宜快速**：只做分类决策，不浪费 token 在不确定要走 fast path 的请求上
- **Customizer 聚焦**：confirmed 走 fast path 后，prompt 只关心"这个 archetype 的字段怎么填"，schema 更聚焦，cache 友好
- **失败止损**：低置信度直接转 slow path，不付 Customizer 的钱
- **总成本可控**：Router $0.001 + Customizer $0.015-0.02 = ~$0.02 / fast hit；fallback 不付 Customizer

### Asset Catalog 机制（保证视觉差异化的关键）

Compiler 是确定性的，但**资源池本身是丰富的**。Phase 1 起点：

- **角色 sprite catalog**：~10 个常见角色（monkey/dog/cat/robot/wizard/ninja/astronaut/...）
- **物体 sprite catalog**：~20 个常见可接 / 可躲物（banana/apple/coin/star/heart/bomb/meteor/...）
- **背景 catalog**：~6 个主题背景（beach/forest/space/city/cave/underwater）
- **配色 catalog**：~6 个 palette 枚举
- **音效**：复用现有 `_build_audio_section()` 的 manifest

Customizer 输出 `sprite_hint: "banana"` 等字符串，Compiler 用 fuzzy match 从 catalog 里挑最接近的（找不到匹配时退到通用默认）。**asset 不命中 catalog 不会失败，只是退化**。

后期可演进：增加图像生成（SD / DALL-E）让 Customizer 描述任意 sprite，但这不是 Phase 1-3 范围。

### 数据契约：GameRecipe JSON

Customizer 的输出 = Compiler 的输入。一份稳定的 JSON schema，分阶段扩展：

```json
{
  "schema_version": "0.1",
  "archetype_id": "catch_v1",
  "confidence": 0.92,
  "meta": {
    "title_i18n": {"en": "Banana Catcher", "zh-Hans": "接香蕉"},
    "description_i18n": {"en": "...", "zh-Hans": "..."},
    "icon": "🍌"
  },
  "theme": {
    "palette": "warm",
    "background_asset_hint": "sunny_beach"
  },
  "params": {
    "spawn_rate_per_sec": 1.8,
    "fall_speed_min": 200,
    "fall_speed_max": 400,
    "win_score": 30,
    "max_lives": 3
  },
  "entities": {
    "player": {"sprite_hint": "monkey", "control": "face_x"},
    "drop": {"sprite_hint": "banana", "score_value": 1},
    "obstacle": {"sprite_hint": "bomb", "lives_cost": 1}
  },
  "audio": {
    "bgm": "bgm_casual_loop_1",
    "sfx_pickup": "pickup",
    "sfx_hit": "hit",
    "sfx_gameover": "level_fail"
  }
}
```

**演进规则**：

- 新增字段从尾部追加，旧 Compiler 按字段忽略 → 向后兼容
- 不兼容时升 `schema_version`，Compiler 按版本号分支
- 字段值域全部由 Pydantic 校验，超出范围自动 clamp 到默认

## 关键组件

### 1. Router（`robot/games/fastpath/router.py`）

**职责**：第 1 次 LLM 调用，做**纯分类决策**。输入用户文本 + input_mode，输出 `{archetype_id, confidence}`，**不输出任何个性化字段**。

**实现**：

- 模型：Haiku 4.5（OpenRouter 实测 ~$0.001/次）
- 调用方式：**单轮 chat completion**，`response_format=json_schema` 严格约束
- System prompt 极简：列出所有已注册 archetype（id + 一句话描述 + 2-3 个典型用户输入示例）+ "如果都不匹配，archetype_id 返回 `null`"
- 用户消息：原始用户文本 + input_mode
- Token 量：input ~1.5K（cache 友好），output < 50 tokens

**置信度建模**：

- Haiku 在 schema 中被要求自评 `confidence ∈ [0, 1]`
- 后端叠加规则校验：archetype_id 是否在注册表内（不在则强制 0）
- 综合后落到 [0, 1]

**Cache 策略**：Router 的 system prompt 极稳定（archetype 描述变更频率极低），prompt cache 命中率应达 95%+。

### 2. Customizer（`robot/games/fastpath/customizer.py`）

**职责**：第 2 次 LLM 调用（仅置信度 ≥ 阈值时触发）。把用户原文 + archetype 的 schema 槽位 → **完整结构化的 GameRecipe JSON**。所有个性化字段都由 Customizer 输出：标题、双语描述、emoji、palette、难度参数、音效。Compiler 拿这个 JSON 直接拼 Godot 工程。

**实现**：

- 模型：Haiku 4.5（Phase 1 实测足够）；通过 `make_openrouter_client(model=settings.game_modify_model)` 构造
- 调用方式：**单轮 chat completion**，`response_format={"type": "json_object"}`
- System prompt 包含：
  - 该 archetype 的 JSON schema（来自 `strategy.customizer_schema`）
  - 约束值 catalog（palette / background / bgm / sfx 的合法 enum 列表）
  - 2-3 个 few-shot 示例 recipe（来自 `strategy.customizer_examples`）
  - 关键 emoji 选取规则（"包子→🥟 / 寿司→🍣 / 火锅→🍲"）
- 用户消息：原始用户文本 + 来自 Router 的 archetype_id
- Token 量：input ~2.5-3K（schema + catalog + examples），output ~350-500
- 单价：~$0.003-0.005 / 调用（Haiku 4.5 OpenRouter）

**实例 — "狗接包子" Customizer 实际产出**：

```json
{
  "schema_version": "0.1",
  "archetype_id": "catch_v1",          // 由 Router 决定，Customizer 不改
  "confidence": 0.95,

  "meta": {
    "title_i18n": {"en": "Puppy Buns", "zh-Hans": "小狗接包子"},
    "description_i18n": {"en": "Help the pup catch falling buns!",
                         "zh-Hans": "帮小狗接住掉下来的包子！"},
    "icon": "🥟"
  },
  "theme": {
    "palette": "warm",            // 6 个 palette enum 选 1
    "background_hint": "city"     // 6 个 background enum 选 1
  },
  "entities": {
    "player":   {"emoji": "🐶"},                     // 任何 Unicode emoji
    "drop":     {"emoji": "🥟", "score_value": 1},   // 接到 +1 分
    "obstacle": {"emoji": "🔥", "lives_cost": 1}     // 撞到 -1 命
  },
  "params": {
    "spawn_rate_per_sec": 1.6,     // LLM 根据用户描述推断难度
    "fall_speed_min": 200, "fall_speed_max": 380,
    "obstacle_probability": 0.22,
    "win_score": 25, "max_lives": 4
  },
  "audio": {
    "bgm": "bgm_playful",          // catalog 8 个 BGM 选 1
    "sfx_pickup": "pickup", "sfx_hit": "hit", "sfx_gameover": "level_fail"
  }
}
```

**输出字段决策矩阵**：

| 字段 | LLM 自由度 | 约束 |
|---|---|---|
| `meta.title_i18n` / `description_i18n` / `icon` | 完全自由 | 无 |
| `entities.*.emoji` | 完全自由（Phase 2 W1.5 起） | 任何 Unicode emoji，Compiler PIL 现场渲染 |
| `theme.palette` / `background_hint` | enum 6 选 1 | catalog enum 严格约束 |
| `audio.bgm` / `sfx_*` | enum 8/10 选 1 | catalog enum 严格约束 |
| `params.*` | 区间内自由 | Pydantic schema range 校验 |

**校验降级**（piecewise rebuild）：

LLM 输出后 Pydantic 校验。整体合法 → 直接用；部分子字段越界（例如 `params.fall_speed_max < min` 触发 cross-field 校验失败）→ **只把那个 sub-field 降级到默认值**，其他 LLM 输出全保留。LLM 整段崩了（JSON 解析失败 / 关键 key 全缺）→ raise `CustomizerError` → orchestrator fallback 到 slow path。

**Cache 策略**：每个 archetype 的 Customizer system prompt 是稳定 prefix（schema + catalog + examples 几乎不变），理论上 cache 命中率 90%+。**实测在 OpenRouter 非 BYOK 账户上 cache 不工作**（账户级关闭，详见路径 D 探讨笔记），所以当前每次都付全价。BYOK 切到 anthropic.com 直连后才能省。

### 3. Recipe Compiler（`robot/games/fastpath/compiler.py`）

**职责**：GameRecipe JSON → 完整 Godot 工程目录。**纯 Python，零 LLM 调用**，30-300ms 完成。

**5 个步骤**（继续用"狗接包子" recipe 做例子）：

#### Step 1 — 校验 archetype，拷 skeleton

```python
strategy_cls = STRATEGIES.get(recipe.archetype_id)  # → CatchV1Strategy
shutil.copytree(template_dir, target_dir)           # _template/ → ai_xxx/
```

target_dir 现在是 `_template/` 的完整副本：`project.godot` / `scenes/main.gd`（含 plumbing + 默认 `pass # TODO(AI)`）/ `scripts/{audio_manager,game_manager,i18n,i18n_strings,input_bridge}.gd` / `assets/background.png` / `audio/`（空）/ `fonts/` / `CLAUDE.md`。

#### Step 2 — 改写 main.gd（核心步骤）

```python
skeleton = main_gd_path.read_text()
truncated = _truncate_at_ai_zone(skeleton)            # 切到 AI ZONE 之前
ai_zone   = strategy.render_ai_zone(recipe, catalog)  # 渲染 archetype 专属 GDScript
main_gd_path.write_text(truncated + ai_zone)
```

`_truncate_at_ai_zone` 找到 `# REQUIRED: called once when player taps to start` 这行就切掉之后所有内容（默认占位 hooks）。

`strategy.render_ai_zone(recipe)` 把模板里的 `__TOKEN__` 占位符替换成 recipe 里的具体值：

```python
"score += __SCORE_VALUE__"          → "score += 1"
"randf_range(__FALL_MIN__, __FALL_MAX__)"  → "randf_range(200.0, 380.0)"
'bgm_track = "__BGM__"'             → 'bgm_track = "bgm_playful"'
```

最终 main.gd 头部 ~280 行 plumbing 不变，尾部是为这个游戏量身渲染的玩法逻辑（catch_v1 ~100 行，whack/dodger 类似）。

#### Step 3 — 渲染 sprite assets（emoji → PNG）

```python
strategy.prepare_assets(recipe, catalog, target_dir)
```

catch_v1 的 `prepare_assets` 调用 PIL + Apple Color Emoji 现场渲染：

```python
emoji_renderer.render_emoji_to_png("🐶", target_dir / "assets/player.png")
emoji_renderer.render_emoji_to_png("🥟", target_dir / "assets/drop.png")
emoji_renderer.render_emoji_to_png("🔥", target_dir / "assets/obstacle.png")
```

每张 96×96 透明 PNG，~50-100ms。空 emoji 字段退化到 `sprite_hint` + catalog 预渲染 PNG（legacy 兼容）。

#### Step 4 — 覆盖 i18n_strings.gd

skeleton 自带的 i18n_strings.gd 是默认值（"Game" / "游戏"）。Compiler 用 strategy 渲染的覆盖：

```python
i18n_path.write_text(strategy.render_i18n_strings(recipe))
```

输出含 recipe.meta.title_i18n 双语 + 默认的 start_hint / retry_hint / score / lives 双语 keys。

#### Step 5 — 写 meta.json（含 recipe 快照）

```json
{
  "schema_version": "0.1",
  "archetype_id": "catch_v1",
  "provenance": "fastpath",            // ← 跟 SDK 路径区分（modify 路径会读）
  "created_at": 1778597477,
  "title": "Puppy Buns",
  "title_i18n": {"en": "Puppy Buns", "zh-Hans": "小狗接包子"},
  "icon": "🥟",
  "recipe": { ...完整 GameRecipe 快照... }
}
```

`provenance: "fastpath"` 让以后的 modify 路径能识别"这个游戏是 fast path 出的，可以走 recipe-patch 而不是 SDK 改代码"。

#### 完整产出对照（compile 前 vs 后）

```
Compile 之前 (拷完 skeleton):
  scenes/main.gd          ~307 行 skeleton 含 pass # TODO(AI)
  scripts/i18n_strings.gd 默认空模板
  meta.json               不存在
  assets/                 只有 background.png

Compile 之后:
  scenes/main.gd          ~280 行 plumbing + ~100 行 catch_v1 实现
  scripts/i18n_strings.gd "小狗接包子" 双语版
  meta.json               含完整 recipe 快照 + provenance="fastpath"
  assets/
    background.png        (skeleton 自带)
    player.png            🐶 emoji 渲的 PNG
    drop.png              🥟
    obstacle.png          🔥
```

#### 关键设计点

1. **Strategy delegate 模式**：Compiler 自己不知道 catch / dodger / whack 的差异，全部委托 strategy 实现 `render_ai_zone` / `render_i18n_strings` / `prepare_assets`。**加新 archetype = 新写一个 strategy 类，Compiler 0 行改动**。
2. **Truncate-and-append**：保留 skeleton ~280 行 plumbing 不变，只动尾部 AI ZONE。所有"游戏外壳"（state machine / HUD / 字体 / i18n routing / body-pose helper）都是 Phase 1 验证过的硬编码，AI 永远改不坏。
3. **Token 替换 vs Jinja**：用 `__TOKEN__` 占位符 + `str.replace`，不引 Jinja 依赖，也避免 `{}` 跟 GDScript Dictionary 字面量冲突。
4. **失败语义**：Compiler 抛 `CompileError` → orchestrator 清理 game_dir → fallback 到 slow path。**绝不部分写盘**——target_dir 已存在直接 raise（防覆盖正在跑的游戏）。
5. **零 LLM**：纯 Python str + PIL render + shutil copy。可单测、可复现、可 debug。

#### 性能拆解

| 步骤 | 耗时 |
|---|---|
| copytree skeleton | ~10ms |
| truncate + render_ai_zone + write main.gd | ~5ms |
| 3 张 emoji PNG render | 150-300ms（PIL + LANCZOS resize） |
| write i18n_strings.gd | ~1ms |
| write meta.json | ~1ms |
| **合计** | **~170-320ms** |

跟整条 fast path（~17s）比，Compiler 是 1-2% 的时间，剩下都是 LLM 等待。

#### 模块结构（实际落地）

```
robot/games/fastpath/
  schemas.py             # GameRecipe Pydantic + RouterDecision + CompileResult
  catalog.py             # AssetCatalog (palette / background / bgm / sfx enum)
  emoji_renderer.py      # PIL + Apple Color Emoji → PNG
  compiler.py            # RecipeCompiler 主流程
  strategies/
    base.py              # ArchetypeStrategy 抽象基类
    catch_v1.py          # Phase 1 接物
    dodger_v1.py         # Phase 2 躲避
    whack_v1.py          # Phase 3 W1 拍打
    # slicer_v1.py       # Phase 3 W2-W3（未实现，待 iOS 手势引擎）
```

#### Strategy 抽象接口

```python
class ArchetypeStrategy(ABC):
    archetype_id: ClassVar[str]
    router_description: ClassVar[str]      # 喂 Router 的一句话分类描述
    customizer_schema: ClassVar[dict]      # 喂 Customizer 的 JSON schema
    customizer_examples: ClassVar[list]    # 喂 Customizer 的 few-shot examples

    @abstractmethod
    def render_ai_zone(self, recipe, catalog) -> str: ...      # main.gd AI ZONE
    @abstractmethod
    def render_i18n_strings(self, recipe) -> str: ...          # i18n_strings.gd

    def prepare_assets(self, recipe, catalog, target_dir) -> list[str]:
        """渲染 / 拷贝 sprite assets 到 target_dir/assets/。默认实现走
        legacy required_assets() 拷 catalog 预渲染 PNG。"""
```

### 4. 置信度网关（`robot/agent/streaming.py` 内嵌）

**职责**：在 `_handle_game_direct("create", ...)` 入口处做分流。

**位置**：替换 `_extract_game_meta()` 调用，改为依次：Router → 网关 → (Customizer + Compiler) 或 (SDK fallback)。

**伪逻辑**：

```
decision = await Router.route(user_text, input_mode)
if decision.archetype_id and decision.confidence >= CONFIDENCE_THRESHOLD:
    try:
        recipe = await Customizer.customize(user_text, decision.archetype_id)
        result = await RecipeCompiler.compile(recipe, ...)
        emit("fast_path_hit", decision.archetype_id)
        return result
    except CompilerError as e:
        emit("fast_path_compile_failure", error=str(e), archetype=decision.archetype_id)
        # fall through to slow path
    except CustomizerError as e:
        emit("fast_path_customizer_failure", error=str(e))
        # fall through to slow path
emit("slow_path_fallback", reason=decision.confidence_or_none)
return await create_game.ainvoke({...})  # 现有 SDK 链路，不动
```

**阈值起点**：0.7（保守起步，宁可 fallback 多一些）；后续按观测面板数据调整。

### 5. Token / Trace 观测面板（**Day-1 必须有**）

**为什么必须 Day 1 上线**：没有观测就没有路由器调优依据，整个项目的"5× 降本"是 PPT。

**字段**：

| 字段 | 来源 |
|---|---|
| `request_id` | UUID |
| `user_id` | context |
| `user_text` | 原始输入（脱敏） |
| `input_mode` | touch / body_pose |
| `route_decision` | fast / slow |
| `archetype_id` | router 输出，或 null |
| `confidence` | router 自评 + 后端校验后的综合值 |
| `fallback_reason` | low_confidence / customizer_error / compile_error / null |
| `router_tokens_in/out` | 第 1 次 LLM 消耗 |
| `router_cost_usd` | 同上 |
| `customizer_tokens_in/out` | 第 2 次 LLM 消耗（仅 fast path 触发） |
| `customizer_cost_usd` | 同上 |
| `customizer_cache_read_input_tokens` | 关键 cache 指标 |
| `sdk_tokens_in/out` | slow path 时 |
| `sdk_cost_usd` | 同上 |
| `sdk_cache_read_input_tokens` | 同上 |
| `sdk_cache_creation_input_tokens` | 同上 |
| `compiler_duration_ms` | fast path |
| `sdk_duration_ms` | slow path |
| `wall_clock_ms` | 端到端 |
| `final_outcome` | game_ready / failed |

**落盘**：先简单 append 到 `logs/game_creation.jsonl`，后续按需上 ClickHouse / Grafana。

**Dashboard 关键图表**：

- 每日 fast path / slow path 流量比
- 每个 archetype 的命中数 + confidence 分布
- fallback 原因分布
- fast vs slow 的成本 / 时延对比箱线图
- Customizer 输出多样性指标（同 archetype 下不同 game 的 title / sprite_hint / palette 是否真的有差异）

## 已实现 archetype（玩法骨架）

archetype 是 Fast Path 内部对"玩法类型"的代号 ID（如 `catch_v1`），**不是用户看到的游戏名**。每个 archetype 对应一套硬编码的 GDScript 模板 + Customizer schema + Router 分类描述，加新 archetype = 加一个 `*_v1.py` strategy 类，**Router 和 Compiler 0 行改动自动支持**。

### 命名规则

| 规则 | 例子 |
|---|---|
| 蛇形小写 + `_v` + 版本号 | `catch_v1` / `dodger_v1` / `whack_v1` |
| 动词为主 | `catch`（不是 `catcher`）/ `dodger` / `whack` / 未来 `slicer` / `runner` |
| 破坏性升级才升版本 | 加道具系统 / 换控制方式 → `catch_v2`（推 v2 后老游戏仍用 v1 strategy） |
| Strategy 类名 PascalCase 对齐 | `CatchV1Strategy` / `WhackV1Strategy` |
| 文件名 snake_case 对齐 | `robot/games/fastpath/strategies/catch_v1.py` |

`provenance=fastpath` + `archetype_id=catch_v1` 写入 `meta.json`，让 modify 路径以后能安全识别"这游戏属于哪套骨架 v 几"。

### catch_v1 — 接物

**玩法**：玩家在屏幕底部横向移动，**接住**从上方落下的好物（drop）得分，**躲开**坏物（obstacle）扣命。得分达 `win_score` 或 `lives == 0` 结束。

**控制**：体感横移——`bridge.face_position.x` 或 `body_lean_x` → 玩家 X（带 lerp 平滑）。触摸不参与玩法（除 tap-to-start / tap-to-restart）。

**实体 slot**：
- `player` — 接东西的角色（猴子 / 篮子 / 飞船 …）
- `drop` — 落下的好物（香蕉 / 金币 / 星星 …），命中 +score_value
- `obstacle` — 落下的坏物（炸弹 / 石头 …），命中 -lives_cost

**评分**：累积型，每接一个 drop 得 `score_value` 分。

**典型用户输入**：「猴子接香蕉」/「狗接包子」/「Catch falling stars」。

### dodger_v1 — 躲避

**玩法**：玩家在屏幕底部横向移动，**全力躲开**所有从上方落下的危险物。没有可接物——纯生存。Score = 存活秒数（int），`win_score` 表示"撑过 N 秒就赢"，`lives == 0` 失败。

**控制**：同 catch_v1（体感横移）。复用同一套左右位移控制器——这是 Phase 2 选 dodger 第二个 archetype 的工程红利来源。

**实体 slot**：
- `player` — 躲东西的角色（飞船 / 猫 / 机器人 …）
- `obstacle` — 要躲的危险物（陨石 / 闪电 / 鱼骨头 …），命中 -lives_cost
- `drop` slot 存在但不渲染（Phase 2 没改 schema；dodger 的 prepare_assets 只拷 player + obstacle 两张 sprite）

**评分**：时间累积型，`score = int(elapsed_seconds)`。

**典型用户输入**：「操控飞船躲开陨石」/「猫躲下落的鱼骨头」/「Dodge falling rocks」。

### whack_v1 — 打地鼠

**玩法**：目标（target）和诱饵（decoy）在屏幕随机位置弹出，每个存活 1.5 秒后自动消失。**击中 target = +score**；**击中 decoy = -life**。错过 target 不扣分也不扣命（只是消失）。

**控制（双模式并存）**：
1. **触屏**：直接 tap 屏幕，命中检测半径 60px
2. **视觉**：黄白十字光标跟随 `bridge.face_position`，停留 0.35s 在目标上 → 自动击中（dwell-to-hit）。绿色进度环显示倒计时
3. 用户可以混用——光标 + 触屏并存，触屏永远是即时命中

**实体 slot**：
- `player` slot 存在但不渲染（whack 没玩家 avatar）
- `drop` — target（要拍的，命中 +score_value）
- `obstacle` — decoy（不要拍的，命中 -lives_cost）

**评分**：累积型，每拍中 target 得 `score_value` 分。

**典型用户输入**：「打地鼠」/「拍小怪兽，别拍小猫」/「Whack-a-mole」。

### 还没实现的 archetype（候选清单）

按市场调研 ROI 排序，等 Phase 3 W2-W3 或 Phase 4 实现：

| ID | 中文 | 工程量 | iOS 改动 | 市场盘子 |
|---|---|---|---|---|
| `slicer_v1` | 挥砍（Fruit Ninja） | 高 | **需要**（iOS Vision 手部 X/Y 追踪） | 🥇 Fruit Ninja 10 亿下载 |
| `runner_v1` | 跑酷（Subway Surfers） | 中-高 | 跳跃手势 | 🥈 Subway Surfers 13 年 10 亿 |
| `rhythm_v1` | 节奏（Beatstar） | 中 | 无 | 🥉 Beatstar / Just Dance |
| `sports_mini_v1` | 体感运动（Wii Sports） | 高 | 多 sub-game | 🏆 Wii Sports 8200 万 |

每个候选都至少有一类用户输入当前会 fall through 到 slow path，量大就值得做 archetype。

## Phase 1：接物模板 + 二分类路由（2-3 周）

### 范围

- Router 仅做**二分类**：`catch_v1` 或 `null`
- Customizer 服务 `catch_v1`，输出完整个性化 GameRecipe
- 1 个精修接物 archetype 端到端跑通
- Asset catalog v0（≥10 个角色 + ≥20 个物体 + 6 个背景 + 6 个 palette）
- 观测面板上线

### 任务分解

**Week 1 — 骨架与 Compiler**

1. 新建模块结构：`robot/games/{router.py, customizer.py, compiler.py, recipes/}`
2. 定义 `GameRecipe` Pydantic schema（v0.1，只包含接物所需字段）
3. 实现 `ArchetypeStrategy` 基类、`AssetCatalog` 模糊匹配
4. Asset catalog v0 资源准备（PNG / OGG 收集 + manifest）
5. 实现 `recipes.catch_v1.CatchStrategy`：能基于**硬编码** recipe 生成可玩接物游戏（先不接任何 LLM）
6. 单测覆盖：schema 校验、Compiler 输出 gdparse 通过、catalog fuzzy match 退化路径、meta.json 字段齐全
7. 落 observability 埋点框架 + `logs/game_creation.jsonl` 写盘

**Week 2 — Router + Customizer 接入**

8. 实现 `Router.route()`：Haiku + JSON schema strict，输出 `{archetype_id, confidence}`，二分类
9. Router system prompt：catch_v1 一句话描述 + 4-6 个典型 / 边界 few-shot
10. 实现 `Customizer.customize()`：Haiku + JSON schema strict（schema 来自 catch_v1.customizer_schema），输出完整 GameRecipe
11. Customizer system prompt：catch_v1 字段说明 + 完整 catalog 序列化 + 6-10 个不同主题的 recipe few-shot
12. 在 `_handle_game_direct("create", ...)` 加入置信度网关分流逻辑：Router → 网关 → Customizer → Compiler，全链路任一失败 fallback 到现有 SDK
13. 集成测试：跑 20 条"接物意图"+ 20 条"非接物意图"用户文本
    - 验证路由准确率 / fallback 路径
    - 验证 20 条接物输入生成的 20 个游戏在 title / sprite_hint / palette / params 上**有真实差异**（避免 Customizer 输出收敛到模板默认值）

**Week 3 — 精修 archetype + 上 staging**

14. 接物 archetype 视觉精修：catalog 扩到 ≥15 个角色 + ≥30 个物体；palette / 音效配齐
15. 配 2-3 个 reference instance（不同主题：水果 / 礼物 / 星星），用 hard-coded recipe 走 Compiler 生成，作为冷启动展示 + 回归测试 fixture
16. 观测面板 Grafana / 简易 HTML dashboard 拼起来
17. 在 staging 环境 dogfood 一周，收集第一批真实路由 + Customizer 输出数据

### 验收标准

- [ ] 用户输入"接苹果 / 接东西 / 接落下的球"类描述 → Router 命中率 ≥ 85%
- [ ] 用户输入明显非接物（"飞机射外星人"）→ confidence < 0.5 → fallback slow path
- [ ] Fast path 端到端 p50 < 10s，单次成本 < $0.025
- [ ] **多样性**：20 个不同的接物意图输入生成 20 个游戏，title / sprite_hint / palette 字段的去重率 ≥ 80%（不允许出现"全部都是接香蕉"的退化）
- [ ] Fast path 失败时 fallback 完全无感（用户感知不到 fast 失败）
- [ ] 观测面板能看到上述所有指标的实时分布

## Phase 2：加躲避 + 多分类（2-3 周）

### 范围

- 第 2 个 archetype：`dodger_v1`（躲避）
- L1 路由器升级为三分类：`catch_v1` / `dodger_v1` / `other`
- Recipe schema 扩展支持躲避字段（player_lives、obstacle_speed_curve 等）

### 关键挑战

**多分类比二分类难得多**——"接物 vs 躲避"边界模糊：

| 用户输入 | 应路由到 |
|---|---|
| "接掉下来的金币" | catch |
| "躲开飞来的陨石" | dodger |
| "操控飞船躲子弹同时收集能量" | 混合，需选主导意图 |
| "在桌面上接东西躲东西" | 多义，宁可 slow path |

**Mitigation**：

- L1 prompt 加 8-12 个边界 example 教模型辨析
- 提高置信度阈值到 0.8（宁可少命中也不要误路由）
- 观测面板必须能区分**路由错误**（archetype_id 不对）和**生成失败**（Compiler 异常）

### 任务分解

1. 设计 `dodger_v1` strategy 和 schema 扩展
2. Recipe schema v0.2 升级，向后兼容（旧接物 recipe 仍可被 v0.2 Compiler 编译）
3. L1 prompt 重写：从二分类改为多分类 + 边界辨析 example
4. 精修躲避 archetype + 2-3 个 reference instance
5. 跑 50 条边界用户输入做路由准确率回归测试
6. 上 staging 验证

### 验收标准

- [ ] Fast path 命中率（合计 catch + dodger）≥ 50%
- [ ] 路由错误率（archetype_id 对换）≤ 5%
- [ ] 整体平均成本继续下降（Phase 1 基础上再 30%+）

## Phase 3：挥砍 + 拍打 + 手势引擎（3-4 周）

### 范围

- 2 个新 archetype：`slicer_v1`（挥砍）/ `whack_v1`（拍打）
- **iOS 手势引擎**（这是这一期最大工程量，跨 iOS / 后端）
- L1 路由器升级为 5 分类
- 首批 4 个 archetype 全部到位

### iOS 手势引擎需求

- 使用 iOS Vision 框架识别：挥手方向 + 速度 / 握拳 / 张开 / 拍击动作
- 1m 距离下识别准确率 > 90%
- 通过 SwiftGodotKit 的 `messageFromHost` 信号将手势数据推到 Godot
- 数据频率 30Hz，延迟 < 50ms

### 关键风险

- 手势识别在 0.5-1.5m 距离的稳定性——如果识别率达不到 90%，挥砍 / 拍打不是好玩法，**宁可砍掉这两个 archetype 也不要带 bug 上线**
- 因此 Phase 3 第一周必须先做手势引擎 PoC，决定是否继续

### 任务分解

**Week 1 — 手势引擎 PoC**

1. iOS 端 Vision API 调研 + 手势分类器原型
2. 实测 0.5 / 1.0 / 1.5m 距离下的识别准确率
3. 决策门：准确率 ≥ 90% → 继续；< 90% → 跳过挥砍/拍打，Phase 3 提前结束

**Week 2-3 — Archetype 实现**

4. 手势引擎完整接入（Swift Vision → Godot bridge）
5. 实现 `slicer_v1` + `whack_v1` strategy
6. 精修两个 archetype + 各 2-3 个 reference instance

**Week 4 — 路由升级 + 上线**

7. L1 路由器升级到 5 分类
8. 跑 100 条用户输入做路由回归
9. 上 staging dogfood

### 验收标准

- [ ] Fast path 命中率 ≥ 70%
- [ ] 4 个 archetype 流量分布相对均衡（无某个 archetype 命中 < 5%）
- [ ] 手势引擎在 0.5-1.5m 识别率 ≥ 90%
- [ ] 整体加权 create 成本 ≤ $0.03，p50 时延 ≤ 20s

## 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| **Router 命中率不达标** | 高 | Phase 1 先二分类调到 90%+ 再扩；置信度阈值宁高勿低；fallback 完全无感 |
| **Customizer 输出收敛 / 千篇一律** | 高 | few-shot example 必须覆盖多主题；验收标准包含"去重率 ≥ 80%"；asset catalog 必须丰富（不能 Phase 1 只有 5 个 sprite）；观测面板加多样性指标持续监控 |
| **Recipe schema 早期设计错误** | 中 | v0.1 只服务接物，迭代式演进；不追求一次到位 |
| **Compiler 生成的游戏质量不及 SDK 路径** | 中 | 每个 archetype 配 2-3 个 reference instance 作为黄金样本；CI 跑 gdparse + Godot import smoke test |
| **手势引擎识别率不足** | 中 | Phase 3 第一周 PoC 拦截；不达标直接砍挥砍/拍打 |
| **路由错误导致用户感觉"AI 不懂我"** | 中 | 置信度阈值保守 + 观测面板区分路由错 vs 生成错；后期可加"猜你想要的是 [接物]，对吗？"微确认 |
| **现有 SDK fallback 路径腐化** | 低 | 永久保留 slow path 作为兜底；CI 持续跑 SDK 路径回归 |

## 与已有工作的关系

| 已有产物 | 关系 |
|---|---|
| `docs/modify-game-cost-optimization.md` | 正交。该方案治 modify，本方案治 create，不冲突 |
| `_template/` skeleton hook 模式 | **复用**。Recipe Compiler 通过填 hook 来生成游戏，不绕过 skeleton |
| `streaming.py::_extract_game_meta()` | **替换**。原来抽 title/icon/template_id，现在拆为 Router（archetype 分类）+ Customizer（完整个性化字段） |
| `streaming.py::_load_official_templates()` | **演进**。原 registry 仅指向"模板 game_id"，新方案以 archetype + strategy 取代 |
| `games/_templates/_registry.json` | **重定义**。改为 archetype 注册表（archetype_id → strategy 类） |
| `game-gen-optimize` worktree（9 paradigm recipes + keyword selector） | **参考**。该 worktree 已有 paradigm / recipe / selector 雏形，落地时优先评估能否直接借用，避免重复造轮子 |
| `game-gen-poc` worktree | **参考**。如有可复用代码片段直接拿 |

## 跨阶段保留事项

- **现有 SDK 链路永不删除**：始终作为 fallback / Escape Hatch 存在
- **`meta.json` 加 `provenance` 字段**：`fastpath` / `slow_sdk`，便于 modify 路径识别游戏来源
- **`schema_version` 严格演进**：Recipe Compiler 必须按版本分支编译，旧 recipe 永远能被新 Compiler 处理

## 不在本方案内

- iOS UI 改造（GameCreatorView 保持纯文本输入）
- Modify 路径切换到 recipe-patch（继续走现有 SDK + session resume 优化）
- Microgame 聚合层
- Lv0 主厨推荐 / 种子游戏池
- Lv3 付费墙
- LoRA / Fine-tune（搁置至少 3 个月）
- Archetype 数量超出 4 个（节奏 / 鬼脸 / Sports Mini / 双人 等留待 Phase 3 数据后决定）

## 总时间线

| 阶段 | 周数 | 累计 | 关键里程碑 |
|---|---|---|---|
| Phase 1 | 2-3 周 | 3 周 | 接物 archetype 上 staging + 观测面板上线 |
| Phase 2 | 2-3 周 | 6 周 | 接物 + 躲避双 archetype 上线 |
| Phase 3 | 3-4 周 | 10 周 | 4 archetype 全到位 + 手势引擎 |

**Phase 1 走通 = 整套架构方向被验证**。后续节奏按观测面板数据微调。

---

## 附录 A：Archetype 选型外部数据调研

### A.1 调研背景

motem 产品尚未上线，没有真实用户数据可以用来判断"哪些品类应该做"。本项目最初基于产品形态约束（桌面 + 体感 + 30-90s 碎片）拍脑袋列了 8 个候选品类：射击 / 接球 / 跑酷 / 弹球 / 躲避 / 节奏点按 / 鬼脸 / 双人。

为避免选型偏差，做了一轮**外部市场代理数据调研**，覆盖 8 个数据源，目标回答：**哪 5-8 个游戏品类既在主流市场被验证过，又最适合 motem 的硬件形态？**

### A.2 数据源摘要

| # | 数据源 | 关键洞察 |
|---|---|---|
| 1 | App Store 美区休闲/街机榜 2025 | Block Blast! 登顶下载榜；Subway Surfers 13 年后 2025 突破 10 亿下载；Geometry Dash / Color Switch 仍稳居 Arcade 常青。AppMagic H1 2025：Puzzle + Arcade 占头部收入 80%+ |
| 2 | Sensor Tower 2025 | Arcade 是下载量第二大品类（2.52B/季度），盈利偏广告，意味着"碎片体验 + 大用户量"池子非常大 |
| 3 | Roblox Top 100（2025） | 头部由 Obby（Tower of Hell）/ Simulator（Pet Sim 99）/ Tycoon 三类瓜分。Obby/Parkour 是其中天然短局的代表 |
| 4 | 抖音小游戏头部（2024-2025） | **跟 motem 形态最贴近的池子**。《抓大鹅》（三消 + 摇手机体感）霸榜数月；《找茬大湿》《节拍大师》《难度飙升》《玩到爽飞》全部 30s-3min/局 |
| 5 | Toca Boca / Sago Mini | 开放沙盒短交互（做菜 / 照顾宠物 / 装扮），手势直觉式交互而非精细操作，针对低龄 + 普通人 |
| 6 | Switch 体感代表作 | Switch Sports（继承 Wii Sports 8200 万销量）、Just Dance 2024、Ring Fit Adventure（6 年王者）、**WarioWare: Move It**（200+ 5-10 秒微游戏，靠 Joy-Con 姿势完成）|
| 7 | Kinect 历史 Top 10 | Kinect Adventures!（2400 万套，史上最畅销）含躲避 / 接物 / 水管漏水 / 激流；Kinect Sports（300 万）；Dance Central（250 万）。**与 motem 物理形态最像**（摄像头 + 体感 + 1-3m 距离） |
| 8 | AR / 前置摄像头游戏 | Fruit Ninja 10 亿+ 下载，Vision Pro 2024 重制版选作手势示范品类；Eyeware Beam 头部追踪验证人脸位置输入可行 |

### A.3 候选品类汇总（17 个）

| # | 品类 | 代表游戏 | 一句话玩法 |
|---|---|---|---|
| 1 | Endless Runner | Subway Surfers, Temple Run | 自动前进 + 左右躲避 + 收集 |
| 2 | Fruit-Slicer 挥砍 | Fruit Ninja | 挥手 / 挥指切飞行物体 |
| 3 | Whack-a-Mole 拍打 | Whack A Mole Mobile, Snappables | 限时打地鼠 / 蹦出物 |
| 4 | Catch-and-Collect 接物 | Kinect Adventures Rallyball | 左右移动接住下落物 |
| 5 | Dodger 躲避 | Geometry Dash, Color Switch | 躲飞来障碍物 |
| 6 | Rhythm Tap 节奏 | Beatstar, Just Dance | 跟节拍点击 / 做动作 |
| 7 | Body-Pose 健身 | Ring Fit Adventure | 全身动作驱动 |
| 8 | Sports Mini（网球/保龄/拳击） | Wii Sports, Switch Sports | 挥臂模拟运动动作 |
| 9 | Microgame 合集 | WarioWare: Move It | 5-10s 体感小游戏串烧 |
| 10 | Obby / Parkour | Tower of Hell | 跳跃通关障碍跑酷 |
| 11 | Match-3 三消 | Block Blast!, 抓大鹅 | 找三件相同消除 |
| 12 | 找茬 Spot-Diff | 找茬大湿 | 两图差异比对 |
| 13 | Bullet Hell 短局 | Vampire Survivors | 自动开火只管走位 |
| 14 | Physics Puzzle | Cut the Rope 类 | 物理引擎解谜 |
| 15 | Idle / Tycoon | Welcome to Bloxburg | 放置经营 |
| 16 | 2-Player 同屏对战 | Stickman Party | 同设备双人迷你游戏 |
| 17 | Face-Filter 表情挑战 | Snappables, Face Dance | 跟随屏幕做表情 |

### A.4 motem 形态适配评分

三维度打分，每维 0-2 分，满分 6：
- **体感适配度**：身体倾斜 / 人脸位置 / 手势能否自然映射操作
- **30-90s 适配度**：是否天然短局，无需长流程
- **桌面距离适配度**：1-1.5m 距离下能否看清、能否互动

| 品类 | 体感 | 30s | 桌面距离 | **总分** | 备注 |
|---|---|---|---|---|---|
| Catch-and-Collect 接物 | 2 | 2 | 2 | **6** | 人脸位置 / 倾斜 = 左右移动，天然映射 |
| Dodger 躲避 | 2 | 2 | 2 | **6** | 同上，几何块大、远距可见 |
| Whack-a-Mole 拍打 | 2 | 2 | 2 | **6** | 触摸戳屏 + 握拳手势均可 |
| Fruit-Slicer 挥砍 | 2 | 2 | 2 | **6** | 挥手手势是最直觉的"切" |
| Microgame 合集 | 2 | 2 | 2 | **6** | WarioWare 模式天然 5-10s/局 |
| Sports Mini | 2 | 1 | 2 | **5** | 一局 1-2min 可控；网球 / 拳击挥臂适合 |
| Rhythm Tap 节奏 | 1 | 2 | 2 | **5** | 节拍可，但远距点击精度差 |
| Face-Filter 表情 | 2 | 2 | 1 | **5** | 适合，但表情细节远距可能看不清 |
| 2-Player 同屏 | 1 | 2 | 2 | **5** | 屏幕 6 寸两人挤紧 |
| Endless Runner | 1 | 2 | 2 | **5** | 倾斜切道 OK，UI 看累 |
| Body-Pose 健身 | 2 | 0 | 2 | **4** | 单局太长，与碎片冲突 |
| Bullet Hell | 1 | 1 | 1 | **3** | 弹幕过密、远距看不清 |
| Match-3 三消 | 0 | 2 | 1 | **3** | 纯触摸精细操作，体感用不上 |
| 找茬 Spot-Diff | 0 | 2 | 0 | **2** | 远距看不见小差异 |
| Physics Puzzle | 0 | 1 | 1 | **2** | 精细操作，触摸不便 |
| Obby / Parkour | 1 | 1 | 1 | **3** | UI 复杂、多按键 |
| Idle/Tycoon | 0 | 0 | 1 | **1** | 长流程菜单堆叠 |

### A.5 推荐 6 个 archetype（按优先级）

| # | Archetype | 评分 | 关键市场证据 | 控制方式 | 落地优先级 |
|---|---|---|---|---|---|
| 1 | **接物** Catch-and-Collect | 6/6 | Kinect Adventures 2400 万套（Kinect 史上最畅销） | 人脸位置 + 身体倾斜 | **Phase 1** |
| 2 | **躲避** Dodger | 6/6 | Geometry Dash / Color Switch / Subway Surfers 长青 | 人脸位置 + 身体倾斜 | **Phase 2** |
| 3 | **挥砍** Fruit-Slicer | 6/6 | Fruit Ninja 10 亿+ 下载，Vision Pro 选作手势示范 | 手势挥手 | **Phase 3** |
| 4 | **拍打** Whack-a-Mole | 6/6 | 50 年街机经典，Snappables AR 滤镜玩法 | 触摸 + 握拳手势 | **Phase 3** |
| 5 | **Sports Mini** | 5/6 | Wii Sports 8200 万套（单平台史上第一） | 手势挥臂 + 人脸瞄准 | Phase 3+ 数据决定 |
| 6 | **Microgame 合集** | 6/6 | WarioWare: Move It（2023 Switch） | 前 4 个 archetype 串烧 | Phase 4 以后 |

**首批 4 个（满分 6/6）** = 本方案 Phase 1-3 范围。

### A.6 明确否决的品类（说明为什么 NOT）

| 品类 | 否决理由 |
|---|---|
| **Match-3 三消（含抓大鹅）** | 抖音 #1 但完全依赖手指精细点击小图标。1m 外人脸 / 手势没法替代精细触摸 |
| **找茬 Spot-Difference** | 抖音爆款，但本质是"看清两图细节"，桌面 1m 距离物理上看不见差异 |
| **Bullet Hell（含 Vampire Survivors）** | Steam / 手机爆款，但弹幕过密 + 小敌人，远距识别失败 |
| **Obby / Parkour** | Roblox 头部，但需要多键操作（跳 / 冲 / 视角），体感无法稳定映射 |
| **Idle / Tycoon / SLG** | 中国头部品类但全是菜单流，单局数小时，与"30s 碎片"反着 |

### A.7 对原 8 品类清单的批改

| 原清单 | 处置 | 依据 |
|---|---|---|
| 射击 | **降级 → 改为挥砍** | 桌面 1m 瞄准精度差；挥砍（Fruit Ninja 10 亿下载）市场盘子更大、控制更直觉 |
| 接球 | **保留并提升为 Phase 1 首发** | Kinect Adventures 同款，体感映射最自然 |
| 跑酷（Endless Runner） | 保留但 Phase 3 之后 | App Store 长青，但要做大色块 UI |
| **弹球** | **砍** | 没有明确数据源，物理球需精细瞄准；并入 Sports Mini 的"保龄/网球"更稳 |
| 躲避 | **保留并提升为 Phase 2** | Dodger 类，几何块大、远距可见 |
| 节奏点按 | 保留但 Phase 3+ | Beatstar / Just Dance 验证有市场，但需要超大按钮 |
| 鬼脸（Face Filter） | 保留但 Phase 3+ | Snappables 验证可行，需明确"远距能识别的大表情" |
| 双人合作 | 保留但 Phase 3+ | Stickman Party 验证，但 6 寸屏挤紧，建议非对称合作（一人体感 + 一人触摸） |
| **新增：挥砍** | **加入 Phase 3** | 10 亿下载级品类，手势最直觉 |
| **新增：Microgame 合集** | Phase 4+（不在本方案） | WarioWare 模式做整合层免费拿粘性 |
| **新增：Sports Mini** | Phase 3+（不在本方案） | Wii Sports 8200 万套盘子最大 |

### A.8 与本方案的关联

调研结论直接映射到 Phase 范围：

- **Phase 1（接物）**：从 6/6 满分 + 体感映射最简单（人脸 X = 角色 X）的 archetype 起步
- **Phase 2（加躲避）**：与接物共用左右位移控制器（工程红利）
- **Phase 3（挥砍 + 拍打）**：首批 4 个 6/6 满分 archetype 全到位
- **Phase 4+ 暂不做**：Microgame 聚合层 / Sports Mini / 节奏 / 鬼脸 / 双人 / 跑酷，留待 Phase 3 真实用户数据决定优先级

**首批 4 个 archetype 已覆盖 6 大满分品类中的 4 个**，工程范围明确、市场证据充分、形态适配最强。

### A.9 参考资料

- [Sensor Tower: Top 10 Worldwide Mobile Games - Aug 2025](https://sensortower.com/blog/top-10-worldwide-mobile-games-by-revenue-and-downloads-in-august-2025)
- [AppMagic Casual Games Report H1 2025](https://appmagic.rocks/research/casual-report-h1-2025)
- [Year Ender 2025: Top 10 Most Downloaded Games](https://respawn.outlookindia.com/gaming/gaming-guides/top-10-most-downloaded-mobile-games-2025-roblox-takes-the-crown)
- [Top 100 Played Roblox Games (Oct 2025)](https://spinthewheel.app/top-100-played-roblox-games)
- [抓大鹅 — 风靡全网的成功秘密（腾讯新闻）](https://news.qq.com/rain/a/20240428A09VAH00)
- [抖音畅销榜女性向、轻度为主（36氪）](https://36kr.com/p/3231477172125061)
- [Wii Sports - Wikipedia（8200 万销量）](https://en.wikipedia.org/wiki/Wii_Sports)
- [WarioWare: Move It! - Wikipedia](https://en.wikipedia.org/wiki/WarioWare:_Move_It!)
- [Ring Fit Adventure 6 年仍是健身王者（GamesRadar）](https://www.gamesradar.com/platforms/nintendo-switch/6-years-after-its-release-ring-fit-adventure-remains-the-unrivalled-king-of-fitness-games/)
- [Best Kinect Games of All Time（Cultured Vultures）](https://culturedvultures.com/best-kinect-games/)
- [Kinect Sports & Dance Central sales（GameSpot）](https://www.gamespot.com/articles/kinect-sports-scores-3-million-sales-dance-central-sells-25-million/1100-6330113/)
- [Fruit Ninja - Wikipedia（10 亿下载）](https://en.wikipedia.org/wiki/Fruit_Ninja)
- [Super Fruit Ninja on Vision Pro（MacRumors 2024）](https://www.macrumors.com/2024/01/18/super-fruit-ninja-vision-pro/)
- [Best Mobile Rhythm Games 2026（Scrambly）](https://scrambly.io/blog/games/best-mobile-rhythm-games)
- [Best Two-Player Mobile Games（Pocket Gamer）](https://www.pocketgamer.com/best-games/2-player-mobile-games/)
- [Eyeware Beam head tracking（TheGamer）](https://www.thegamer.com/eyeware-beam-head-tracking-app/)
- [Pokemon GO 2024 active users（Business of Apps）](https://www.businessofapps.com/data/pokemon-go-statistics/)
