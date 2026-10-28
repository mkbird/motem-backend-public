"""Fast Path LLM 调用抽象 — 给 Router / Customizer 共用。

把"打开 AsyncOpenAI 客户端 + 单轮 chat.completions + JSON 解析 + 错误兜底"
抽出来一份，让 Router / Customizer 只关心 prompt 和 schema。

为什么不直接复用 streaming.py::_extract_game_meta 的内联代码：
- _extract_game_meta 把 LLM 调用、template 注册表选择、fallback 默认值、
  meta 字段清洗全混在一个函数里（150+ 行），无法精确单测
- Fast Path 要做依赖注入支持 mock 测试
- LLMResponse 结构化返回 tokens / cache_read 用于 observability
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """LLM 单轮调用的结构化结果。"""

    parsed: dict[str, Any]                       # JSON 解析后的 dict
    raw_text: str                                # 原始 LLM 输出（便于 debug）
    tokens_in: int                               # prompt tokens（含 cached）
    tokens_out: int                              # completion tokens
    cache_read_input_tokens: int                 # OpenRouter / Anthropic 标注的 cache hit
    model: str
    duration_ms: int


class LLMCallError(Exception):
    """LLM 调用 / JSON 解析失败。caller 应 fallback。"""


class JSONChatClient(Protocol):
    """单轮 JSON 输出 chat 调用的最小接口 — 便于测试 mock。"""

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.4,
    ) -> LLMResponse: ...


class OpenAIChatClient:
    """生产用客户端 — 走 settings.openai_base_url（dashscope-compatible）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = default_model
        # OpenRouter 专用：provider routing / privacy / etc. 透传到 request body
        self._extra_body = extra_body or {}

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.4,
        cache_system: bool = True,
    ) -> LLMResponse:
        # 延迟 import 避免测试时硬依赖
        from openai import AsyncOpenAI

        if not self._api_key:
            raise LLMCallError("api_key is empty; cannot call LLM")

        # 显式 timeout：OpenAI SDK 默认 600s 太长；Fast Path 单次调用应在
        # 30-60s 内完成，否则用户体验远不如 slow path 起步快。
        client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, timeout=60.0)
        chosen_model = model or self._default_model

        # Anthropic prompt caching：把 system 消息标记为 ephemeral cache。
        # OpenRouter 的 OpenAI-compat 层接受 content 是 array of parts 的形式，
        # part 上挂 `cache_control` 后会透传给 Anthropic API。要求 system 至少
        # ~1024 tokens 才会真正写 cache（Anthropic 硬规则）；否则该标记被忽略
        # 但不报错。Router 的 ~340 tokens system 不会触发，Customizer 的
        # ~2400 tokens 会。
        if cache_system:
            system_content: Any = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_content = system

        t0 = time.monotonic()
        try:
            resp = await client.chat.completions.create(
                model=chosen_model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                extra_body=self._extra_body or None,
            )
        except Exception as e:
            raise LLMCallError(f"LLM call failed: {e}") from e
        duration_ms = int((time.monotonic() - t0) * 1000)

        raw = (resp.choices[0].message.content or "").strip()
        parsed = _extract_json(raw)

        # OpenAI-compat usage 字段
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        # cache 字段：Anthropic 经过 OpenAI 兼容层时，可能在 prompt_tokens_details
        # 里有 cached_tokens；先尝试拿，没有就 0
        cache_read = 0
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_read = int(getattr(details, "cached_tokens", 0) or 0)

        return LLMResponse(
            parsed=parsed,
            raw_text=raw,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_input_tokens=cache_read,
            model=chosen_model,
            duration_ms=duration_ms,
        )


# ─── helpers ─────────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict[str, Any]:
    """容忍 markdown 围栏 / 前后噪声，提取第一个完整 JSON 对象。"""
    if not raw:
        raise LLMCallError("LLM returned empty response")
    text = raw
    # 去掉 markdown 围栏
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    # 截 {...}
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        raise LLMCallError(f"no JSON object in LLM response: {raw[:200]!r}")
    fragment = text[first:last + 1]
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError as e:
        raise LLMCallError(f"invalid JSON: {e}; raw={raw[:200]!r}") from e
    if not isinstance(parsed, dict):
        raise LLMCallError(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


class AnthropicMessagesClient:
    """Anthropic 原生 `/v1/messages` 端点（用 anthropic SDK）。

    **TODO Phase 2+ BYOK 路径**：当前 fast path 默认走 `OpenAIChatClient` +
    OpenRouter（Haiku 4.5 + Bedrock 默认）。实测我们 OpenRouter 账户拿不到
    prompt caching（账户级限制）。要拿 cache 必须申请 sk-ant-* key 直连
    anthropic.com——此 client 就是为那条路径备的。当前生产链路 (`streaming.py
    ::_try_fastpath_create`) 还没接它，等团队拿到 Anthropic 直营 key 再切。


    主要面向 BYOK 直连 anthropic.com（sk-ant-* key）的场景，那里 prompt
    caching 工作正常。**经 OpenRouter 转发时实测拿不到 cache**——OpenRouter
    在账户级对非 BYOK 流量关闭了 prompt caching，无论 client 怎么伪装请求
    协议都没用。该客户端保留是为方便未来切到 BYOK 时直接复用。

    Auth：sk-ant-* 走 `api_key`（x-api-key header），sk-or-v1-* 走
    `auth_token`（Authorization Bearer header）。
    """

    def __init__(
        self,
        *,
        auth_token: str = "",
        api_key: str = "",
        base_url: str,
        default_model: str,
    ) -> None:
        self._auth_token = auth_token
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = default_model

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.4,
        cache_system: bool = True,
    ) -> LLMResponse:
        from anthropic import AsyncAnthropic

        if not (self._auth_token or self._api_key):
            raise LLMCallError("anthropic auth_token / api_key both empty")

        kwargs: dict[str, Any] = {"base_url": self._base_url, "timeout": 60.0}
        if self._auth_token:
            kwargs["auth_token"] = self._auth_token
        if self._api_key:
            kwargs["api_key"] = self._api_key
        client = AsyncAnthropic(**kwargs)
        chosen_model = model or self._default_model

        if cache_system:
            system_param: Any = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_param = system

        messages = [{"role": "user", "content": user}]

        t0 = time.monotonic()
        try:
            resp = await client.messages.create(
                model=chosen_model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_param,
                messages=messages,
            )
        except Exception as e:
            raise LLMCallError(f"Anthropic call failed: {e}") from e
        duration_ms = int((time.monotonic() - t0) * 1000)

        text_block = ""
        for block in resp.content or []:
            if getattr(block, "type", "") == "text":
                text_block = getattr(block, "text", "")
                break
        parsed = _extract_json(text_block)

        usage = resp.usage
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        return LLMResponse(
            parsed=parsed,
            raw_text=text_block,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_input_tokens=cache_read,
            model=resp.model,
            duration_ms=duration_ms,
        )


def make_default_client() -> OpenAIChatClient:
    """从 settings 构造默认生产客户端（dashscope-compatible / qwen）。"""
    from robot.config import settings
    return OpenAIChatClient(
        api_key=settings.dashscope_api_key,
        base_url=settings.openai_base_url,
        default_model=settings.default_model,
    )


def make_openrouter_client(model: str = "anthropic/claude-haiku-4.5") -> OpenAIChatClient:
    """构造 OpenRouter OpenAI-compat 客户端（`/v1/chat/completions`）。

    - base_url 走 `settings.anthropic_base_url`（proxy 或直 OpenRouter），
      OpenAI SDK 要求 `/v1` 后缀，本函数自动追加
    - Auth 复用 `settings.anthropic_auth_token`（OpenRouter sk-or-v1-* token）
    - **不强制 provider routing**：让 OpenRouter 默认按其策略选 provider
      （Haiku 通常落到 Bedrock）。实测对我们账户而言强制 Anthropic 直连
      并不能拿到 prompt caching（OpenRouter 在账户级关闭了对非 BYOK
      流量的 cache），所以不再为此付额外的路由约束代价。
    """
    from robot.config import settings

    raw_base = (settings.anthropic_base_url or "").rstrip("/")
    if not raw_base:
        base_url = "https://openrouter.ai/api/v1"
    elif raw_base.endswith("/v1"):
        base_url = raw_base
    else:
        base_url = raw_base + "/v1"

    api_key = settings.anthropic_auth_token or settings.anthropic_api_key
    return OpenAIChatClient(
        api_key=api_key,
        base_url=base_url,
        default_model=model,
    )


def make_anthropic_native_client(model: str = "claude-haiku-4-5") -> AnthropicMessagesClient:
    """BYOK 直连 anthropic.com（sk-ant-* key）。**保留 cache_control 工作的唯一路径**。

    需要 `settings.anthropic_api_key`（sk-ant-* 形式）。OpenRouter 转发的
    Anthropic API 拿不到 cache（账户级限制），所以本 helper 强制走 anthropic.com。
    模型名用 Anthropic 官方格式（`claude-haiku-4-5`），不是 OpenRouter 的
    provider/model 形式。
    """
    from robot.config import settings

    if not settings.anthropic_api_key:
        raise LLMCallError(
            "settings.anthropic_api_key (sk-ant-*) is empty; cannot direct-connect to anthropic.com"
        )
    return AnthropicMessagesClient(
        api_key=settings.anthropic_api_key,
        base_url="https://api.anthropic.com",
        default_model=model,
    )
