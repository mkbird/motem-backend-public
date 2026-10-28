"""Weather tool — get_weather."""

import logging

from langchain_core.tools import tool

from robot.agent.tools._context import get_user_lang
from robot.i18n import t as _t

logger = logging.getLogger(__name__)


@tool
async def get_weather(lat: float = 39.9, lon: float = 116.4) -> str:
    """获取天气信息。默认北京。"""
    lang = get_user_lang()
    logger.info("[Tool] get_weather: lat=%s lon=%s", lat, lon)
    from robot.weather.service import get_weather as fetch

    data = await fetch(lat, lon)
    if "error" in data:
        return _t(lang, "weather_fetch_fail", err=data["error"])
    return _t(
        lang,
        "weather_result",
        desc=data["description"],
        temp=data["temp"],
        feels=data["feels_like"],
        humi=data["humidity"],
    )


@tool
async def real_weather_tool(action: str, city: str) -> str:
    """城市天气查询（DashScope Weather MCP）。

    Args:
        action: current（实况）/ daily_15d（未来15天天气预报）
        city: 城市名称，如"北京市""上海""深圳"

    Returns:
        结构化 JSON 字符串（day/night 结构），供 LLM 进一步总结并用于语音播报。
    """
    import json

    lang = get_user_lang()
    action = (action or "").strip()
    city = (city or "").strip()
    if not city:
        return _t(lang, "weather_city_required")

    from robot.config import settings

    dashscope_key = settings.dashscope_api_key.strip()
    if not dashscope_key:
        return _t(lang, "weather_not_configured")

    from robot.mcp.weather.city_mapping import get_city_id, get_token

    try:
        city_id = get_city_id(city)
    except Exception as e:
        return _t(lang, "weather_city_map_fail", err=e)
    if city_id is None:
        return _t(lang, "weather_city_not_found", city=city)

    token = get_token(action)
    if not token:
        return _t(lang, "weather_unknown_action", action=action)

    def _parse_weather_response(action_: str, raw_response: str) -> str:
        json_str = (raw_response or "").split(" - ")[0].strip()
        data = json.loads(json_str)

        if data.get("code") != 0:
            return _t(lang, "weather_query_fail", msg=data.get("msg", "unknown"))

        city_data = data.get("data", {})
        city_name = city_data.get("city", {}).get("name", "未知" if not lang.startswith("en") else "Unknown")

        result: list[dict[str, str]] = []

        if action_ == "current":
            condition = city_data.get("condition", {})
            day_weather = f"{condition.get('condition', '')} {condition.get('temp', '')}°C"
            day_wind = f"{condition.get('windDir', '')}{condition.get('windLevel', '')}级"

            result.append(
                {
                    "location": city_name,
                    "date": str(condition.get("updatetime", ""))[:10],
                    "day_weather": day_weather,
                    "day_wind": day_wind,
                    "night_weather": "",
                    "night_wind": "",
                }
            )

        elif action_ == "daily_15d":
            forecast = city_data.get("forecast", [])
            for day in forecast:
                day_weather = f"{day.get('conditionDay', '')} {day.get('tempDay', '')}°C"
                day_wind = f"{day.get('windDirDay', '')}{day.get('windLevelDay', '')}级"
                night_weather = f"{day.get('conditionNight', '')} {day.get('tempNight', '')}°C"
                night_wind = f"{day.get('windDirNight', '')}{day.get('windLevelNight', '')}级"

                result.append(
                    {
                        "location": city_name,
                        "date": day.get("predictDate", ""),
                        "day_weather": day_weather,
                        "day_wind": day_wind,
                        "night_weather": night_weather,
                        "night_wind": night_wind,
                    }
                )

        return json.dumps(result, ensure_ascii=False)

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {
                "weather": {
                    "url": "https://dashscope.aliyuncs.com/api/v1/mcps/market-cmapi013828/mcp",
                    "transport": "http",
                    "headers": {"Authorization": f"Bearer {dashscope_key}"},
                }
            },
            tool_name_prefix=False,
        )

        tools = await client.get_tools()
        action_names = {"current": "天气实况", "daily_15d": "天气预报15天"}
        tool_name = action_names.get(action)
        if not tool_name:
            return _t(lang, "weather_unknown_action", action=action)

        for tool in tools:
            if tool.name == tool_name:
                result = await tool.ainvoke(
                    {"cityId": str(city_id), "token": token}
                )
                if isinstance(result, list) and len(result) > 0:
                    item = result[0]
                    if isinstance(item, dict) and "text" in item:
                        raw_text = item["text"]
                        return _parse_weather_response(action, raw_text)
                    return str(result)
                return str(result)

        return _t(lang, "weather_tool_not_found", name=tool_name)
    except Exception as e:
        return _t(lang, "weather_query_fail", msg=str(e))
