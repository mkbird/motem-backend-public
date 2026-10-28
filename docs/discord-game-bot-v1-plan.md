# v1 核心功能落地方案：游戏创作 bot + 视觉控制 Web 玩

> 本文是**本次要实现的核心功能**的方案（架构/范围级，暂不含具体实现）。
> 背景与全量可行性见 `discord-bot-integration-plan.md`；本文是其中被选定的 v1 落地路径。

---

## 0. 已锁定范围

- **机器人**：把服务端**游戏生成 agent** 包装成 bot，用户通过 bot 文本**创作 / 修改**游戏。
- **交付**：服务端生成游戏后回一个 **web 链接**。
- **玩 + 分享**：点链接在浏览器里玩；链接**公开、免登录、可随意转发**，别人点开也能玩。
- **控制方式（已确认）**：**只做视觉控制**（摄像头体感），不做触摸/键鼠。
- **链接（已确认）**：**公开可分享、免登录**；创作/修改仍绑定 bot 用户。

### 由"只做视觉控制"锁定的三条后果

1. **没有"打开即玩"**：每次玩都有「摄像头授权 → 模型下载 → 校准预热」三步，浏览器层面删不掉。
2. **手机是头号风险**：公开分享多在手机浏览器打开，前摄 + MediaPipe + Godot WASM 同跑，中低端机会卡。
3. **控制方案偏向脸/手、避开全身 pose**：`face_position`(脸) + 手势(手) 正常坐姿即可用、只需两个模型；
   `body_lean/ducking/hands_up`(全身) 要露躯干退后、最重，Web 上体验差。
   → **生成 agent 为 Web 交付的游戏应优先生成脸/手控制方案。**

---

## ✅ 命门验证结果（已完成）

用标准 **Godot 4.6.2 stable** 编辑器 + 官方 Web 模板，对一个现有 fast-path 游戏
（`games/ai_1779167153_af963b`，body_pose 视觉控制）做了无头 Web 导出并在浏览器跑通。

**已证实可行**：
- 导出成功，产物 `index.html / index.js / index.wasm(37.7MB) / index.pck / audio worklets`。
- 浏览器启动：**WebGL2 / Compatibility 渲染器正常**；**单线程(nothreads)包，无需 COOP/COEP 跨域隔离头**，普通静态服务器即可。
- 4 个 autoload（I18n / AudioManager / GameManager / InputBridge）全部初始化；场景加载 + 切换正常；音频系统装载（8 sfx + 1 bgm）。
- **canvas 正确渲染**（标题/副标题/字体/拉伸都对），零致命报错。

**验证中暴露、管线必做的 4 条**：
1. **渲染器必须切**：游戏 `project.godot` 现为 `rendering_method="mobile"`（Vulkan，Web 跑不了），
   导出前要改成 `"gl_compatibility"`。管线里做一次性变换即可。
2. **CJK 字体必须打包**：导出时 `res://fonts/NotoSansSC-Regular.otf` 加载失败（游戏目录未自带），
   **中文会渲染不出**。Web 导出需确保把 CJK 字体打进包（或走共享字体）。
3. **InputBridge 需 Web 输入入口**：Web 上无 `/root/__swiftgodotkit_bridge__`，InputBridge 停在默认值、
   游戏不响应。需 MediaPipe → `JavaScriptBridge` → InputBridge（模块 ④）。
4. **AudioManager 需 Web 加载分支**（已实测确认）：现有 `AudioManager._load_stream()` 用
   `FileAccess.file_exists()` + `AudioStreamOggVorbis.load_from_file()` 直接读**原始 .ogg 字节**，
   只在 iOS libgodot 的松散目录下成立。Web 导出 pck 里只有**导入后资源**（`.godot/imported/*.oggvorbisstr`），
   没有原始 .ogg → 运行时 `res://audio/x.ogg does NOT exist`、全程静音。
   - `include_filter="*.ogg"`（把原始文件塞进 pck）实测**无效**，不要走这条。
   - 正确修法是**代码层**：Web 上改用 `ResourceLoader.load("res://audio/x.ogg")`，它通过 `.import`
     重映射命中 pck 里的导入资源。给 AudioManager 加一条 Web 分支即可（有界改动，非架构问题）。
   - 渲染本身不受影响——音频缺失不影响画面验证。

**结论：命门②（Godot Web 导出管线）成立**，是工程量而非可行性问题：渲染/运行/autoload/场景全通过，
剩下渲染器切换、字体打包、InputBridge Web 入口、AudioManager Web 加载分支这 4 处都是有界改动。

---

## ✅ 实现进度（持续更新）

- **② Web 导出管线**：已落地 `robot/games/web_export.py`（4 变换 + 无头导出 + 注入）
  + `scripts/export_game_web.py` CLI；真实完整游戏浏览器验证通过（渲染/视觉控制/音频）。
  模板 `games/_template/scripts/{input_bridge,audio_manager}.gd` 内置 `OS.has_feature("web")`
  分支（新游戏出厂即 web-ready，iOS/macOS 不受影响）。
- **③ 静态托管 + 公开链接**：`web_export.publish_build`（原子覆盖到 `web_builds/`）
  + `robot/games/play_router.py`（公开免登录 `GET /play/{game_id}/`，正确 content-type
  含 `application/wasm` + 防目录穿越）；挂载到 `robot/main.py`。
- **④ 视觉输入**：face-only 原型已验证（MediaPipe FaceLandmarker → `window.__motem_input`
  JSON → `JavaScriptBridge` → InputBridge）；shell 在 `robot/games/assets/web_runtime/motem_input.js`。
  body-pose / hand-gesture 生产者 + FaceDetector 解释逻辑移植待做。
- **⑥ 创作流水线接入**：create/modify 收尾后 `asyncio.create_task(export_and_publish_async)`
  fire-and-forget（`asyncio.to_thread` 跑阻塞 godot），门禁 `settings.web_export_enabled`（默认关），
  异常吞掉不影响创建；modify 重导刷新同一 `/play` 链接。接入点：fast `finalize.py`、
  slow `game_tools.create_game/modify_game`。
- **桌面可玩性**：导出时开 `emulate_touch_from_mouse`，桌面鼠标点击 = 触摸（骨架 tap-to-start 跨端可用）。
- **① Bot 适配层（已联调跑通：创建/修改/对话 + 视觉控制玩）**：
  - **改为 agent 驱动**（不再手搓意图解析）：`robot/bot/agent.py` 精简 game-agent，只绑
    `create_browser_game`/`modify_game` + 对话（避开主 agent ~15K token 全量工具）；LLM 自己判断
    创建/修改/闲聊。`run_game_agent` 从 astream 抓 game_id（create←ToolMessage、modify←tool_calls）。
  - **多人隔离**：记忆/活跃游戏/配额按 **Discord 用户 id**（user_id），游戏归属挂 bot 账号（owner_id）；
    `active_game.py`（Redis+TTL）让随口跟进改对游戏。**并发**：per-user 串行锁 + 全局 `bot_max_concurrency`
    信号量 + 重活走线程；**私聊**支持。
  - **进度像 app**：`progress.py` ws_manager 形状的桥，把生成进度实时编辑回 Discord 状态消息。
  - 入口 `scripts/run_discord_bot.py` + `robot/bot/README.md`；config 加 `discord_bot_*` /
    `bot_owner_user_id` / `bot_max_concurrency`。
- **Fast Path 统一下沉**：`game_tools._try_fastpath` 一处实现，`create_game` 内部先 fast-path 后 SDK；
  iOS `[game_create]`、主 agent、bot 三条创建路径都受益；iOS 预调用块保留其 meta 优化，用
  `_skip_fastpath` ContextVar 避免 miss 时双跑。streaming `_try_fastpath_create` 降为薄委托。
- **⑤ 创作门禁（已落地）**：`robot/bot/gating.py` —— kill-switch（`bot_creation_enabled`，
  急停一切创作）→ 内容护栏（`screen_prompt`：空/超长/starter denylist，待换真审核 API）→
  按发起者每日配额（复用 `rate_limiter.check_ws_create_limit`，Redis 计数，按 Discord 用户 id
  keying）。在 `discord_bot.on_message` 里跑 agent **之前**拦截（不为被挡请求烧钱）；被挡回
  双语友好提示 `format_blocked`。config 加 `bot_creation_enabled` / `bot_daily_create_limit` /
  `bot_daily_modify_limit`。播放永不门禁。
- **测试**：`test_web_export.py`(20) + `test_play_router.py`(6) + `test_bot_service.py`(7,改写)
  + `test_bot_gating.py`(12) + `test_bot_agent.py`(7) + `test_bot_active_game.py`(6)
  + `test_bot_progress.py`(5) + `test_create_game_fastpath.py`(5) + `test_fastpath_streaming.py`(5)
  = 73 全过；`test_web_export_integration.py`(1，需 `RUN_GODOT_INTEGRATION=1`)默认 skip。
  集成级未单测（已实弹验证）：`discord_bot.on_message` 端到端 + 并发、流水线 export 触发、
  GDScript web 分支运行时。
- **env**：新增 keys 已写入 `.env` + `.env.example`（web_export / share base url / discord bot /
  bot owner / 门禁配额）。
- **待办**：① 联调（token）、④ 深化视觉控制（body/hand + FaceDetector 解释逻辑移植）、
  bot 按人用户映射、内容护栏换真实审核 API、异步创作队列（高并发时）。

---

## 1. 端到端流程

```
用户 ──(bot 发文字: 创作/修改)──▶ Bot 适配层 ──▶ 游戏生成 agent (复用)
                                                      │ 产出 Godot 工程
                                                      ▼
                                            Godot Web 无头导出  ★命门
                                                      │ HTML5 包
                                                      ▼
                                            静态托管 + 生成可分享 URL
                                                      │
   用户 ◀──(bot 回 web 链接 + 进度)───────────────────┘
     │
     ├─ 点链接 → 播放页(加载→授权摄像头→校准→视觉控制玩)
     └─ 转发链接 → 任何人点开也能玩(免登录)

   用户 ──(bot 发文字: 修改)──▶ 同 game_id 原地更新 → 重新导出 → 同一链接内容刷新
```

---

## 2. 模块分解（复用 vs 新建）

**复用**：游戏生成 agent（create / modify）、game_id 体系、meta.json、本地化进度推送、
modify 原地更新语义。

| 模块 | 职责 | 性质 | 难度 |
|---|---|---|---|
| **① Bot 适配层** | 收文本 → 调 agent → 推进度 → 回链接；bot 当独立客户端 | 新建 | 轻 |
| **② Godot Web 导出管线** ★ | 生成的 Godot 工程**无头导出 HTML5**（标准 Godot 模板 + Compatibility + 单线程）；modify 后重导 | 新建 | **重，命门** |
| **③ 静态托管 + 链接** | 导出包发 CDN/静态桶；公开免登录 URL；稳定指向 game_id，内容随 modify 刷新 | 新建 | 中 |
| **④ Web 播放页 + 视觉输入** | 加载页/tap-to-start；MediaPipe 视觉栈；经 `JavaScriptBridge` 把识别结果写进 InputBridge | 新建 | 中~重 |
| **⑤ 创作门禁** | 创作绑 bot 用户 + 配额/异步队列/成本熔断；播放公开 | 新建 | 中 |

> **能否落地的瓶颈 100% 在 ②。** bot 那层最轻。现有管线只产 SwiftGodotKit(iOS) 能用的工程，
> 不产 Web 包，必须新建无头导出步骤。

---

## 3. ② Godot Web 导出管线（命门，要点）

- **引擎自带 Web 导出**：标准 Godot 4.6 编辑器 + 官方 Web export 模板，无头(headless)导出。
  与 iOS 用的 libgodot fork 是两套运行时，但**游戏工程文件通用**，不为 Web 重写游戏。
- **必须 Compatibility 渲染器**（WebGL2）；2D 游戏没问题。
- **单线程导出**（Godot 4.3+ 支持）绕开 SharedArrayBuffer/COOP-COEP 头要求，利于分发。
- **modify 重导**：同 game_id 修改后需重新跑导出、覆盖托管内容；链接不变。
- **要验证**：现有 fast-path 游戏在标准 Web 运行时下能跑（音频手势、输入、性能、手机 Safari）；
  既往 P0「不扫 class_name 必须 preload」在 Web 上不反咬（标准运行时反而扫 class_name）。

---

## 4. ④ 视觉控制落地（库选型 + 边界）

- **库**：主推 **MediaPipe Tasks (`@mediapipe/tasks-vision`)**，备选 TensorFlow.js。
  - **Face Landmarker** → `face_position`、yaw/pitch（变换矩阵直接出）、眨眼（blendshapes 自带 `eyeBlink`，
    省掉 iOS 手算 EAR 那套）。
  - **Hand Landmarker / Gesture Recognizer** → 手势 + 手位置。
  - **Pose Landmarker** → 全身 lean/ducking/hands_up（**v1 尽量不用**，见 0.3）。
- **输入桥接**：游戏只读 InputBridge ~10 属性、与输入源无关。Web 视觉栈 = **第三个 InputBridge 生产者**
  （继 iOS FaceDetector、macOS MacFaceDetector），经 Godot 4 `JavaScriptBridge` 写属性，**游戏代码不改**。
  需给 InputBridge 增加 Web 输入入口（替代 iOS 的原生宿主信号）。
- **必须重写的"解释逻辑"**：landmarks → 语义属性的映射（lean 归一化+死区、ducking 速度判定、
  比心/ok/wave 等非现成手势分类）≈ 现有 `FaceDetector.swift` 那块逻辑，要在 JS 重写。
  眨眼/朝向因 MediaPipe 自带反而更省。
- **性能策略**：错帧跑（脸每帧、手/身体降频）；按游戏只加载需要的模型；可放 Web Worker。
- **三件消不掉的摩擦**：摄像头授权点击、模型下载、校准预热（见 0.1）。

---

## 5. ③ 链接与分享

- **公开、免登录**：链接带不可猜的标识，拿到即玩，无需任何账号。
- **创作归属**：创作/修改在 bot 侧绑定用户；播放完全公开。
- **稳定链接 + 内容刷新**：链接指向 game_id；modify 后重导覆盖，链接不变、内容更新。
- **分享即转发 URL**，无额外机制。

### 5.1 Discord 链接展示（A + B 已实现）

- **现象**：bot 回裸 `localhost` URL 时 Discord **不出预览卡片**（爬虫够不到本机；且 Godot
  `index.html` 无 OG 标签）。
- **A（已实现）**：bot 不靠爬虫，直接发 **rich Embed** 卡片——`service.load_game_card` 读
  `meta.json` 的 `title_i18n/description_i18n/icon`，`discord_bot` 构造 `discord.Embed`
  （图标+标题可点 → play URL、描述、"▶️ 在浏览器游玩"、页脚）。
- **B（已实现，复用 iOS 分享设施）**：
  1. **公网 URL 自动发现**：`robot/bot/tunnel.py` 启动时查 ngrok 本地 API
     `:4040/api/tunnels`（同 `start_dev_with_ngrok.py`），无 `ROBOT_SHARE_PUBLIC_BASE_URL`
     时自动用 ngrok 公网域名 → `build_play_url` 出公网 /play 链接。
  2. **缩略图 = 进程内渲染 + 附件上传**：复用 `share_router._render_og_image`（1200×630 PNG，
     PIL + NotoSansSC，本就为 iOS 分享 `/share/{id}/og-image.png` 写的），bot **在自己进程里
     渲染 → `discord.File` 附件上传**，embed `set_image("attachment://og.png")`。
  3. ⚠️ **关键坑（为什么不让 Discord 抓 URL）**：**ngrok 免费版对 Discord 抓图（Discordbot UA）
     返回拦截警告页（text/html）而非 PNG** → URL 式 `og:image` 在 Discord 显示不出来。所以走
     **附件上传**绕过：图由 bot 本机生成直传，Discord 不发起任何对 ngrok 的抓取。点击 play 链接
     时浏览器仍会遇到 ngrok 免费版一次性 "Visit Site" 确认页（换 cloudflared / ngrok 付费域名
     可免）。
  4. 现成 `/share/{id}` 落地页本身带全套 OG（给 iOS 分享用）；若要让裸 `/share` URL 转发到
     微信/Slack 也出大图 + 提供"网页游玩"入口，再给落地页加 `/play` 按钮（可选，未做）。

---

## 6. ⑤ 创作门禁（公共必做）

- 创作走昂贵的生成 agent（Claude Code SDK / fast-path），公共下任何人可触发 → 必须：
  - 每用户配额（每天 N 个 / 修改次数）；
  - 创作走**异步队列 + 进度消息**（不阻塞 bot）；
  - 总成本上限 + kill-switch；
  - 内容护栏（prompt 滥用 / NSFW），遵守平台 ToS。
- **播放不门禁**（公开免登录），门禁只加在**创作/修改**侧。

---

## 7. 主要风险

1. **手机浏览器性能**（视觉栈 + WASM 同跑）——v1 头号风险，需早做真机实测。
2. **Godot Web 导出兼容性**——现有游戏在标准 Web 运行时是否全跑通，需一轮验证。
3. **视觉控制工作量**——FaceDetector 解释逻辑移植 + 非现成手势分类。
4. **"无打开即玩"与"公开休闲分享"的体验张力**——已知并接受。
5. **成本失控**——公共创作的烧钱风险，门禁要先于开放上线。

---

## 8. v1 内部落地顺序（建议）

> 让"命门"②先于其它，避免 bot 做完了却没东西能玩。

1. **先打通 ② Godot Web 无头导出**：拿一个现有游戏，导出 HTML5 + 本地跑通（不接 bot、不接视觉）。
2. **③ 托管 + 公开链接**：导出包能通过 URL 打开玩（仍可先用键鼠/默认输入临时验证渲染）。
3. **④ 视觉输入接入**：MediaPipe + JavaScriptBridge 喂 InputBridge，验证一个脸/手控制游戏可玩。
4. **① Bot 适配层**：文本创作/修改 → agent → 回链接 + 进度。
5. **⑤ 门禁 + ⑥ modify 重导刷新**：配额/熔断、修改原地更新链接。

---

## 9. 不在 v1 范围

- 触摸/键鼠控制（本次明确只做视觉控制）。
- Discord Activity 站内玩（本次走外部链接）。
- 文本 AI 助手 / 日历任务等通用 agent 能力（本次只包装游戏生成 agent）。
- 语音、小组件、端侧硬件特性。
