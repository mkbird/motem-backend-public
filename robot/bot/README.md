# Bot 适配层（① v1）

把游戏生成 agent 包装成聊天机器人：用户发文本 → 创作/修改游戏 → 回一个
公开、免登录、可分享的 `/play/{game_id}/` 浏览器链接（摄像头视觉控制）。

bot 是**独立进程**，复用现有 agent（`create_game` / `modify_game`），
不走 iOS WebSocket。

## 组成

| 文件 | 职责 |
|---|---|
| `service.py` | 平台无关桥接：意图解析 / 链接构造 / game_id 提取 / 回复文案 / `handle_message` |
| `discord_bot.py` | Discord 客户端（`discord.py`，懒导入），监听频道→调 service→回链接 |
| `../../scripts/run_discord_bot.py` | 启动入口 |

## 联调前置

1. **装依赖**（只在跑 bot 的机器上）：
   ```bash
   conda activate robot && pip install discord.py && pip freeze > requirements.txt
   ```

2. **拿 Discord token**（见 `docs/discord-game-bot-v1-plan.md` 获取步骤）：
   Developer Portal 建 Application → Bot → Reset Token；**打开 MESSAGE CONTENT INTENT**；
   OAuth2 URL Generator 勾 `bot` + `Send Messages`/`Read Message History`/`Embed Links`，
   邀请进你的测试服务器。

3. **建一个后端用户**当 bot 的创作身份，拿到 `users.id`（UUID）。

4. **配 `.env`**：
   ```
   ROBOT_DISCORD_BOT_ENABLED=1
   ROBOT_DISCORD_BOT_TOKEN=<你的 bot token>      # 绝不进 git
   ROBOT_DISCORD_BOT_CHANNEL_ID=<测试频道 id>     # 可选，限定一个频道
   ROBOT_BOT_OWNER_USER_ID=<后端 users.id>
   ROBOT_SHARE_PUBLIC_BASE_URL=https://<对外域名>  # 链接前缀（如 ngrok 域名）
   ROBOT_WEB_EXPORT_ENABLED=1                      # 否则 /play 不会有内容
   ROBOT_GODOT_PATH=/path/to/godot                 # 需装 Web 导出模板
   ```

5. **跑起来**：
   ```bash
   conda run -n robot python scripts/run_discord_bot.py
   ```
   在测试频道发「做个躲陨石的太空游戏」→ bot 回「创作中…」→ 完成后回 `/play` 链接。
   修改：`修改 ai_xxx_yyyyyy 敌人加速` 或 `modify ai_xxx_yyyyyy: make enemies faster`。

## 用法（聊天里）

- **创作**：直接描述要什么游戏（默认 `body_pose` 视觉控制，适配 Web）。
- **修改**：`modify <game_id>: <指令>`（也认 `修改 <game_id> <指令>`）。

## 待办（联调时定）

- **用户映射**：当前用单一 `bot_owner_user_id`；后续可做 Discord 用户 ↔ 后端用户的
  按人映射（需要 users 表加 external_auth_id 或类似）。
- **公共门禁**（⑤）：公开开放前必须加配额 / 异步队列 / 成本熔断 / 内容护栏。
- **进度推送**：现在只发「创作中…」+ 最终链接；可接更细的阶段进度。
- **平台扩展**：service.py 平台无关，换 Telegram 只需新增一个 `telegram_bot.py`。
