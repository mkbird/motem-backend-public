# Fast Path v2 实施方案（A+C 路线：桌面游戏机）

> 起草于 2026-05-13。决策者：用户。前置文档：`game-fastpath-implementation-plan.md`（v1 路线）。

## 战略定位

**motem 是桌面游戏机，不是桌面陪伴玩具。**

这一句决定了 v2 所有改动方向：

- 用户来 motem **是为了挑战自己 / 刷分 / 探索组合 / 录炫技视频**，不是为了"看那只狗在不在"
- 玩家开机 30 秒后的内心独白应该是「我能不能破纪录 / 这次的 director 组合怎么打」，**不是**「我家小白今天好不好」
- 失败必须有意义（教学反馈），不是被 nerf 掉的"永远不死局"

## 路径对比（决策依据）

| 路径 | 定位 | 招牌 | 留存来源 | 决策 |
|---|---|---|---|---|
| **A**：桌面 Tetris | 体感游戏框架 | 单一 archetype 100 小时玩法 | mastery 投入 | 单做太单调 |
| **B**：桌面 Tamagotchi | 桌面陪伴玩具 | mascot 永久化 + 日历 | 情感依附 | **已放弃** |
| **C**：桌面 Slay the Spire | 涌现玩法引擎 | chemistry + director 组合 | 探索发现 | 单做无 mastery 钩子 |
| **A+C**（**采用**）| 桌面游戏机 | 每局组合不同 × 反复优化精通 | mastery + 探索 + 短视频炫技 | ✓ |

## A+C 一句话

**4 个 v1 archetype 全部升级 v2，每个都获得：mastery 深度（隐藏 tech / heat map / risk-reward）+ procedural 变化（chemistry 表 / director 卡组合 / 物品多维属性）。LLM 真正变成 game director，根据用户 prompt 选 director + tag 组合，生成机制层不同的游戏（不是只换 emoji）。**

## 已放弃的方向（B 路径相关，永久剔除）

| 放弃项 | 原因 |
|---|---|
| Mascot 永久化（`users.mascot_state` JSONB） | 游戏机不需要持久化角色 |
| Idle Ambient Layer（不玩时狗在屏幕活着） | 游戏机不玩 = 黑屏正常 |
| Calendar Pulse / 日历感（早晚 / 周末 / 节日） | 游戏机不需要"知道今天几号" |
| `grow_v1` 养成 archetype | 跟"游戏机"定位冲突 |
| 删 lives / "永远不死局" | 游戏机必须 fail-state 给 mastery 反馈 |
| Persona 字段（player_role / companion / stakes / death_line） | 30 秒短视频游戏不需要叙事层 |
| iOS 推送"小白想你了" | 游戏机不需要情感钩子 |

## 六位专家共识总结（保留的设计点）

来源：两轮各 3 位专家（街机老炮 / hyper-casual PM / 体感设计师 / skill ceiling / 玩具设计师 / systems & emergence）。仅保留与 A+C 兼容的部分，B 路径相关点已剔除。

### 第一轮共识（首次体验）

| 机制 | 落地位置 |
|---|---|
| Hitstop / 慢放 / 屏幕震 helper | `_template_fastpath/scripts/juice.gd` |
| 粒子 helper（爆汁 / 浮字 / COMBO BLAST 大字） | `_template_fastpath/scripts/particles.gd` |
| Result card 组件（数字滚动 / Best diff 红字 / 大橙色按钮） | `_template_fastpath/scripts/result_card.gd` |
| "续 1 命" 3-5s 挑战框架 | result_card 组件内嵌 |

### 第二轮 — Skill ceiling（mastery 深度）

> 现状残酷数据：catch 1.3× / dodger 1.8× / whack 2× / slicer 3× —— 全部低于 mastery 死亡线（3×）。

**通用病灶**：没有 risk/reward、没有微观决策、没有隐藏机制、没有 route、死亡是终结不是教学。

**通用解药**（每个 archetype 都要加）：
1. **Risk/reward 选择**（高难度路线得分翻倍 vs 安全路线稳得分）
2. **隐藏 tech**（让玩家 10 局后发现"卧槽这能这样玩"）
3. **Heat map / 看自己变强**（结算屏 timeline / 反应时间 / 轨迹叠加）
4. **失败的教育价值**（慢镜头 + 鬼影显示玩家 0.3 秒前应在哪）

### 第二轮 — Systems / Emergence（procedural 变化）

**核心想法**：每个 archetype 现在是 0.7-1 个系统，必须加第二个系统让它涌现。

**通用基础设施**（4 archetype 共用）：
1. **Chemistry 5×5 表**（🔥+🌿=蔓延 / 💧+🔥=灭 / 🌿+💣=缠住延迟 等 11 条规则）
2. **物品多维属性 schema**（tag / weight / fragility / value / edible / magnetic / combo_key 等）
3. **Director enum 字段**（10 个 modifier：tense / relaxing / chaotic / gravity_flip / slowmo / magnet / dark / wind / boss_round / puzzle）
4. **LLM 当 director**：customizer prompt 学会根据用户描述选 director + tag 组合

## Phase 划分

总工作量约 **13-15 天**（不含真机测试 + LLM smoke）。

### Phase 1：公共基础设施（所有 v2 依赖，最先做）

```
P1.1  _template_fastpath/scripts/juice.gd          0.5 天
      hitstop / screen_shake / time_dilation helper（4 archetype 共用）

P1.2  _template_fastpath/scripts/particles.gd      0.5 天
      爆汁 / 浮字 / COMBO BLAST 大字 helper

P1.3  _template_fastpath/scripts/result_card.gd    1.0 天
      Score 滚动 / Best diff 红字 / 80% 宽橙色 "再来一次"
      / 续 1 命 3s 挑战框架

P1.4  _template_fastpath/scripts/chemistry.gd      1.5 天
      5×5 物品互动矩阵 + 11 条规则函数
      （所有 archetype 碰撞回调里调 Chemistry.react(a, b)）

P1.5  Director 模板分支（_template/scenes/main.gd） 0.5 天
      10 个 modifier flag 的 GDScript 占位符 + if 分支
      （__DIRECTOR_TENSE_ON__ / __GRAVITY_FLIP_ON__ 等）
```

### Phase 2：Schema + LLM director

```
P2.1  schemas.py 扩字段                            0.5 天
      - directors: list[Director] (enum, maxItems=3)
      - 每个 EntitySpec 加 tag: str (enum: fire/water/grass/metal/explosive/neutral)
      - 每个 archetype recipe 加 hidden_tech_enabled: bool

P2.2  customizer prompt 改造                       0.5 天
      LLM 学会根据用户描述选 director + tag
      "紧张刺激" → ["tense", "chaotic"]
      "轻松解压" → ["relaxing", "slowmo"]
      "狗接包子" → entity tag {drop: edible, obstacle: explosive}

P2.3  smoke 测试                                   0.5 天
      15 个 prompt 看 LLM 实际 director / tag 命中率
      （扩 scripts/fastpath_smoke.py）
```

### Phase 3：slicer_v2（招牌 archetype，优先做）

slicer 是 4 个里最有 mastery 潜力的（2D 连续输入 + 速度 + 方向 + 时长），也是最有短视频卖点的（挥手画 8 字 ×40 combo 上镜）。

```
P3.1  几何识别器                                   1.5 天
      Trail 末段 Douglas-Peucker 简化 → corner / closure 检测
      4 个模板：
        - 一刀连切 ≥3 fruit = triple ×2
        - 闭合环包住 ≥2 fruit = loop ×3，包圈内同时切
        - 8 字形 = infinity ×5 + 0.5s 慢动作
        - Z 字横扫整屏 = zorro ×8

P3.2  bomb graze + Charge Slash                    0.5 天
      bomb 双层判定（52 / 85），擦弹 +5 + "close call"
      Trail 静止 ≥0.5s → "charged" → 下一刀整屏闪光线

P3.3  item 类型分化                                0.5 天
      樱桃 pair（共线必须一刀双切）/ 星星轮廓（预判空中位置）
      / 钻石（必须包圈才能 collect, ×10）

P3.4  Heat map                                     0.5 天
      结算屏：path trace heatmap（trail 颜色按 slice density）
      / geometry bonus 触发次数 / charge slash 释放次数 / 最长 combo
```

### Phase 4：dodger_v2（v1 最薄弱补强）

```
P4.1  Graze 双层判定                               0.5 天
      碰撞圈 22 + 擦弹圈 55，graze +5 + ⚡ 金色粒子
      "贴着死亡线走"从作死变成最高分技

P4.2  Dash energy 主动 ability                     0.5 天
      dash_energy ∈ [0, 1]，graze +0.2，每秒衰减 -0.05
      双击屏幕或快速倾斜身体 → 0.4s 无敌帧 + 瞬间位移
      4-5 次/局，强制"何时用大招"决策

P4.3  3 种 hazard + wave pattern                   1.0 天
      🪨 Slow Rock v=200 / ☄️ Fast Comet v=550 / 🔥 Tracking Flame（aim 玩家）
      Spawn 改 deterministic seed + wave pattern
      每 5 秒一个 wave（V 字 / 左密集 / 全屏稀疏 + aim）
      Speedrunner 能记 pattern 路由

P4.4  Heat map                                     0.5 天
      Death heat map：grazing 密度 / shield 使用时刻 / 死亡 hazard 类型
```

### Phase 5：catch_v2

```
P5.1  3 车道 Risk Lane                             0.5 天
      Left ×1 (5% hazard) / Center ×2 (25%) / Right ×5 (60%)
      drop 显式带车道角标（×N label）

P5.2  Combo Path + Charge Catch                    0.5 天
      Combo +1 = 连续在不同车道接 drop（强制跨车道横扫）
      Charge Catch：静止 200ms → 蓝晕 → 下一次 ×3 + 不算 combo

P5.3  配方累积（chemistry 复用）                    0.5 天
      屏幕角落 3 槽 emoji combo（🍌🍌🍒 → 香蕉冰沙 → 接物筐变宽 1.8 倍 6s）
      LLM 在 recipe 里指定 1-3 个配方
```

### Phase 6：whack_v2

```
P6.1  Lifetime × Score 反比                        0.3 天
      score_value × (1 + life_remaining / TARGET_LIFETIME × 3)
      早点 ×4 / 晚点 ×1，反应速度 = 4× 倍率

P6.2  Disguise Targets                             0.5 天
      10% target 先以 target 外观出现 0.4s，然后变 decoy
      奖励忍住手不点的玩家（格斗游戏"看招见解"）

P6.3  Multi-Touch Chord（隐藏 tech）                0.3 天
      2 指 200ms 内同时点 = ×2 / 3 指 = ×4

P6.4  节拍化 spawn                                  0.5 天
      Spawn 严格落在 BGM 1/8 拍上
      老手能"听 BGM 预读 spawn 时机"
```

### Phase 7（可选 / 缓）：Replay 系统

只在 Phase 1-6 完成且实测真机表现良好后再做：

- 存储用户操作 timeline（每帧 input + game state snapshot）
- 结算屏支持回看 / 分享
- 工作量大（要 ~5 天），且不是 v2 体验的必需品

## 横向原则（所有 archetype 共用）

1. **chemistry 必须经过 `chemistry.gd` 间接** —— 4 个 archetype 各自硬编码 chemistry 等于灾难，3 个月后没人敢动
2. **director / chemistry 都走 GDScript 模板的占位符开关** —— LLM 决策面只暴露 enum，绝不让 LLM 写表达式
3. **新增维度永远 = 加 1 个 enum 值 + 加 1 段 GDScript 分支**，不引入运行时插件加载
4. **fast path strategy 的命名规范延续**：`<verb>_v2.py`，老的 `_v1.py` 不删 / 不改，作为对比基线保留
5. **iOS 不动**：A+C 路线下不需要 iOS UI 改造，现有 GodotGameView + face/hand 管线足够。**不用拉 iOS worktree**
6. **持久化只在后端**：高分纪录 / heat map 历史走后端 DB，不放 mascot_state 那种 JSONB

## 关键技术细节

### Chemistry 表（落 `chemistry.gd`）

5×5 物品互动矩阵：

|        | 🔥 火 | 💧 水 | 🌿 草 | 🪙 金属 | 💣 炸弹 |
|--------|------|------|------|--------|--------|
| 🔥 火  | bigger_fire | extinguish | spread_fire | heat_metal | chain_explode |
| 💧 水  | extinguish | merge_pool | grow_plant | rust | wet_dud |
| 🌿 草  | spread_fire | grow_plant | no_op | no_op | wrap_bomb |
| 🪙 金属 | heat_metal | rust | no_op | clink_combo | shrapnel |
| 💣 炸弹 | chain_explode | wet_dud | wrap_bomb | shrapnel | mega_boom |

11 条规则：spread_fire / extinguish / grow_plant / heat_metal / chain_explode / wet_dud / wrap_bomb / clink_combo / rust / mega_boom / no_op。

### Director enum（schemas.py）

```python
class Director(str, Enum):
    TENSE = "tense"               # spawn × 1.4, speed × 1.2, lives - 1
    RELAXING = "relaxing"         # spawn × 0.7, speed × 0.7, lives + 1
    CHAOTIC = "chaotic"           # 开启 chemistry, density × 1.3
    PUZZLE = "puzzle"             # 慢动作 + chemistry，每个物体都有属性
    GRAVITY_FLIP = "gravity_flip"
    SLOWMO = "slowmo"
    MAGNET = "magnet"
    DARK = "dark"                 # 屏幕周期性黑屏闪烁
    WIND = "wind"                 # 物体水平 vx 偏移
    BOSS_ROUND = "boss_round"     # 每 20s 一次 boss-style 事件
```

每个 director 在 `_template/scenes/main.gd` 有对应 if 分支（编译时根据 recipe.directors 设占位符）。

### 物品属性 schema（schemas.py）

```python
class EntitySpec(BaseModel):
    emoji: str = ""
    sprite_hint: str = "default"
    tag: ItemTag = ItemTag.NEUTRAL           # 新增：chemistry 索引键
    weight: float = 1.0                       # 新增：影响下落速度
    fragility: float = 0.0                    # 新增：碎裂动画触发
    magnetic: bool = False                    # 新增
    ignitable: bool = False                   # 新增
    score_value: int = 1                      # 已有
    lives_cost: int = 0                       # 已有
    combo_key: str = ""                       # 新增：配方累积键
```

## 决策点 / 待办

### 当前已经决定

- [x] 路径 = A+C
- [x] B 路径所有元素全部剔除
- [x] iOS 不动
- [x] Phase 顺序：基础设施 → schema → slicer → dodger → catch → whack

### 待决策

- [ ] Replay 系统（Phase 7）是否做？建议先看 Phase 1-6 真机表现再定
- [ ] heat map / 高分纪录的数据存哪？建议复用 `ai_games` 表加 `play_history JSONB`，避免新表
- [ ] customizer prompt 改造时是否保留旧 prompt 兼容老 recipe？建议保留（旧 recipe 默认 `directors=[]` 跟现行 v1 行为等价）
- [ ] LLM 模型选择 — 现在 customizer 走 qwen-plus，director 选择需不需要换 Sonnet？建议先用 qwen 试，smoke 不行再升级

## 测试策略

每个 Phase 完成都跑：

1. **单元测试**：strategy / chemistry / director 各加专属测试文件
2. **LLM smoke**：15 个 prompt（紧张 / 解压 / 紧张+chemistry / 异国主题等）实测 LLM 选择命中率
3. **真机端到端**：iPhone 16 Pro Max（device id `00008140-001670683E6A801C`）跑通"创建 → 下载 → 玩 30 秒"全流程

## 不在本方案范围

- ❌ Modify 路径切换 recipe-patch（延续现有 SDK + session resume）
- ❌ Microgame 聚合层 / 付费墙等产品形态改动
- ❌ iOS UI 改造
- ❌ Mascot / Persona / 陪伴功能（已放弃）
- ❌ LoRA / 微调（搁置 ≥3 个月）
- ❌ BYOK Anthropic（OpenRouter 非 BYOK 拿不到 cache，搁置）

## 历史包袱处理

| 老资产 | 处理方式 |
|---|---|
| 4 个 `<archetype>_v1.py` strategy | **保留不删**，作为 v2 对比基线 |
| `STRATEGIES` dict | v2 实现完成后 v1 / v2 同时注册，router 自动多分类 |
| 已生成的 ai_* 游戏 zip | 不迁移 / 不 patch，老用户继续玩 v1 生成的游戏；v2 只影响新生成游戏 |
| `_template_v1.bak` | 已删（cc18624 commit） |
| `_templates/` curated registry | 已删（cc18624 commit），暂不重建 |

## 起步建议

按顺序：

1. **Phase 1 全部完成**（公共基础设施，4 天）→ 跑通最小 smoke 验证 juice / chemistry / director 可调用
2. **Phase 2 完成**（schema + LLM director，1.5 天）→ smoke 看 LLM 命中率，命中 80%+ 再进 Phase 3
3. **Phase 3 完成 slicer_v2**（3 天）→ 真机录视频，看 8 字 ×40 combo 体感如何
4. **slicer_v2 demo 通过用户验证**后再继续 Phase 4-6

Phase 1-3 共 8.5 天，是验证 A+C 路径可行性的最小闭环。如果 slicer_v2 demo 用户反馈"还是不够好玩"，再决定是否继续 Phase 4-6 还是回炉重设计。
