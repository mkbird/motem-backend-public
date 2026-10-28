from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings

_WEAK_SECRETS = frozenset({
    "change-me-in-production", "secret", "password", "test",
    "changeme", "your-secret-key", "jwt-secret",
})


class Settings(BaseSettings):
    # App
    app_name: str = "MotemBackend"
    debug: bool = False
    # 部署区域：cn=中国大陆，overseas=海外。影响第三方服务的优先级顺序
    region: str = "cn"

    # Database
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "robot"

    @property
    def database_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60 * 24 * 7  # 7 days — TODO: reduce to 60-240 min
    jwt_refresh_token_expire_days: int = 30

    # CORS — 逗号分隔的允许 origin 列表；生产务必设置为真实域名。
    # 示例：ROBOT_CORS_ALLOW_ORIGINS=https://app.motem.com,https://share.motem.com
    cors_allow_origins: list[str] = ["http://localhost:3000", "http://localhost:8080"]

    # DashScope API Key（LLM、ASR/TTS、天气、搜索等统一使用）
    dashscope_api_key: str = ""
    openai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_model: str = "qwen-plus"
    vision_model: str = "qwen-vl-plus"

    # Memory
    chat_history_max_rounds: int = 10

    # Timezone
    #default_timezone: str = "Asia/Shanghai"
    default_timezone: str = "America/New_York"

    # Amap (高德地图) — 逆地理编码，国内备用
    amap_api_key: str = ""

    # Anthropic（Claude Code SDK, 游戏代码生成）
    anthropic_api_key: str = ""
    anthropic_auth_token: str = ""
    anthropic_base_url: str = ""

    # Godot（godot-mcp 场景操作和项目验证）
    godot_path: str = ""  # 留空则让 godot-mcp 自动检测

    # Claude Code SDK 模型（游戏代码生成 — create_game 路径用）
    # 默认 z-ai/glm-5.1：开源 SOTA SWE-bench Verified 77.8%，长任务自主能力强。
    # 经 OpenRouter 走 sk-or-v1-* token + base_url=https://openrouter.ai/api。
    game_code_model: str = "z-ai/glm-5.1"

    # Claude Code SDK 模型（游戏修改 — modify_game 路径专用）
    # 与 create 分开：modify 是增量改动 + sliding-window compact 后 input 已
    # 控制在 ~30K tokens 内，用更快更便宜的 haiku 4.5（200K context、~$1/M
    # input、~$5/M output）即可，约比 sonnet 4.6 省 3-5×。
    # 复杂 modify（加新功能那种）haiku 也够，simple ROI 更高。
    game_modify_model: str = "anthropic/claude-haiku-4.5"

    # Claude Code SDK 模型（小组件生成）
    # 与 game 拆开是因为：widget 是单文件 HTML+JS 50-200 行的小活，
    # qwen3-coder / glm-4.7 这种便宜的代码专精模型 ROI 更高。
    # 推荐（2026-04 价格）：
    #   qwen/qwen3-coder-next ($0.14/$0.80) 2026-02 最新 sparse MoE 专精 agentic
    #   z-ai/glm-4.7-flash    ($0.06/$0.40) 2026-01 超低价
    widget_code_model: str = "qwen/qwen3-coder-next"

    # Mock 模式：True = create_game / modify_game 跳过 LLM，从 _template 起
    # 一份空脚手架秒回。用于 UI / 流程测试，避免每次烧 OpenRouter token。
    # ROBOT_MOCK_CREATION=1 / true / yes / on 都接受（pydantic 自动 coerce）。
    mock_creation: bool = False

    # Fast Path：create_game 路径前置 archetype 路由 + 确定性拼装，命中时
    # 跳过 Claude Code SDK agent loop。默认 False（灰度），生产开启用
    # ROBOT_FASTPATH_ENABLED=1。详见 docs/game-fastpath-implementation-plan.md。
    fastpath_enabled: bool = False
    # Router 置信度阈值：≥ 阈值才走 fast path，否则 fallback 现有 SDK。
    # 0.7 起步保守，留 30% 余量给"模糊匹配但拿不准"。后续按观测数据下调。
    fastpath_confidence_threshold: float = 0.7

    # Web (HTML5) 导出：游戏 create/modify 收尾后异步把工程导出成可在浏览器
    # 玩的 bundle 并发布到 web_builds/，对外经 /play/{game_id}/ serve。默认
    # False（需服务器装 Godot + Web 导出模板才有意义），开启用
    # ROBOT_WEB_EXPORT_ENABLED=1。详见 docs/discord-game-bot-v1-plan.md。
    web_export_enabled: bool = False

    # ── Bot 适配层（① v1）：文本创作/修改游戏 → 回 /play 链接 ──
    # token 只放 .env，绝不进代码 / git。默认 off。
    discord_bot_enabled: bool = False            # ROBOT_DISCORD_BOT_ENABLED=1
    discord_bot_token: str = ""                  # ROBOT_DISCORD_BOT_TOKEN
    # 仅监听这个频道（留空 = 任意频道；建议联调时填一个测试频道 id）
    discord_bot_channel_id: str = ""             # ROBOT_DISCORD_BOT_CHANNEL_ID
    # bot 以哪个后端 User.id 身份创作游戏（owner_id/user_id）。联调前先在
    # users 表建一个用户、把 id 填这里。留空则 bot 拒绝创作并提示。
    bot_owner_user_id: str = ""                  # ROBOT_BOT_OWNER_USER_ID

    # ── 创作门禁（⑤：公开开放前必做的配额 / kill-switch / 护栏）──
    # 全局总开关：False 立即停掉一切创作/修改（成本失控时的 kill-switch）。
    bot_creation_enabled: bool = True            # ROBOT_BOT_CREATION_ENABLED=0 急停
    # 每个发起者（Discord 用户）每日配额。窗口 24h，Redis 计数。
    bot_daily_create_limit: int = 20             # ROBOT_BOT_DAILY_CREATE_LIMIT
    bot_daily_modify_limit: int = 50             # ROBOT_BOT_DAILY_MODIFY_LIMIT
    # 全局同时进行的游戏生成上限（信号量）。超出排队，防资源/成本打爆。
    bot_max_concurrency: int = 3                 # ROBOT_BOT_MAX_CONCURRENCY

    # gRPC services
    grpc_asr_addr: str = "localhost:8011"
    grpc_tts_addr: str = "localhost:8012"

    # Speech - Opus
    speech_opus_enabled: bool = True

    # Speech - TTS
    speech_tts_model: str = "cosyvoice-v2"
    speech_tts_voice: str = "longxiaochun_v2"
    speech_tts_sample_rate: int = 24000

    # Speech - ASR
    speech_asr_model: str = "paraformer-realtime-v2"
    speech_asr_sample_rate: int = 16000

    # Server version (v4 protocol)
    server_version: str = "4.0.0"

    # Profile avatars. Relative paths are resolved from the backend repo root.
    avatar_dir: str = "static/avatars"

    # APNs（Apple Push Notification service）—— token-based JWT 认证。
    # 一个 .p8 同时覆盖 sandbox + production；env 由 user_devices.apns_environment
    # 决定，配 `apns_bundle_id` 作为 apns-topic。所有字段留空 → push_service
    # 走 log-only 兜底（不影响其他功能）。
    apns_key_path: str = ""        # .p8 私钥文件绝对路径
    apns_key_id: str = ""           # 10 字符 Key ID
    apns_team_id: str = ""          # 10 字符 Team ID
    apns_bundle_id: str = ""        # iOS bundle identifier，比如 com.motem.app
    apns_connect_timeout: float = 10.0

    # AI 游戏社媒分享
    # share_public_base_url: 留空则按请求 Host + X-Forwarded-Proto 推断
    # （ngrok 重启域名变化时无需改 env）
    share_public_base_url: str = ""
    # OneLink 模板 URL，形如 https://motem.onelink.me/XXXX
    # TODO: 由用户在 AppsFlyer 后台建好 OneLink 后替换
    onelink_template_url: str = "https://TODO.onelink.me/TODO"

    model_config = {
        "env_prefix": "ROBOT_",
        "env_file": Path(__file__).resolve().parent.parent / ".env",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _validate_jwt_secret(self) -> "Settings":
        # CI is not a security boundary: production and test deployments must
        # both supply a real signing key. Test fixtures inject an ephemeral key.
        key = self.jwt_secret_key
        if key in _WEAK_SECRETS or len(key) < 32:
            if not self.debug:
                raise ValueError(
                    "ROBOT_JWT_SECRET_KEY is weak or still the default value. "
                    "Generate a strong key: "
                    "python -c \"import secrets; print(secrets.token_hex(32))\""
                )
        return self


settings = Settings()
