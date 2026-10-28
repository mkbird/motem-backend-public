# Robot Backend

桌面 AI 机器人的后端服务 — 基于 FastAPI，提供 AI 对话、语音识别/合成、日历任务管理等能力。

## 公开副本说明与安全边界

这是独立发布的去敏代码快照，不包含原私有仓库的 Git 历史、用户生成的
`games/ai_*` 工程、AI 会话 JSONL、上传头像、真实环境文件或凭据。
模板、参考素材和测试代码予以保留。原私有仓库与线上服务不会因本副本的
发布而自动获得修改。

- 启动前必须在本地 `.env` 或部署密钥管理器中配置自己的
  `ROBOT_JWT_SECRET_KEY`；不要使用示例字符串或他人 API Key。
  可用 `python -c 'import secrets; print(secrets.token_hex(32))'` 生成签名密钥。
- WebSocket 的数据归属由已认证用户决定。`group_id` 只能留空或等于当前
  用户 ID；共享组功能需要另外实现服务端成员权限校验。
- `scripts/start_server.py` 默认只监听 `127.0.0.1`，且不会自动启动独立
  gRPC 服务。只有在限制好网络访问后，才显式设置 `ROBOT_START_GRPC=1`。
- 公开源码不等于可以直接开放公网服务。生产部署还需 HTTPS/WSS、数据库
  最小权限、限流/配额、运行时隔离，以及对日志中的令牌和用户数据脱敏。
  游戏/小组件生成会运行代码生成工具，应放在独立受限环境，避免与生产
  凭据或其他用户数据共享文件系统权限。
- Python 包 `aichat` 来自独立的公开项目
  [AIChatServer](https://github.com/mkbird/AIChatServer)，不是 iOS SDK。
  `requirements.txt` 通过 HTTPS 引用固定提交，无需私有仓库权限。
  不要用来源不明的同名包替代它。
- 本次发布未自行添加或变更许可证。

## 环境搭建

### 1. 创建 Conda 环境

```bash
# 创建 Python 3.11 环境
conda create -n robot python=3.11 -y

# 激活环境
conda activate robot
```

### 2. 安装依赖

```bash
cd motem-backend

# 安装项目依赖
pip install -r requirements.txt

# 如需开发/测试依赖
pip install -e ".[dev]"
```

> **注意**：请使用 `requirements.txt` 安装固定提交的公开 `AIChatServer`/`aichat` 依赖；运行服务仍需自行配置数据库、Redis 和所用模型服务的凭据。

### 3. 安装并配置 MySQL

#### macOS（Homebrew）

```bash
# 安装 MySQL 8.0
brew install mysql

# 启动服务
brew services start mysql

# 首次安全配置（设置 root 密码等）
mysql_secure_installation
```

#### Ubuntu/Debian

```bash
sudo apt update
sudo apt install mysql-server -y
sudo systemctl start mysql
sudo systemctl enable mysql
sudo mysql_secure_installation
```

#### 创建数据库

```bash
# 登录 MySQL
mysql -u root -p

# 创建数据库（字符集 utf8mb4）
CREATE DATABASE robot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

# 退出
exit;
```

#### 运行数据库迁移

```bash
conda activate robot
cd motem-backend

# 执行 Alembic 迁移，创建所有表
alembic upgrade head
```

> 迁移默认连接 `mysql+aiomysql://root:@localhost:3306/robot`（无密码）。
> 如需修改，编辑 `alembic.ini` 中的 `sqlalchemy.url`，或通过 `.env` 配置。

### 4. 安装并配置 Redis

#### macOS（Homebrew）

```bash
brew install redis
brew services start redis
```

#### Ubuntu/Debian

```bash
sudo apt install redis-server -y
sudo systemctl start redis
sudo systemctl enable redis
```

验证：

```bash
redis-cli ping
# 应返回 PONG
```

### 5. 安装 Godot 4（AI 游戏生成需要）

AI 游戏创作功能使用 [godot-mcp](https://github.com/Coding-Solo/godot-mcp) 进行场景操作和运行时验证，需要 Godot 4 可执行文件。

#### macOS（Homebrew）

```bash
brew install --cask godot
```

安装后 Godot 位于 `/Applications/Godot.app/Contents/MacOS/Godot`，godot-mcp 会自动检测。

#### Ubuntu/Debian（Linux 服务器）

```bash
# 下载 Godot 4 Linux 无头版（无需 GUI）
wget https://github.com/godotengine/godot/releases/download/4.4.1-stable/Godot_v4.4.1-stable_linux.x86_64.zip
unzip Godot_v4.4.1-stable_linux.x86_64.zip
sudo mv Godot_v4.4.1-stable_linux.x86_64 /usr/local/bin/godot

# 验证
godot --version
```

> **注意**：Linux 服务器建议使用无头版（不带 `_mono` 后缀）。如果 godot-mcp 未能自动检测到 Godot，通过环境变量指定路径：
> ```bash
> export ROBOT_GODOT_PATH=/usr/local/bin/godot
> ```

#### 同时需要 Node.js / npx

godot-mcp 通过 `npx @coding-solo/godot-mcp` 启动，需要 Node.js 环境：

```bash
# macOS
brew install node

# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

## 环境变量

复制示例配置并编辑：

```bash
cp .env.example .env
```

编辑 `.env`，所有变量以 `ROBOT_` 为前缀：

```bash
# 数据库
ROBOT_MYSQL_HOST=localhost
ROBOT_MYSQL_PORT=3306
ROBOT_MYSQL_USER=root
ROBOT_MYSQL_PASSWORD=your_password
ROBOT_MYSQL_DATABASE=robot

# Redis
ROBOT_REDIS_URL=redis://localhost:6379/0

# JWT（生产环境务必修改）
ROBOT_JWT_SECRET_KEY=

# DashScope API Key（LLM、ASR/TTS、天气、搜索统一使用）
ROBOT_DASHSCOPE_API_KEY=your_api_key
ROBOT_OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ROBOT_DEFAULT_MODEL=qwen-plus

# Anthropic（Claude Code SDK, 游戏代码生成）
# 代理模式：AUTH_TOKEN + BASE_URL，API_KEY 留空
ROBOT_ANTHROPIC_AUTH_TOKEN=your_auth_token
ROBOT_ANTHROPIC_API_KEY=
ROBOT_ANTHROPIC_BASE_URL=https://your-proxy/claudecode
# 直连模式：仅设置 API_KEY
# ROBOT_ANTHROPIC_API_KEY=sk-ant-xxx

# Godot（godot-mcp，留空自动检测）
# ROBOT_GODOT_PATH=/usr/local/bin/godot

# Claude Code SDK 模型（游戏代码生成）
# 默认 z-ai/glm-5.1（OpenRouter，开源 SOTA SWE-bench 77.8%）
ROBOT_GAME_CODE_MODEL=z-ai/glm-5.1

# gRPC 语音服务地址
ROBOT_GRPC_ASR_ADDR=localhost:8011
ROBOT_GRPC_TTS_ADDR=localhost:8012

# 用户头像上传目录；相对路径按后端仓库根目录解析
ROBOT_AVATAR_DIR=static/avatars
```

## Profile / Social API

后端提供基础个人主页与关注关系接口：

- `GET /api/auth/me`：当前用户资料，包含 `username`、`bio`、`avatar_url`、`followers_count`、`following_count`
- `PATCH /api/auth/me`：更新 `display_name`、`username`、`bio`、`avatar_url`
- `POST /api/auth/me/avatar`：上传 JPEG / PNG / WebP 头像，默认写入 `ROBOT_AVATAR_DIR`
- `GET /api/users/{id_or_handle}`：查看其他用户公开资料；只返回公开字段，不返回邮箱、时区、locale
- `GET /api/users/{id_or_handle}/games?tab=created|liked|favorited`：Profile 游戏 tab 列表，返回结构与 `/api/games/*` 一致；查看他人时只返回公开 created / liked，`favorited` 返回空
- `POST /api/users/{id_or_handle}/follow` / `DELETE /api/users/{id_or_handle}/follow`：关注 / 取消关注
- `GET /api/users/{id_or_handle}/followers` / `following`：分页拉取粉丝 / 关注列表

`users.username` 对历史用户允许为 `NULL`；客户端需要以 `display_name` 或 `id` 兜底展示。删除账号时会同步清理双向 `user_follows` 关系。

## 启动服务

```bash
conda activate robot
cd motem-backend

# 直接启动
uvicorn robot.main:app --host 127.0.0.1 --port 8000
```

## 一键本地联调（ngrok）

提供脚本 `scripts/start_dev_with_ngrok.py`，自动完成：
- 启动后端 `uvicorn`
- 启动或复用 `ngrok http 8000`
- 获取 ngrok 公网地址

```bash
conda activate robot
python scripts/start_dev_with_ngrok.py
```

启动成功后输出本地地址和公网地址。`Ctrl+C` 退出并自动清理进程。

### 可选环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BACKEND_PORT` | 后端端口 | `8000` |
| `AICHATSERVER_PATH` | AIChatServer 路径 | `../AIChatServer` |
| `PYTHON_BIN` | gRPC 子进程 Python | 当前解释器 |
| `UVICORN_HOST` | 监听地址 | `127.0.0.1` |
| `KILL_EXISTING_BACKEND` | 退出时杀死占用端口的进程 | `0` |

## 测试

```bash
conda activate robot
cd motem-backend
python -m pytest tests/ --tb=short -v
```

## 测试账号

- 邮箱：`test@robot.com`
- 密码：`robot123`

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/auth/login` | POST | 登录 |
| `/auth/register` | POST | 注册 |
| `/auth/refresh` | POST | 刷新 Token |
| `/ws` | WebSocket | 全双工 AI 对话（需 Token + device_id） |
| `/api/calendar/events` | GET | 日历事件 |
| `/api/tasks` | GET/POST/PUT | 任务管理 |
| `/api/weather` | GET | 天气查询 |
| `/api/devices` | GET | 设备列表 |
| `/api/games` | GET | 兼容旧 iOS — 合并 explore + mine + fs walk fallback |
| `/api/games/explore` | GET | **小游戏市场** — 公开游戏列表（含内置 + 所有人 share 的 AI），auth 可选 |
| `/api/games/mine` | GET | **小游戏市场** — 自创 + 收藏，auth 必须 |
| `/api/games/{id}/download` | GET | 下载游戏项目 zip（private 仅 owner，builtin 404） |
| `/api/games/{id}/files` | GET | 获取游戏文件内容（visibility gate） |
| `/api/games/{id}/stats` | GET | 单游戏 share / like 计数 |
| `/api/games/{id}/share` | POST | 匿名 +1 分享计数 |
| `/api/games/{id}/like` | POST | toggle 点赞（auth 必须） |
| `/api/games/{id}/favorite` | POST | toggle 收藏（auth 必须，自创不允许） |
| `/api/games/{id}/remix` | POST | 二创 — 复制 Godot 工程 + 新建 ai_games 行（rejects own / view_only / builtin） |
| `/api/games/{id}/visibility` | PATCH | 修改可见性（owner-only，public_remixable / public_view_only / private） |
| `/api/games/{id}` | DELETE | 删除 AI 游戏（owner-only） |
| `/share/{id}` | GET | **公开** AI 游戏社媒分享落地页（OG / Twitter card meta + Pillow 渲染 1200×630 图 + AppsFlyer OneLink CTA），按 `Accept-Language` 渲染中英文 |
| `/share/{id}/og-image.png` | GET | **公开** 落地页 `og:image` PNG |
| `/api/widgets/explore` | GET | 全局探索区列表（公开 widgets，含内置 + AI 公开），auth 可选 |
| `/api/widgets/mine` | GET | 当前用户的「我的」（自创 + 收藏），auth 必须 |
| `/api/widgets/{id}/like` | POST | toggle 点赞 |
| `/api/widgets/{id}/favorite` | POST | toggle 收藏（自己创建的不能收藏自己） |
| `/api/widgets/{id}/remix` | POST | 二创：拷贝目录 + 写新行（`original_widget_id` 记血缘），原作 `remix_count++` |
| `/api/widgets/{id}/visibility` | PATCH | owner-only 修改可见性（`public_remixable` / `public_view_only` / `private`） |
| `/api/widgets/{id}/files` | GET | 获取 widget 源文件（visibility 鉴权：private 仅 owner） |
| `/api/widgets/{id}/download` | GET | 下载 zip（同上鉴权） |
| `/api/widgets/{id}` | DELETE | owner-only 删除（同步清理 view 表 + likes） |
| `/api/widgets` | GET | **legacy** — 兼容老 iOS，返回 `{widgets, seeds}` |

## 小组件市场（Widget Marketplace）

**单源 + 两张视图表**：所有 widget 在 `ai_widgets` 中只有一行 + 文件系统一份目录（builtin 无目录）。Tab 分布由两张索引表派生：

| 表 | 用途 |
|----|------|
| `ai_widgets` | 全局唯一组件源（`visibility` / `kind` / `original_widget_id` / `like_count` / `favorite_count` / `remix_count`） |
| `widget_likes` | 点赞关系（`(user_id, widget_id)` PK） |
| `user_widget_view` | per-user「我的」视图（`view_type` ∈ `created` / `favorited`） |
| `explore_widget_view` | 全局「探索区」索引（暂时所有用户共享） |

**事件驱动维护两张视图表** —— 见 `robot/widgets/service.py` 的 `set_visibility / add_favorite / clone_widget / _do_delete_for_owner` 等 helper。

**Redis 缓存**：`widgets:explore:v1`（60s）、`widgets:mine:{user_id}:v1`（60s）、`widgets:meta:{widget_id}:v1`（300s），任何写操作通过 `invalidate_widget_caches` 显式 DEL。

**Remix 语义**：作者修改自己的 widget = 原 widget_id 的 in-place 更新（**不新建**）；他人 `public_remixable` → POST `/remix` 拷贝目录 + 新建 DB 行，原作内容不变，`remix_count++`。

**内置组件**：5 个 (`builtin_time` / `weather` / `todo` / `focus` / `photo`) 由 alembic 迁移种子写入 `kind='builtin'` + `owner_id=NULL` + `visibility='public_view_only'`，iOS 端用原生 SwiftUI 渲染，不走 H5 文件层。

测试覆盖见 `tests/robot/test_widget_marketplace.py`（58 个用例：visibility 状态机 / 端点筛选 / like/favorite/remix / remix 链 A→B→C / 缓存失效矩阵 / 多语言 Accept-Language / 删除级联 / 端到端 lifecycle / view-index 防漂移）。

## 小游戏市场（Games Marketplace）

把 AI 游戏从"fs-only 元数据"升级为完整的市场后端。结构与小组件市场对照
（参考 [WIDGET_MARKETPLACE_PLAN](../motem-backend-widget/) 同款约束）。

**存储分层**（单源 + 视图表）：
- `ai_games` — 主表，每个 game_id 唯一一行 + filesystem 上一份 Godot 工程目录
  （kind='ai' 时；builtin 没有目录由 iOS 原生渲染）。包含 visibility / kind /
  input_mode / 4 个 count 列 / original_game_id 血缘 / session_id 续话 token
- `user_game_view` — per-user "我的"（view_type='created' / 'favorited'）
- `explore_game_view` — 全局 "探索区"（暂时所有用户共享）
- `game_likes` — 点赞统计（已存在，与 ai_games.like_count 双写期）
- 兼容期：老 `game_stats` 表保留，新代码读 ai_games，写双写；下个 release 后 drop

**关键约束**：
- 同一游戏只有一行 DB + 一份目录，不因可见性 / Tab 分布复制
- 作者 modify 自己已发布的游戏 = 原 game_id in-place 更新（不是 remix）
- Remix 仅在"非作者"对 `public_remixable` 触发 → 新 game_id + 新目录 + 新行
- Godot `.godot/` 编辑器缓存目录在 clone 时被 `service.clone_game` 显式剔除
- `input_mode`（'touch' / 'body_pose'）在 create / clone / list 全程透传
- 老 `ai_*` 目录无 ai_games 行时，`/like` `/favorite` `/share` `/stats`
  端点会自动懒回灌（防 alembic 迁移漂移 + 兼容期）
- `/files` 和 `/download` **不**走懒回灌：row 缺失统一 404，不再对登录用户
  开 fs fallback（防 visibility=private 被旁路；详见下方"安全契约"）
- Redis cache：`games:explore:v1` (60s) / `games:mine:{user_id}:v1` (60s) /
  `games:meta:{game_id}:v1` (300s)，事件驱动 DEL

**安全契约（P0 修复，2026-05）**：

1. **`game_id` 强校验** — fast-path 与 `modify_game` 都用 `_GAME_ID_PATTERN`
   (`^ai_\d+_[a-f0-9]{6}$`) 强匹配，拒绝 `_template` / `..` / 绝对路径 / 任
   何不符合 `_generate_game_id` 产出的 id。旧实现下，登录用户可发
   `[game_modify]{"game_id":"/Users/foo","instruction":"..."}` 让 Claude
   Code SDK 以 backend 进程身份读写主机任意目录（pathlib 在右侧绝对路径时
   返回右侧本身；ownership check 在 `meta.json` 缺失时静默放行）。
2. **`_check_files_access` row 缺失统一 404** — `/api/games/{id}/files` 与
   `/api/games/{id}/download` 不再对已登录用户走 fs fallback。未入库的老
   `ai_*` 目录在这两个端点上一律 404，避免 private zip 被旁路访问。其它
   端点（`/stats` `/like` `/favorite` `/share`）仍懒回灌以保兼容。
3. **`game_modification_failed` WS 事件** — `modify_game` 在 5 条失败路径
   推送 `robot_command { action: "game_modification_failed", game_id, reason }`：
   `invalid_game_id` / `game_not_found` / `not_owner` / `no_changes` /
   `internal_error`。iOS `AISessionManager.onGameModifyFailed` 拿到事件后
   立即复位 `aiGameCreating = false`，否则 GameCreator UI 永久卡死（与
   widget 历史 P0 同款的"三点闭环"硬约束）。
4. **`modify_game` ownership 查 DB 不读 meta.json** — 之前 ownership check
   只读 `game_dir/meta.json` 的 `owner_id` 字段，但 AI 在 modify 流程里有
   `Write` 工具完全可以覆写 meta.json，构成自我授权攻击；且 `meta_owner`
   为空时 `if meta_owner and ...` 整段跳过校验默认放行。现在改为先查
   `ai_games` 表 row（权威源），DB 行缺失直接 404 + 推 `game_not_found`
   失败事件；只有 DB 基础设施故障时才 fallback 到 meta.json（记 ERROR）。
5. **删除 create_game 硬编码绝对路径 fallback** — 旧实现在 main.gd 校验
   失败时主动从 `/home/developer/work/robot/scenes/main.gd` 等硬编码绝
   对路径拷贝文件回正确位置。风险：(a) 暴露部署主机路径；(b) 与"AI 只在
   cwd 内操作"的安全模型冲突，AI 经 prompt-injection Write 到 cwd 外的
   "诱饵路径"后会被后端主动拷进受害游戏目录。现已删除，AI 没写 main.gd
   直接走 `incomplete=True → game_creation_failed` 闭环。

**测试**：`tests/robot/test_game_marketplace.py` 36 case 覆盖
- visibility 状态机 / explore + mine 端点筛选 / like + favorite toggle / remix
  全流程（含 .godot 剔除 + main.gd 字节级一致 + meta.json 改写）
- 作者 modify 不新建（保 visibility 不被覆盖回 private 的关键防御）
- `/files` 访问鉴权（私有他人 → 404）
- 缓存失效（monkeypatch redis 验证 explore + mine + meta 三 key DEL）
- 端到端 user journey（11 步 like→favorite→remix→...）

## AI 游戏生成的成本优化

modify_game 单次平均成本经历多轮优化。下面给两套数字：**SDK
`ResultMessage.total_cost_usd`** 和反代抓到的 OpenRouter **真实账单**
（详见下节"成本数字口径"，SDK 估算对 haiku 4.5 偏高 ~5×）。

| 阶段 | SDK 估算 | OpenRouter 真实账单 | 关键改动 |
|---|---|---|---|
| 初版（28 turn 全量重写，sonnet 时期） | $0.28 | （未测） | — |
| 加 SDK session resume（haiku 4.5） | $0.49 | ~$0.10 | resume 重放 56K cache-miss tokens |
| sliding-window compact | $0.39 | ~$0.08 | jsonl 历史压缩到 ≤50KB |
| `--tools` 限制 schema | $0.29 | ~$0.06 | 内置 29 个工具只声明用到的 5 个，省 ~23K tok / round-trip |
| `--bare` 关 CLAUDE.md auto-discovery | $0.22 | ~$0.045 | 父目录递归扫到 4 份 CLAUDE.md ~7K tok 不再注入 |
| `use_mcp=False` 时去 MCP 段 | $0.21 | ~$0.043 | system_prompt 砍 ~137 tok / round-trip |

**成本数字口径**：

- **SDK 估算**（DONE 日志 `cost=$x.xxxx`）来自 `ResultMessage.total_cost_usd`，CLI 内置 model→price 表。
  CLI 2.1.138 对 `anthropic/claude-haiku-4.5` 缺失定价 entry，会 fallback 到 sonnet 量级（$3/$15
  vs 实际 haiku $1/$5），整体放大 ~5×。趋势可用，绝对值不可信。
- **OpenRouter 真实账单**：通过 `scripts/openrouter_proxy.py` 反代抓 `/v1/messages` SSE
  response 中的 `usage.cost` 字段累加得到，是 OpenRouter 实际扣费数。

详细方案与历史背景见 `docs/modify-game-cost-optimization.md`。

**Claude Code SDK 配置要点**（`robot/agent/tools/game_tools.py::_run_claude_agent`）：

- `extra_args = {"tools": "Write,Edit,Read,Glob,Grep", "bare": None}` — 限制 schema
  注入集合 + 关 CLAUDE.md auto-discovery / hooks / LSP / auto-memory
- `system_prompt` 显式提供领域上下文（`_build_system_prompt` + 游戏目录 `CLAUDE.md` 手动 append）
- `cwd=<game_dir>` + `env={"CLAUDE_CONFIG_DIR": str(<game_dir>.claude)}`：
  让 session jsonl 落到 game 同级 `<game_id>.claude/` 目录（remix 复制游戏目录自动带走会话）
- `use_mcp=True` 仅 create 路径，启动 godot-mcp 调 `create_scene` / `add_node`；
  modify 路径 `use_mcp=False`，system_prompt 自动去掉 MCP 段

**抓包诊断 prompt token 实际构成**：见 `CLAUDE.md` 中"调试 AI 游戏生成的 prompt token 消耗"小节
（透明 HTTP 反代 + `ROBOT_CODE_AGENT_DEBUG_DUMP_DIR` 双轨抓全 request body + CLI debug 日志）。

## AI 游戏创建 Fast Path

`create_game` 路径加了一层 archetype 路由 + 确定性拼装，命中时跳过 Claude Code SDK
agent loop，秒级出游戏。详细方案：[`docs/game-fastpath-implementation-plan.md`](docs/game-fastpath-implementation-plan.md)。

### 架构

```
用户文本 [game_create]
   ↓
[Router]      ← Haiku 单轮分类：archetype_id + confidence
   ↓
[置信度网关] ─── confidence ≥ 阈值 ─► [Customizer]    ← Haiku 单轮生成 GameRecipe JSON
   │                                       ↓
   │                                  [Compiler]      ← 纯 Python，0 LLM，拼 Godot 工程
   │                                       ↓
   │                                  [Finalize]      ← audio harvest + godot import + zip + DB + game_ready
   │                                       ↓
   │                                   ✓ done (~17s, ~$0.005)
   └─ confidence < 阈值（或任何环节异常）→ 现有 Claude Code SDK 链路（slow path）
```

iOS UI 完全不变；fallback 对用户完全无感（不发任何 fast-path 失败信号）。

### `input_mode` 必须写进 meta.json（与 DB 一致）

`Compiler` 写 `meta.json`、`Finalize` 写 `ai_games.input_mode`（DB）。**两处必须用同一个 `input_mode`**：archetype 一律 `body_pose`。历史 P0：`Compiler` 漏写 `input_mode` → fast-path 游戏 meta 无该字段（被老 `modify` 路径按默认 `touch` 回写），而 DB 是真实 `body_pose` → 下载到端上的 meta 与列表/DB 分叉，端侧据 meta 判定输入方式时体感游戏摄像头开不起来。

存量游戏用 `scripts/backfill_game_input_mode.py` 修复（以 DB 为权威回灌 meta + bump `modified_at` 触发端上重下 + 重打 zip）：

```bash
conda activate robot
python scripts/backfill_game_input_mode.py --dry-run   # 只报告
python scripts/backfill_game_input_mode.py             # 实际修复（幂等）
```

### Phase 1 实测（OpenRouter Haiku 4.5）

| 指标 | 数值 |
|---|---|
| 命中率（5 catch prompts） | 5/5 (100%) |
| Fallback 准确率（5 非 catch） | 5/5 (100%) |
| 多样性（drop_sprite 去重） | 5/5 unique |
| 单次成本 | ~$0.005 |
| Wall-clock p50 / p95 | 16.7s / 18.7s |
| Customizer cache_read | 0（OpenRouter 账户级关闭，**目前唯一拿到 cache 的路径是 BYOK sk-ant-* 直连 anthropic.com**） |

### 配置

```bash
# .env
ROBOT_FASTPATH_ENABLED=1              # 默认 False；staging 灰度时开
ROBOT_FASTPATH_CONFIDENCE_THRESHOLD=0.7  # 路由阈值，宁高勿低
ROBOT_FASTPATH_TRACE_PATH=logs/game_creation.jsonl  # 观测埋点路径，默认 logs/
ROBOT_FASTPATH_TRACE_DISABLE=         # 1=测试用关闭埋点
```

### 当前覆盖 archetype（v2 全量上线）

| archetype | 玩法 | 深度机制 |
|---|---|---|
| `catch_v2` | 接物（人脸 / 倾斜横向移动） | 3 lane 风险分层 + Combo Path + Charge Catch + Frenzy |
| `dodger_v2` | 闪避 | Graze 双半径 + Dash 能量 + 3 hazard 类型 + 5 wave cycle + 失败热力图 |
| `whack_v2` | 打地鼠 | Lifetime × Score 反比 + 10% Disguise morph + Quick Streak Frenzy |
| `slicer_v2` | 划手势 | Douglas-Peucker 简化 + 几何识别（LOOP/INFINITY/ZORRO/TRIPLE+） |

v1 文件保留在 `strategies/` 作 reference 不再注册；老 zip 完全 self-contained，不受影响。

### Endless 模式 + 最高分纪录

所有 v2 archetype 都是 endless（没有 `win_score`，只有 `lives ≤ 0` 才 GAME_OVER），最高分跨局持久化：

- `games/_template_fastpath/scripts/high_score.gd` 是共用 helper，`class_name HighScore extends RefCounted`。
- 每个 `game_id` 一个 `user://hs_<game_id>.json` 文件，节流 1s 写盘。
- strategy 在 `_init_game` 调 `HighScore.load_best()`，在 `_update_gameplay` 末尾调 `HighScore.tick(self, score, game)`。
- 当前局分数首次超过盘上 `best_at_start`（且 best > 0）时，tick 触发 **NEW RECORD!** 庆祝：`Particles.combo_blast` + `Juice.impact` + `Juice.time_dilation(0.5, 0.4)` + `AudioManager.play("pickup")`。
- HUD 右上角持续显示 `Best: N`；新增 v2 archetype 时复用同一套 state 字段（`best_at_start / best_score / broke_record / last_save_t`）。

### 观测

```bash
python scripts/fastpath_stats.py            # 全量
python scripts/fastpath_stats.py --since 1d # 近 1 天
```

打印每日 fast/slow 流量比、命中率、p50/p95 延迟、token 消耗、fallback 原因分布、archetype 命中分布、置信度近似 NMS 建议。

### 端到端真实 smoke

```bash
conda activate robot
python scripts/fastpath_smoke.py            # 跑 10 个典型 prompt，看路由 + 多样性
FASTPATH_SMOKE_BACKEND=dashscope python scripts/fastpath_smoke.py  # 切回 qwen-plus 对比
```

产物落到 `/tmp/fastpath_smoke/<ts>/`，每个 prompt 一个生成好的 Godot 工程目录 + 一份 `_results.json` 聚合。

## 中英双语

- HTTP、WebSocket 和 Agent 请求以客户端 `Accept-Language` 为准，空值或非支持语言回退到 `en`，中文统一为 `zh-Hans`。
- 后台 scheduler 和离线通知以数据库 `users.locale` 为准。
- 用户可见文案统一通过 `robot/i18n.py` 渲染；新增 key 使用 `SCREAMING_SNAKE_CASE`，必须同时补齐 `en` 和 `zh-Hans`。
- 主 Agent 和游戏开发 Agent 的内部 prompt、tool schema、Claude Code SDK prompt 保持英文；最终回复、游戏进度、错误和通知按用户语言输出。

## 消息推送（APNs）

**链路：** iOS 注册 → `POST /api/auth/me/device-token`（写 `user_devices.apns_token` + `apns_environment`）→ `robot.notification.push_service.send_push(...)` → aioapns（token-based JWT，HTTP/2）→ APNs sandbox / production。

**认证：** token-based `.p8`，一份 key 同时覆盖 sandbox + production，env 由 `user_devices.apns_environment` 决定路由到 `api.sandbox.push.apple.com` 还是 `api.push.apple.com`。

**`.env` 必填四项**（任一为空或 `.p8` 不存在 → `push_service._is_configured()` 返回 False，退化成 log-only，不影响其他功能）：

```
ROBOT_APNS_KEY_PATH=
ROBOT_APNS_KEY_ID=
ROBOT_APNS_TEAM_ID=
ROBOT_APNS_BUNDLE_ID=
```

> `.p8` 私钥**绝不入库** —— `.gitignore` 已加 `secrets/` 与 `*.p8`。

**关键文件：**

| 文件 | 职责 |
|------|------|
| `robot/notification/push_service.py` | `send_push` / `send_silent_push`，按 device.apns_environment 分 sandbox/prod；缓存 `(env, topic) → APNs` client；遇 `BadDeviceToken/Unregistered/410` 自动清死 token |
| `robot/auth/router.py` `POST /api/auth/me/device-token` | iOS 端 `didRegister...DeviceToken` 后上报 hex token + environment ∈ {sandbox, production} |
| `robot/auth/service.py::register_device` | 幂等 upsert (user_id, device_id)，同时刷 `apns_token` / `apns_environment` |
| `robot/db/models.py::UserDevice` | 新列 `apns_environment VARCHAR(20)`，迁移 `k1l2m3n4o5p6_add_apns_environment` |
| `robot/notification/scheduler.py` | calendar reminder / task deadline 触发时**始终发 APNs**（在线 + 离线都发）；用户在线时附加 WebSocket `notification` event 给 in-app 红点 / 列表实时刷新。两条通道并行：APNs 给系统层 banner，WS 给应用内状态 |

**冒烟测试一条真实推送：**

```python
# conda activate robot && python -
import asyncio
from sqlalchemy import select
from robot.db.database import async_session
from robot.db.models import UserDevice
from robot.notification.push_service import send_push

async def main():
    async with async_session() as db:
        d = (await db.execute(
            select(UserDevice).where(UserDevice.apns_token.isnot(None)).limit(1)
        )).scalar_one()
        ok = await send_push(db, d.user_id, title="Hello", body="from APNs",
                              category="system", data={"kind": "smoke_test"})
        print("delivered:", ok)

asyncio.run(main())
```

## AI Agent 取消语义

`robot.agent.streaming.run_agent_with_streaming` 现在按 `cancel_check()` 返回值的**字符串原因**决定行为，不再纯 truthy。aichat（AIChatServer）从 v4 起把 `turn.cancel_reason` 透出给下游 streamer。

| reason | 行为 | 场景 |
|--------|------|------|
| `"speech_stopped"` | **忽略**，stream 继续跑（pass） | 用户没说话了 / 离开摄像头 / ASR 超时 → 不该砍掉正在进行的 LLM / tool |
| `"new_turn_created"` | 检查 `UNCANCELLABLE_TOOLS`，无则 break | 用户真说了第二句要打断 → 砍 stream + 重新走新 utterance |
| 其它 / bool True | 按"取消"处理（向后兼容旧 caller） | 历史代码 / 测试桩 |

`UNCANCELLABLE_TOOLS = {create_game, modify_game, create_widget, modify_widget}` —— 已经进入这些「昂贵 + 强副作用」工具调用流后，任何 reason 都不取消（已经烧了几十秒 LLM 钱 + 文件落盘 + DB 行，半截砍掉等于扔了所有算力 + 前端永远收不到 ready）。便宜的只读 tool（calendar / weather / search / photo …）保留可取消，让 "stop + 补一段" 拼接重跑的语义照常工作。

## 技术栈

| 组件 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | MySQL 8.0 + async SQLAlchemy 2.0 + aiomysql |
| 迁移 | Alembic |
| 缓存 | Redis |
| 认证 | JWT（python-jose + passlib/bcrypt） |
| AI Agent | LangGraph + LangChain + OpenAI 兼容 API |
| AI 游戏生成 | Claude Code SDK + Godot 4 项目模板 + godot-mcp，支持触摸/视觉控制模式 |
| 语音 | gRPC ASR/TTS（DashScope Paraformer + CosyVoice） |
| 定时任务 | APScheduler |

## Web 游戏交付 + Discord Bot（视觉控制浏览器版）

把生成的 Godot 游戏**无头导出成浏览器可玩的 HTML5 包**，对外经公开免登录链接
`/play/{game_id}/` 游玩（摄像头视觉控制，可分享）；并把生成 agent 包装成 Discord
bot，用户发文本即可创作/修改、回链接。详细设计见
`docs/discord-game-bot-v1-plan.md`，bot 联调见 `robot/bot/README.md`。

- **Web 导出管线** `robot/games/web_export.py`（+ `scripts/export_game_web.py` CLI）：
  渲染器 `mobile`→`gl_compatibility`、CJK 字体打包、模板 web 分支刷新、注入视觉 shell、
  无头 `godot --export`、`emulate_touch_from_mouse`（桌面鼠标点击=触摸，开始游戏跨端可用）。
- **公开链接** `robot/games/play_router.py`：`GET /play/{game_id}/`，正确 content-type
  （`application/wasm` 等）+ 防目录穿越；bundle 落 `web_builds/{game_id}/`（已 gitignore）。
- **流水线接入**：create/modify 收尾 `asyncio.create_task(export_and_publish_async)`
  fire-and-forget（`asyncio.to_thread` 跑阻塞 godot），门禁 `ROBOT_WEB_EXPORT_ENABLED`
  （默认关），失败只 warning、绝不影响游戏创建；modify 重导刷新同一链接。
- **视觉输入** `robot/games/assets/web_runtime/motem_input.js`：MediaPipe FaceLandmarker
  → `window.__motem_input` → `JavaScriptBridge` → InputBridge。游戏模板
  `games/_template/scripts/{input_bridge,audio_manager}.gd` 内置 `OS.has_feature("web")`
  分支（新游戏出厂即 web-ready，iOS/macOS 不受影响）。
- **Bot 适配层** `robot/bot/` + `scripts/run_discord_bot.py`：
  - `agent.py` — **精简 game-agent**（只绑 `create_browser_game`/`modify_game` + 对话），LLM
    自己判断创建/修改/闲聊；`run_game_agent` 从 astream 抓 game_id（create←ToolMessage、
    modify←tool_calls）。
  - `active_game.py` — 按 Discord 用户的"当前游戏"（Redis + TTL），随口跟进改正确的游戏。
  - `progress.py` — ws_manager 形状的进度桥，把生成进度实时编辑回 Discord 状态消息（像 app）。
  - `discord_bot.py` — 客户端：**按用户隔离记忆/配额**（user_id=Discord 用户、owner=bot 账号）、
    per-user 串行锁 + 全局并发上限、私聊支持；回复用 **rich Embed 卡片**（`service.load_game_card`
    读 `meta.json` 的标题/描述/图标）+ **缩略图**（进程内 `share_router._render_og_image` 渲染
    1200×630 PNG，作为附件直传，绕过 ngrok 免费版抓图拦截页）。
  - `tunnel.py` — 启动时查 ngrok 本地 API（`:4040`）**自动发现公网 URL**，没配
    `ROBOT_SHARE_PUBLIC_BASE_URL` 时自动用 → /play 链接走公网。细节见 plan §5.1。
  - `gating.py` — kill-switch + 内容护栏 + 按发起者每日配额（复用 Redis 限流）。
- **Fast Path 统一**：`game_tools._try_fastpath` 一处实现，`create_game` 内部先 fast-path 后
  SDK；iOS `[game_create]`、主 agent、bot 三条创建路径都受益（iOS 用 `_skip_fastpath` 避免双跑）。
- **运行前置**：服务器装 Godot 4.6 + Web 导出模板；`.env` 设 `ROBOT_WEB_EXPORT_ENABLED=1`、
  `ROBOT_GODOT_PATH`、`ROBOT_SHARE_PUBLIC_BASE_URL`、`ROBOT_DISCORD_BOT_*`、
  `ROBOT_BOT_OWNER_USER_ID`、`ROBOT_BOT_CREATION_ENABLED`、`ROBOT_BOT_DAILY_{CREATE,MODIFY}_LIMIT`。
