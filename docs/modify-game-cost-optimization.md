# 修改游戏成本优化方案

## 背景

modify_game 加入 SDK session resume 后，速度从 400s 降到 28s（Edit 引导 + 去掉 MCP + resume），但费用从 $0.28 升到 $0.49。

根因：resume 重放 create 阶段的完整 session（~56K tokens），而 Anthropic prompt cache TTL 约 5 分钟，create 到首次 modify 间隔通常超过 5 分钟，导致 cache miss，56K tokens 按全价计费（~$0.21/次）。

## 费用拆解（实测数据）

| API 调用 | prompt tokens | cached | 费用 | 说明 |
|---------|-------------|--------|------|------|
| 1 | 56,909 | 0 | $0.215 | resume 重放历史，cache miss |
| 2 | 58,207 | 0 | $0.220 | thinking + Read，cache miss |
| 3 | 59,275 | 58,206 | $0.036 | Grep + Edit，cache hit |
| 4 | 60,287 | 59,274 | $0.023 | 最终回复，cache hit |
| **合计** | | | **$0.49** | |

前两次 cache miss 占 $0.43（88%），后两次 cache hit 只花 $0.06。

## 速度收益来源分析

28s 的速度提升拆解：

| 优化项 | 节省时间 | 是否依赖 resume |
|-------|---------|---------------|
| 去掉 MCP（modify 不启动 godot-mcp） | ~110s | 否 |
| Edit 引导（局部修改替代全量重写） | ~250s | 不一定 |
| resume（模型已有上下文） | ~10-20s | 是 |

**结论：速度提升主要来自去掉 MCP 和 Edit 引导，resume 贡献有限。**

## 方案对比

### 方案 A：不 resume create session，只 resume modify 之间

- 第 1 次 modify：不 resume，把 meta.json history 拼入 prompt，agent Read + Edit
- 第 2 次 modify：resume 第 1 次 modify 的 session（~15K tokens，且间隔短容易命中 cache）
- 后续 modify：持续 resume，session 小且 cache 命中率高

**预估费用**：
- 第 1 次 modify：~10K input → ~$0.03
- 第 2 次+：~15K input（多数 cached）→ ~$0.04

**优点**：简单，不增加 create 耗时，首次 modify 成本低
**缺点**：第 1 次 modify 需要 Read 理解代码（多 ~5-10s）

### 方案 B：create 完成后创建轻量 "摘要 session"

create 完成后，立刻用同一个 cwd 发起一个微型对话：
```
prompt: "Read scenes/main.gd，用一句话概括游戏机制。"
max_turns: 2
```
产出 ~15K tokens 的 session（system prompt + 代码内容 + 简短回复），存 session_id 到 meta.json。

**预估费用**：
- 摘要 session：~$0.005（额外开销）
- modify resume 摘要 session：~$0.045（vs 现在 $0.21）

**优点**：modify 有代码上下文，不需要额外 Read
**缺点**：create 多花 ~10s，增加了复杂度

### 方案 C：裁剪 session JSONL

create 完成后，程序直接修改 `~/.claude/projects/<cwd>/<session-id>.jsonl`，删除：
- Write 工具调用中的完整代码（24K chars）
- 长 thinking 块
- 只保留对话骨架（system prompt + Read 结果摘要 + 简短回复）

56K tokens → ~15K tokens。

**优点**：不多花时间，不多发请求
**缺点**：依赖 SDK 内部文件格式，SDK 升级可能 break，维护风险高

### 方案 D：废弃 resume，纯靠 prompt context

完全不 resume。利用现有 meta.json history + Edit 引导：
- agent 自己 Read 代码（~0.5s）
- history 提供修改脉络
- Edit 引导确保局部修改

**预估费用**：每次 ~10K input → ~$0.03
**预估速度**：~40-60s（比 resume 的 28s 慢，但比旧版 400s 快 10 倍）

**优点**：最简单，零额外复杂度，费用最低
**缺点**：每次都要 Read 代码，速度略慢

## 推荐

**优先试方案 D**（废弃 resume），理由：
1. 速度收益主要来自去掉 MCP 和 Edit 引导，不依赖 resume
2. 费用最低（$0.03/次 vs $0.49/次）
3. 最简单，无额外复杂度
4. meta.json history 已实现，无需改动

如果方案 D 的速度不够理想（>60s），再升级到方案 A（modify 之间 resume）。

## 验证步骤

1. 去掉 resume（revert session_id 传入）
2. 把 history 重新拼入 modify prompt
3. 测试 modify 速度和费用
4. 对比：

| 指标 | 旧版（无优化） | 当前（resume） | 方案 D |
|------|-------------|-------------|--------|
| 速度 | 400s | 28s | ? |
| 费用 | $0.28 | $0.49 | ~$0.03 |

---

## 2026-05 续：Claude Code SDK 黑盒注入审计

resume 方案保留，但通过反代抓包发现 SDK 子进程发给 OpenRouter 的真实
request body 中有大量我们**没主动注入**的 schema/上下文。详细抓包步骤
见仓根 `CLAUDE.md` 的"调试 AI 游戏生成的 prompt token 消耗"。

### 黑盒发现

| 黑盒来源 | tokens / round-trip | 处理 |
|---|---|---|
| 29 个内置 tool 全 schema 注入（Bash / TodoWrite / Agent / Cron / Worktree / Team / WebFetch 等） | ~23K | `extra_args={"tools": "Write,Edit,Read,Glob,Grep"}` 限制声明集合 |
| 父目录递归扫到的 4 份 CLAUDE.md auto-discovery | ~7K | `extra_args={"bare": None}` 关掉 + 手动 append 当前游戏目录的 `CLAUDE.md` 到 system_prompt |
| `## Godot MCP Tools` system 段（modify 不挂 MCP） | ~137 | `_build_system_prompt(use_mcp=False)` 条件去掉 |

### 实测对比（同游戏，同 instruction 复杂度，haiku 4.5）

| 阶段 | request body | 总 input / round-trip | SDK 估算 | OpenRouter 真实 |
|---|---|---|---|---|
| 优化前（resume） | 196 KB | ~41 K tok | $0.49 | ~$0.10 |
| 加 `--tools` | 86 KB | ~17 K tok | $0.29 | ~$0.06 |
| 加 `--bare` | 44 KB | ~9.5 K tok | $0.22 | ~$0.045 |
| 加 `use_mcp=False` MCP 段裁剪 | 41 KB | ~9.4 K tok | $0.21 | ~$0.043 |

### ⚠️ 成本数字口径

DONE 日志里的 `cost=$x.xxxx` 来自 `ResultMessage.total_cost_usd`，CLI
内置 model→price 表。CLI 2.1.138 对 `anthropic/claude-haiku-4.5` 没有
专门 entry，会 fallback 到 sonnet 量级（$3/$15 vs haiku 实际 $1/$5），
整体放大 ~5×（实测一次 modify：SDK 报 $0.2251，OpenRouter `usage.cost`
累加只有 $0.0450）。

**取真实数字**：跑 `scripts/openrouter_proxy.py` 反代，看 `/tmp/robot-claude-debug/resp_*.json`
里每次 round-trip 的 `usage.cost`（OpenRouter 真实扣费）累加。

**优化的相对降幅 ~57% 在两套口径下都成立**（同一估算偏差贯穿前后对比），
所以决策依据不变；只是绝对值要除 5。

### 设计要点（沉淀到代码）

`robot/agent/tools/game_tools.py::_run_claude_agent`：

```python
opts = ClaudeCodeOptions(
    system_prompt=system_prompt,           # 含 _build_system_prompt(use_mcp=...) + 游戏 CLAUDE.md（如存在）
    cwd=cwd,
    model=effective_model,
    allowed_tools=[...],                   # permission 层
    mcp_servers=mcp_servers if use_mcp else None,
    permission_mode="acceptEdits",
    env={"CLAUDE_CONFIG_DIR": str(per_game_dir)},   # session jsonl 落到 game 同级
    extra_args={
        "tools": "Write,Edit,Read,Glob,Grep" + mcp_tool_names,  # schema 注入层
        "bare": None,                                            # 关 CLAUDE.md / hooks / LSP / keychain
    },
)
```

关键不变式：
- `--bare` 不读 OAuth / keychain；当前用 `ANTHROPIC_AUTH_TOKEN`（OpenRouter 路径）或 `ANTHROPIC_API_KEY`，两者都仍可用
- 游戏目录的 `CLAUDE.md`（`_template` 自带）由 `_run_claude_agent` 手动 read + append 到 system_prompt，保留游戏专属规则可见
- `--tools` 是**注入给 model 的 schema 集合**，区别于 `--allowedTools` 只管 permission

### 调试钩子

`.env` 中设 `ROBOT_CODE_AGENT_DEBUG_DUMP_DIR=/tmp/robot-claude-debug`（需完整重启 backend 才生效，
详见仓根 `CLAUDE.md`）会让每次 modify 通过 SDK `extra_args` 注入 `--debug api --debug-file`，
把 CLI 内部 debug 日志（skill load / tool dispatch / autocompact level）落盘。
配合 `scripts/openrouter_proxy.py` 反代抓 request body，才能拼出完整 token 构成图。
