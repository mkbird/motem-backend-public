import json
import logging

import httpx

from robot.config import settings
from robot.db.redis import redis_client

logger = logging.getLogger(__name__)

CACHE_TTL = 1800  # 30 minutes


async def _reverse_geocode_amap(lat: float, lon: float) -> str | None:
    """Convert lat/lon to city name via 高德地图 (primary, China-accessible)."""
    amap_key = settings.amap_api_key.strip()
    if not amap_key:
        return None
    # 高德使用 GCJ-02 坐标系，GPS 是 WGS-84，需转换
    # 简单近似：误差约 100-500m，对城市级别定位足够
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {
        "key": amap_key,
        "location": f"{lon:.6f},{lat:.6f}",
        "radius": 1000,
        "extensions": "base",
        "output": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") == "1":
            addr = data.get("regeocode", {}).get("addressComponent", {})
            city = addr.get("city") or addr.get("district") or addr.get("province")
            # 高德返回省直辖市时 city 为 []
            if isinstance(city, list):
                city = addr.get("province")
            logger.info("[Weather] amap reverse geocode lat=%.3f lon=%.3f → %s", lat, lon, city)
            return city
    except Exception as e:
        logger.error("[Weather] amap reverse geocode failed: %s", e)
    return None


async def _reverse_geocode_nominatim(lat: float, lon: float) -> str | None:
    """Convert lat/lon to city name via OSM Nominatim (fallback)."""
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": 10, "accept-language": "zh-CN"}
    headers = {"User-Agent": "motem-weather/1.0"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        addr = data.get("address", {})
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("county")
            or addr.get("state")
        )
        logger.info("[Weather] nominatim reverse geocode lat=%.3f lon=%.3f → %s", lat, lon, city)
        return city
    except Exception as e:
        logger.error("[Weather] nominatim reverse geocode failed: %s", e)
    return None


async def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Convert lat/lon to city name. Order depends on settings.region."""
    if settings.region == "cn":
        city = await _reverse_geocode_amap(lat, lon)
        if city:
            return city
        logger.warning("[Weather] amap failed, falling back to nominatim")
        return await _reverse_geocode_nominatim(lat, lon)
    else:
        city = await _reverse_geocode_nominatim(lat, lon)
        if city:
            return city
        logger.warning("[Weather] nominatim failed, falling back to amap")
        return await _reverse_geocode_amap(lat, lon)


async def _fetch_dashscope(city: str) -> dict | None:
    """Fetch current weather from DashScope MCP for a city name."""
    from robot.mcp.weather.city_mapping import get_city_id, get_token

    city_id = get_city_id(city)
    if city_id is None:
        logger.warning("[Weather] city not found in mapping: %s", city)
        return None

    token = get_token("current")
    dashscope_key = settings.dashscope_api_key.strip()
    if not dashscope_key:
        logger.error("[Weather] DashScope key not configured")
        return None

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
        for tool in tools:
            if tool.name == "天气实况":
                result = await tool.ainvoke({"cityId": str(city_id), "token": token})
                if isinstance(result, list) and result:
                    raw = result[0].get("text", "") if isinstance(result[0], dict) else str(result[0])
                    json_str = raw.split(" - ")[0].strip()
                    data = json.loads(json_str)
                    if data.get("code") != 0:
                        logger.error("[Weather] DashScope error: %s", data.get("msg"))
                        return None
                    cond = data.get("data", {}).get("condition", {})
                    return {
                        "temp": float(cond.get("temp", 0)),
                        "feels_like": float(cond.get("somatosensory", cond.get("temp", 0))),
                        "humidity": int(cond.get("humidity", 0)),
                        "description": cond.get("condition", ""),
                        "icon": _map_condition_to_icon(cond.get("condition", "")),
                        "city": city,
                    }
        logger.error("[Weather] 天气实况 tool not found")
        return None
    except Exception as e:
        logger.error("[Weather] DashScope fetch failed: %s", e)
        return None


def _map_condition_to_icon(condition: str) -> str:
    """Map Chinese weather condition to OpenWeatherMap-style icon code for client mapWeatherType()."""
    c = condition
    if any(k in c for k in ["雷", "雷暴", "雷电"]):
        return "11d"
    if any(k in c for k in ["暴雪", "大雪", "雪暴"]):
        return "13d"  # will map to BLIZZARD/SNOW
    if any(k in c for k in ["雨夹雪", "冻雨", "雨雪"]):
        return "13d"
    if any(k in c for k in ["小雪", "中雪", "雪"]):
        return "13d"
    if any(k in c for k in ["大雨", "暴雨", "特大暴雨", "大暴雨"]):
        return "10d_heavy"  # special marker → H_RAIN
    if any(k in c for k in ["小雨", "中雨", "毛毛雨", "细雨"]):
        return "10d"
    if any(k in c for k in ["阵雨", "骤雨"]):
        return "09d"
    if "雾" in c or "霾" in c:
        return "50d"
    if any(k in c for k in ["阴", "多云转阴"]):
        return "04d"
    if any(k in c for k in ["多云"]):
        return "03d"
    if any(k in c for k in ["少云", "晴间多云"]):
        return "02d"
    if any(k in c for k in ["晴", "大晴"]):
        return "01d"
    return "01d"


async def _city_from_ip_taobao(ip: str) -> str | None:
    """Resolve city from IP via 淘宝 IP 库 (primary, China-accessible, no key required)."""
    try:
        url = f"https://ip.taobao.com/outGetIpInfo?ip={ip or 'myip'}&accessKey=alibaba-inc"
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") == 0:
            d = data.get("data", {})
            city = d.get("city") or d.get("region")
            logger.info("[Weather] taobao IP %s → %s", ip or "self", city)
            return city
    except Exception as e:
        logger.error("[Weather] taobao IP geolocation failed: %s", e)
    return None


async def _city_from_ip_ipapi(ip: str) -> str | None:
    """Resolve city from IP via ip-api.com (fallback, may need VPN in China)."""
    try:
        url = f"http://ip-api.com/json/{ip}"
        params = {"lang": "zh-CN", "fields": "status,city,regionName"}
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") == "success":
            city = data.get("city") or data.get("regionName")
            logger.info("[Weather] ip-api IP %s → %s", ip or "self", city)
            return city
    except Exception as e:
        logger.error("[Weather] ip-api geolocation failed: %s", e)
    return None


async def _city_from_ip(client_ip: str) -> str | None:
    """Resolve city from client IP. Order depends on settings.region."""
    ip = client_ip.split(",")[0].strip()
    if ip in ("127.0.0.1", "::1", "localhost"):
        ip = ""
    if settings.region == "cn":
        city = await _city_from_ip_taobao(ip)
        if city:
            return city
        logger.warning("[Weather] taobao IP failed, falling back to ip-api.com")
        return await _city_from_ip_ipapi(ip)
    else:
        city = await _city_from_ip_ipapi(ip)
        if city:
            return city
        logger.warning("[Weather] ip-api failed, falling back to taobao")
        return await _city_from_ip_taobao(ip)


async def get_weather(lat: float | None, lon: float | None, client_ip: str = "") -> dict:
    if lat is not None and lon is not None:
        cache_key = f"weather:cache:{lat:.2f}:{lon:.2f}"
    else:
        cache_key = f"weather:cache:ip:{client_ip.split(',')[0].strip()}"

    cached = await redis_client.get(cache_key)
    if cached:
        logger.info("[Weather] cache hit: %s", cache_key)
        return json.loads(cached)

    # Step 1: city name — from GPS coords or IP fallback
    if lat is not None and lon is not None:
        city = await _reverse_geocode(lat, lon)
        if not city:
            # Nominatim 不可达时降级用 IP 定位
            logger.warning("[Weather] Nominatim failed, falling back to IP geolocation")
            city = await _city_from_ip(client_ip)
    else:
        city = await _city_from_ip(client_ip)
    if not city:
        from robot.i18n import t as _t
        return {"error": _t("zh-Hans", "location_fail")}

    # Step 2: fetch weather from DashScope
    result = await _fetch_dashscope(city)
    if not result:
        from robot.i18n import t as _t
        return {"error": _t("zh-Hans", "weather_data_fail", city=city)}

    await redis_client.set(cache_key, json.dumps(result, ensure_ascii=False), ex=CACHE_TTL)
    logger.info("[Weather] fetched and cached: city=%s temp=%s", city, result.get("temp"))
    return result
