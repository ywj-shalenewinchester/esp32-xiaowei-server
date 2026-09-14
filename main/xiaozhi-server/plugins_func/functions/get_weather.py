import asyncio
import requests
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.util import get_ip_info
from plugins_func.functions.qweather_location_map import LOCATION_ID_MAP
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取某个地点的天气，用户应提供一个位置，比如用户说杭州天气，参数为：杭州。"
            "如果用户说的是省份，默认用省会城市。如果用户说的不是省份或城市而是一个地名，默认用该地所在省份的省会城市。"
            "重要：本地未来天气已在上下文中提供，用户未指明其他城市时绝对不要调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地点名，例如杭州。可选参数，如果不提供则不传",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}

# 和风天气城市搜索接口（GeoAPI，兜底用）。
# 注：当前 key 在和风控制台未对该接口开放权限，会返回 403 Security Restriction，
# 因此主要依赖上面的内置 LOCATION_ID_MAP；GeoAPI 仅作未来兜底。
GEO_API_URL = "https://geoapi.qweather.com/v2/city/lookup"


def _normalize_city_name(city: str) -> str:
    """去掉行政区划后缀，用于匹配内置映射表。"""
    for suffix in ("特别行政区", "自治区", "自治州", "地区", "市", "省"):
        if city.endswith(suffix):
            return city[: -len(suffix)]
    return city


def _city_to_location_id_sync(api_key: str, city: str):
    """城市名 -> 和风 LocationID（例如 重庆 -> 101040100）。

    优先查内置映射表（覆盖全国 340 个地级市/直辖市及港澳台）；
    查不到再尝试 GeoAPI 城市搜索。
    """
    # 1. 内置映射表（精确匹配 + 去后缀匹配）
    for key in (city, _normalize_city_name(city)):
        if key in LOCATION_ID_MAP:
            return LOCATION_ID_MAP[key]

    # 2. GeoAPI 兜底（需在和风控制台对 key 开放 geo 接口权限）
    try:
        resp = requests.get(GEO_API_URL, params={
            "location": city,
            "key": api_key,
            "number": 1,
        }, timeout=10)
        if resp.status_code != 200:
            logger.bind(tag=TAG).warning(
                f"和风 GeoAPI 城市搜索失败(status={resp.status_code}): {resp.text[:120]}"
            )
            return None
        data = resp.json()
        if data.get("code") != "200":
            return None
        locations = data.get("location") or []
        return locations[0]["id"] if locations else None
    except Exception as e:
        logger.bind(tag=TAG).warning(f"和风 GeoAPI 城市搜索异常: {e}")
        return None


async def fetch_weather(api_key: str, api_host: str, city: str):
    """从和风天气 API 获取实时 + 7天预报（sync HTTP in thread pool）。

    api_host 由智控台下发，例如 kc3fc3q39k.re.qweatherapi.com
    """

    def _fetch():
        # 1. 城市名 -> LocationID（和风 /v7 接口只认编码，不认中文名）
        location_id = _city_to_location_id_sync(api_key, city)
        if not location_id:
            logger.bind(tag=TAG).error(f"未找到城市「{city}」的 LocationID")
            return None

        base = f"https://{api_host}"

        # 2. 实时天气
        live_resp = requests.get(
            f"{base}/v7/weather/now",
            params={"location": location_id, "key": api_key},
            timeout=10,
        )
        if live_resp.status_code == 401:
            logger.bind(tag=TAG).error(f"和风天气鉴权失败: {live_resp.text}")
            return {"auth_error": True}
        live_data = live_resp.json()
        if live_data.get("code") != "200":
            logger.bind(tag=TAG).error(f"和风实时天气失败: {live_data}")
            return None
        now = live_data.get("now") or {}

        # 3. 7天预报
        daily = []
        forecast_resp = requests.get(
            f"{base}/v7/weather/7d",
            params={"location": location_id, "key": api_key},
            timeout=10,
        )
        if forecast_resp.status_code == 200:
            forecast_data = forecast_resp.json()
            if forecast_data.get("code") == "200":
                daily = forecast_data.get("daily") or []

        return {
            "city": city,
            "weather": now.get("text", "未知"),
            "temperature": now.get("temp", "未知"),
            "feels_like": now.get("feelsLike", ""),
            "winddirection": now.get("windDir", "未知"),
            "windpower": now.get("windScale", "未知"),
            "humidity": now.get("humidity", "未知"),
            "reporttime": now.get("obsTime", ""),
            "forecast": daily,
        }

    return await asyncio.to_thread(_fetch)


def format_weather_report(data: dict) -> str:
    """Format weather data into a readable report."""
    report = f"查询地点：{data['city']}\n\n"
    report += f"🌤 当前天气：{data['weather']}\n"
    report += f"🌡 温度：{data['temperature']}°C\n"
    report += f"💧 湿度：{data['humidity']}%\n"
    report += f"🌬 风向：{data['winddirection']}，风力：{data['windpower']}级\n"
    report += f"🕐 更新时间：{data['reporttime']}\n"

    if data.get("forecast"):
        report += "\n📅 未来天气预报：\n"
        for day in data["forecast"]:
            report += (
                f"  {day.get('fxDate', '?')}  {day.get('textDay', '?')}"
                f"  {day.get('tempMin', '?')}°C ~ {day.get('tempMax', '?')}°C\n"
            )

    return report


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    from core.utils.cache.manager import cache_manager, CacheType

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    api_key = weather_config.get("api_key", "")
    api_host = weather_config.get("api_host", "")
    default_location = weather_config.get("default_location") or weather_config.get("default_city") or "广州"

    if not api_key:
        return ActionResponse(Action.REQLLM, "天气服务未配置 API Key，请联系管理员。", None)
    if not api_host:
        return ActionResponse(Action.REQLLM, "天气服务未配置 API Host，请联系管理员。", None)

    if not location:
        client_ip = conn.client_ip
        if client_ip:
            cached_ip_info = cache_manager.get(CacheType.IP_INFO, client_ip)
            if cached_ip_info:
                location = cached_ip_info.get("city")
            else:
                ip_info = get_ip_info(client_ip, logger)
                if ip_info:
                    cache_manager.set(CacheType.IP_INFO, client_ip, ip_info)
                    location = ip_info.get("city")
        if not location:
            location = default_location

    # Check cache
    weather_cache_key = f"qweather_weather_{location}"
    cached = cache_manager.get(CacheType.WEATHER, weather_cache_key)
    if cached:
        return ActionResponse(Action.REQLLM, cached, None)

    data = await fetch_weather(api_key, api_host, location)
    if not data:
        return ActionResponse(Action.REQLLM, f"未找到「{location}」的天气信息，请确认城市名称是否正确", None)
    if data.get("auth_error"):
        return ActionResponse(
            Action.REQLLM,
            "天气服务鉴权失败，请检查智控台天气插件配置的 API Key 与 API Host 是否正确",
            None,
        )

    report = format_weather_report(data)
    cache_manager.set(CacheType.WEATHER, weather_cache_key, report)
    return ActionResponse(Action.REQLLM, report, None)
