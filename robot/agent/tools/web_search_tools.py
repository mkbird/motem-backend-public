"""Web search tool for MotemBackend (DashScope WebSearch MCP)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import tool

from robot.agent.tools._context import get_user_lang
from robot.i18n import t as _t

logger = logging.getLogger(__name__)


def _format_search_results(data: dict[str, Any], lang: str) -> str:
    if data.get("status") != 0:
        return _t(lang, "search_fail")

    pages = data.get("pages", [])
    if not pages:
        return _t(lang, "search_no_result")

    formatted: list[str] = []
    for i, page in enumerate(pages[:3], 1):
        title = page.get("title", "")
        snippet = (page.get("snippet", "") or "")[:200]
        formatted.append(f"{i}. {title}\n{snippet}")
    return "\n\n".join(formatted)


def _parse_search_response(result: Any, lang: str) -> str:
    # Ported parsing style from echo_agent/earphone.
    if isinstance(result, list) and len(result) > 0:
        item = result[0]
        if isinstance(item, dict) and "text" in item:
            raw_text = item["text"]
            try:
                data = json.loads(raw_text)
                return _format_search_results(data, lang)
            except Exception:
                return raw_text

    if isinstance(result, dict):
        return _format_search_results(result, lang)

    return str(result)


@tool
async def web_search_tool(query: str) -> str:
    """联网搜索（DashScope WebSearch MCP）。

    Returns:
        适合 LLM 进一步总结的文本（最多 3 条结果，标题+snippet）。
    """
    from robot.config import settings

    lang = get_user_lang()
    logger.info("[Tool] web_search_tool query=%r", query)

    dashscope_key = settings.dashscope_api_key.strip()
    if not dashscope_key:
        return _t(lang, "search_not_configured")

    from langchain_mcp_adapters.client import MultiServerMCPClient

    try:
        client = MultiServerMCPClient(
            {
                "websearch": {
                    "url": "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/sse",
                    "transport": "sse",
                    "headers": {"Authorization": f"Bearer {dashscope_key}"},
                }
            },
            tool_name_prefix=False,
        )

        tools = await client.get_tools()

        search_tool = None
        for t in tools:
            if "search" in t.name.lower():
                search_tool = t
                break
        if not search_tool:
            return _t(lang, "search_unavail")

        result = await asyncio.wait_for(
            search_tool.ainvoke({"query": query}),
            timeout=30.0,
        )
        return _parse_search_response(result, lang)
    except asyncio.TimeoutError:
        return _t(lang, "search_timeout")
    except Exception as e:
        logger.warning("WebSearch调用失败: %s", e)
        return _t(lang, "search_error")
