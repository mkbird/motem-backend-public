# Widget Agent 优化方案 v2 —— 从游戏 agent 迁移经验

> 调研日期：2026-05-18 / 修订：2026-05-19
> 背景：游戏生成 agent（`game_tools.py` 2378 行）经过一年迭代沉淀了一批 token / 成本 / 稳定性 / 安全相关的工程化优化；小组件 agent（`widget_tools.py` 941 行）虽然独立重写了一份 SDK 调用底座，但**绝大多数优化没有跟上**。本文档逐条列出可迁移项、迁移方式、改造点和优先级。
>
> v2 修订要点：
> - 核验 `config.py:88` —— widget 现用 `qwen/qwen3-coder-next`，**不是** Claude Sonnet。原先依赖 sonnet 假设的 `--effort medium` / 双 model 拆分两条优化失去前提，分别 drop / 降级。
> - 把 P0 拆成两批：**安全 hotfix**（路径校验 + ownership 校验，立刻上线）与 **token 优化**（独立 PR，按节奏推）。
> - 新增 prompt cache 章节（缺失维度）。
> - Fast path 从 P2 提到 P1（widget 已有 `template_widget_id` 框架，离 NL→template 路由器只差一步路由层）。
> - 新增「先量化再做」原则：JS syntax fix loop 上线前必须先采样实际 error 率。

---

## 一、总览

| 类别 | 项数 | 主要影响 |
|---|---|---|
| 安全 hotfix（立刻） | 2 | path traversal + ownership 越权 |
| Token / 成本（P0/P1） | 4 | 每 round-trip 省 6-7K input + 80K tools schema |
| Fast path（P1，提升） | 1 | 模板复刻类需求 SDK 调用降到 0 |
| 稳定性 / UX（P0/P1） | 3 | 修 widget creator 卡死 + cosmetic 假失败 |
| Session 管理（P1） | 2 | remix 会话连续性 + 防 jsonl 爆炸 |
| 已对齐（无需迁移） | 3 | retry / 双超时 / generation_failed 三层 |
| 不适合搬 / 反向规避 | 4 | 游戏专属 / 历史包袱 |

**核心结论：** Widget agent 缺 2 个 P0 级安全洞、4 项 token 优化、3 项稳定性兜底。安全洞先 hotfix，其他按节奏推。

修订后预期收益（v2，去掉了 sonnet-only 假设带来的虚高）：

- create input tokens **减 50-60%**（主要来自 `--bare` + `--tools`）
- modify 成本变化**未知**（需先量化当前模型 cache hit + 看是否切更便宜模型）
- 修复 2 类 P0 失败模式（path traversal / 越权）+ 1 类 UX 卡死

---

## 二、零优先级：安全 hotfix（独立 commit，今天上）

| # | 风险 | 现状（widget_tools.py） | 修复 |
|---|---|---|---|
| **S1** | Path traversal —— `modify_widget(widget_id, ...)` 是 WS 裸接口，能传任意路径，Claude Code SDK 以 backend 进程身份在主机任意目录读写 | `:791-794` 只检查 `widget_dir.is_dir()`；`widget_id` 没正则校验；没 `resolve().is_relative_to(WIDGETS_DIR)` 二次确认 | 加 `_is_safe_widget_id` 函数（参考 `game_tools.py:2056-2072` 的 `_is_safe_ai_game_id`）：正则 `^[a-zA-Z0-9_-]{1,64}$` + resolve 后 `is_relative_to` |
| **S2** | Ownership 越权 —— bob 能改 alice 的 `public_remixable` widget（前端隐藏按钮，但 WS endpoint 是裸的） | `:774-810` modify_widget 完全没 ownership 校验 | 加 DB-first 两层兜底（参考 `game_tools.py:2103-2153`）：先查 `ai_widgets` 表 owner_id，DB 故障才退到 meta.json |

**做法：** 单独一个 commit，`fix(widgets): close path-traversal + ownership-bypass in modify_widget`，**不和其他优化混在一起**，方便事后审计。

---

## 三、Token / 成本优化

### 3.1 已确认收益（P0，立刻做）

| # | 优化 | 现状 | 改动 | 预期收益 | 风险 |
|---|---|---|---|---|---|
| **T1** | `--bare` 关 CLAUDE.md auto-discovery + hooks/LSP/plugin sync | `widget_tools.py:357-369` 完全没传 `extra_args`，SDK 默认扫 `~/.claude/CLAUDE.md` + `robot/CLAUDE.md` + `motem-backend/CLAUDE.md` 注入 messages[0] | 一行加进 `_make_options` 的 `extra_args` | 每 round-trip 省 6-7K input tokens（按 game 侧实测推算） | 极低，game 侧已稳定跑半年 |
| **T2** | `--tools` 白名单 | `widget_tools.py:362` 只设 `allowed_tools`（permission 层），没设 `--tools`（schema 仍发 29 个 tool） | 一行加进 `extra_args["tools"]`，列出 widget 实际需要的 5-6 个 | tools schema ~92K → ~12K bytes，省 80% tools token | 低，但需要核对 widget 实际用到哪些工具，别砍掉刚需 |

两条加起来一次 PR，预计 widget create input tokens 立刻 -50%。

### 3.2 需要先量化才能定（P1，调研）

| # | 优化 | 现状 | 调研目标 | 决策点 |
|---|---|---|---|---|
| **T3** | 双 model 拆分 modify 走更便宜的 | `widget_code_model = qwen/qwen3-coder-next`，create + modify 共用 | 抓 OpenRouter response 的 `usage.cost` 看 Qwen3 modify 单次实际成本；对比 haiku 4.5 / glm-4.5-air 等替代 | Qwen3 modify 成本 > $0.01/次 → 加 `widget_modify_model` 配置切便宜模型；< $0.01 → 不做 |
| ~~T4~~ | ~~`--effort medium`~~ | ~~Sonnet-only feature~~ | **drop** | Qwen3 没有 extended thinking，传也无效 |

### 3.3 缺失维度（P1，方法层面）

| # | 优化 | 说明 |
|---|---|---|
| **T5** | **Prompt cache 监控** | OpenRouter response 里有 `usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens`。**先看 Qwen3 endpoint 是否支持 prefix-cache**（不是所有 OpenRouter model 都支持）。如果支持，把 system prompt 重排成「完全静态前缀 + 动态后缀」让 cache hit rate >70%。如果不支持，跳过整个 cache 优化。**做之前要先确认能不能做。** |

---

## 四、Fast path（P1，提到方案前沿）

| # | 优化 | 现状 | 改动 | 收益 |
|---|---|---|---|---|
| **F1** | NL → template 路由器（widget 版 fast path） | `widget_tools.py` 已有 `template_widget_id` 参数支持「基于现有 widget 做新的」，但**没有自动路由层** ——用户必须从内置 widget 库里选一个再让 AI 改 | 加 `widget_archetype_router`：用便宜 model（haiku / qwen-coder-30b）做 1 次分类，命中已知模板 → 直接 clone + 字段微调（绕过 SDK），未命中 → 走当前 SDK 路径 | 命中时 SDK 调用降到 0；常见请求如"做个 xx 计时器 / xx 倒计时 / xx 比分牌"几乎都能命中 |

**前提：** 先盘点现有 widget 库里"高复用度模板"是哪些。如果 marketplace 里高频 remix 的 widget 集中在 3-5 个，就值得做。如果用户都做完全独特的 widget，fast path 没意义。

**做法：** 不要先盖路由器架子。先在 SQL 里查最近 30 天 `widget_likes` + `view_count` top-10 widgets，看头部集中度。**集中度 < 50% → 跳过 F1**。

---

## 五、稳定性 / UX

| # | 优化 | 现状 | 改动 | 优先级 |
|---|---|---|---|---|
| **U1** | Modify `no-diffs` 失败通知 | `widget_tools.py:893-903` 仅在 `file_count==0` 时发 `widget_creation_failed`，**file_count>0 但 diffs=0**（AI 写了文件但内容没变）没通知 → WidgetCreatorView 永久卡 `isSending=true` | 把 `:893` 分支改成**无条件**发 `widget_creation_failed(reason="no_changes")` | **P0**，已知 bug |
| **U2** | Cosmetic exit-1 兜底 | `widget_tools.py:475-481` 任何 exception 直接当失败 | 加 game 侧 `:1070-1088` 同款判定：`"exit code 1" in str(e) and files>0 and elapsed>5s` → 当成功 | P0，修偶发"已写完文件被吃成 generation_failed"的假失败 |
| **U3** | JS syntax fix loop | 无；语法错的 widget 直接发给 iOS，WebView `Uncaught SyntaxError` 空白 | **先采样** —— 跑 30 次 widget create 统计实际 syntax error 率。> 10% → 做 `node --check` + haiku 修一轮；< 5% → 不做（ROI 不够） | P1（采样）→ 视结果定 P2 是否真做 |

---

## 六、Session / 长上下文管理

| # | 优化 | 现状 | 改动 | 优先级 |
|---|---|---|---|---|
| **C1** | per-widget session 隔离（`CLAUDE_CONFIG_DIR`） | widget session 走 `~/.claude/projects/<global-slug>/`，shutil.copytree（remix）丢 session、删 widget 漏 jsonl | 把 `robot/games/_session_paths.py` 抽象成通用 `_artifact_session_paths.py`，widgets/ 加薄壳 | P1，remix / 删除时数据卫生 |
| **C2** | Sliding-window compaction + cliff fallback | 无 | game 侧阈值 50KB/25KB；widget 单文件 diff 增长慢，**先观察实际 jsonl 大小再决定阈值**，触发频率低就先不上 | P2，按需 |

---

## 七、已对齐 / 无需迁移

- Resume-failure retry（widget 已有 `:475-486` 两条 retry，对齐 game `:1053-1106`）
- Inactivity + wall-clock 双超时（widget `:386-414` vs game `:932-961`，都 300s/600s）
- `generation_failed` 三层防线（widget `:650-684` 三档分类反而比 game 更精确，可反向移植回 game）

---

## 八、反向迁移 / 规避（不要从 game 搬这些）

1. **`_validate_and_fix` 两套 fix loop**（gdparse + godot）—— `game_tools.py:1136` 是历史包袱（注释自承双层 fix 反而是问题），widget 一开始就单层
2. **`slowpath/CLAUDE.md` 动态拼 audio/assets section** —— widget 没 audio manifest 概念
3. **`_harvest_audio` / `_import_assets` / `_generate_script_cache`** —— 全是 Godot 专属
4. **`game_dir/.claude/` 进 zip** —— 隐私 + 体积问题，widget 走分发路径要额外 zip exclude

反向收获：widget 的 `template_unchanged` 字节对比（`:650-684`）比 game 的 `incomplete_reason` 更精确，**可以反向移植回 game**（P2）。

---

## 九、执行顺序

| 阶段 | 内容 | 改动 | 工时 | 触发条件 |
|---|---|---|---|---|
| **HOTFIX** | S1 path traversal + S2 ownership 校验 | `widget_tools.py` ~40 行 + 单测 | 半天 | 立刻 |
| **OPT-1** | T1 `--bare` + T2 `--tools` + U1 no-diffs + U2 cosmetic exit-1 | `widget_tools.py` ~50 行 + 测试 | 1 天 | HOTFIX 后 |
| **MEASURE** | T3 Qwen3 实际成本 + T5 cache 支持调研 + U3 syntax error 采样 + F1 模板集中度查询 | 抓包脚本 + SQL 查询 + 30 次手动 create | 1 天 | OPT-1 上线后 1 周内 |
| **OPT-2** | C1 per-widget session 隔离 | 抽象 `_artifact_session_paths.py` | 1 天 | MEASURE 不阻塞 |
| **OPT-3**（条件） | T3 切便宜模型 / U3 syntax fix loop / F1 fast path | 各 1-3 天 | 总 3-7 天 | 只做 MEASURE 命中阈值的项 |

---

## 十、文件引用速查

### 游戏侧（参考来源）

- `robot/agent/tools/game_tools.py:725, 783-790, 796` — `--bare` / `--tools` / `--effort` 注入点
- `robot/agent/tools/game_tools.py:758-825` — per-game CLAUDE_CONFIG_DIR + session 迁移
- `robot/agent/tools/game_tools.py:1070-1088` — cosmetic exit-1 兜底
- `robot/agent/tools/game_tools.py:2056-2072` — `_is_safe_ai_game_id`
- `robot/agent/tools/game_tools.py:2103-2153` — ownership DB-first 校验
- `robot/agent/tools/game_tools.py:1815-1876` — generation_failed 三层防线

### 小组件侧（迁移目标）

- `robot/agent/tools/widget_tools.py:311-490` — `_run_widget_code_agent` 主体（缺 `extra_args` / 缺 cosmetic exit-1）
- `robot/agent/tools/widget_tools.py:357-369` — `_make_options` 缺 `extra_args` 证据
- `robot/agent/tools/widget_tools.py:474-486` — modify retry 路径（已对齐 game）
- `robot/agent/tools/widget_tools.py:650-684` — generation_failed 三档（反而比 game 更精确）
- `robot/agent/tools/widget_tools.py:774-810` — `modify_widget` 缺 path safety + ownership 校验
- `robot/agent/tools/widget_tools.py:893-903` — no-diffs 失败通知逻辑不完整

### 配置

- `robot/config.py:73` — `game_code_model = "z-ai/glm-5.1"`
- `robot/config.py:80` — `game_modify_model = "anthropic/claude-haiku-4.5"`
- `robot/config.py:88` — `widget_code_model = "qwen/qwen3-coder-next"`（**create + modify 共用**）

### 架构对比

- `robot/games/fastpath/` — archetype fast path 整套（widget 当前无对应物，但 `template_widget_id` 框架已存在）
- `robot/agent/streaming.py:29-166` — 双侧 fast path 入口路由对称
