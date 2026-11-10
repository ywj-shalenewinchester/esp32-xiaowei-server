import asyncio
import datetime
import json
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


# ============================================================================
#  下发设备用的天气报文（type == "weather"）
#
#  下位机（小未 P4）的天气页只有 Montserrat 字体，**没有中文字形**，而且它内置的
#  天气图标只有 8 张，覆盖不了和风几十个码值 —— 所以这一路报文里不放中文、不放图标，
#  只放"英文条件词 + 数字"，设备端一个 lv_label 就能显示。
# ============================================================================

# 和风天气 icon 码 -> 英文短描述（官方"天气图标"文档的码值表）。
# 没收录的码值一律给 N/A：宁可显示"不知道"，也不要显示一个错的天气。
ICON_CODE_EN = {
    100: "Sunny", 101: "Cloudy", 102: "Few Clouds", 103: "Partly Cloudy",
    104: "Overcast",
    150: "Clear", 151: "Cloudy", 152: "Few Clouds", 153: "Partly Cloudy",
    300: "Shower", 301: "Heavy Shower", 302: "Thunder", 303: "Heavy Thunder",
    304: "Hail", 305: "Light Rain", 306: "Moderate Rain", 307: "Heavy Rain",
    308: "Extreme Rain", 309: "Drizzle", 310: "Storm", 311: "Heavy Storm",
    312: "Severe Storm", 313: "Freezing Rain", 314: "Light to Moderate Rain",
    315: "Moderate to Heavy Rain", 316: "Heavy to Storm",
    317: "Storm to Heavy Storm", 318: "Heavy to Severe Storm",
    350: "Shower", 351: "Heavy Shower", 399: "Rain",
    400: "Light Snow", 401: "Moderate Snow", 402: "Heavy Snow", 403: "Snowstorm",
    404: "Sleet", 405: "Rain and Snow", 406: "Shower and Snow",
    407: "Snow Shower", 408: "Light to Moderate Snow",
    409: "Moderate to Heavy Snow", 410: "Heavy to Snowstorm",
    456: "Snow Shower", 457: "Snow Shower", 499: "Snow",
    500: "Mist", 501: "Fog", 502: "Haze", 503: "Sandstorm", 504: "Dust",
    507: "Dust Storm", 508: "Severe Sandstorm", 509: "Dense Fog",
    510: "Strong Fog", 511: "Moderate Haze", 512: "Heavy Haze",
    513: "Severe Haze", 514: "Heavy Fog", 515: "Extra Heavy Fog",
    900: "Hot", 901: "Cold", 999: "Unknown",
}

# date.weekday()：周一 = 0
WEEKDAYS_EN = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

DEVICE_DAILY_MAX = 7
DEVICE_HOURLY_MAX = 24


def _cond_en(icon) -> str:
    try:
        return ICON_CODE_EN.get(int(icon), "N/A")
    except (TypeError, ValueError):
        return "N/A"


def _int_or_none(value):
    """和风的数值字段都是字符串（"26"），转不成 int 就给 None —— 设备端会显示 "--"。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _weekday_en(date_str: str) -> str:
    """'2026-10-15' -> 'THU'。解析不了返回空串（设备端留空，不显示错的星期）。"""
    try:
        return WEEKDAYS_EN[datetime.date.fromisoformat(date_str).weekday()]
    except (TypeError, ValueError):
        return ""


def _hhmm(iso_time: str) -> str:
    """和风的时间字段长这样：'2026-10-15T14:00+08:00' —— 取中间的 HH:MM。

    直接用字符串切，不做时区换算：和风返回的就是**查询地当地时间**，
    设备也在同一个时区，再转一次反而会错。
    """
    if isinstance(iso_time, str) and len(iso_time) >= 16 and iso_time[10] == "T":
        return iso_time[11:16]
    return ""


def build_device_payload(weather: dict) -> dict:
    """把结构化天气裁成设备端要的那一份（英文 + 数字，无图标）。"""
    now = weather.get("now") or {}
    daily = []
    for day in (weather.get("daily") or [])[:DEVICE_DAILY_MAX]:
        daily.append(
            {
                "day": _weekday_en(day.get("fxDate")),
                "cond": _cond_en(day.get("iconDay")),
                "tmin": _int_or_none(day.get("tempMin")),
                "tmax": _int_or_none(day.get("tempMax")),
            }
        )
    hourly = []
    for hour in (weather.get("hourly") or [])[:DEVICE_HOURLY_MAX]:
        hourly.append(
            {
                "time": _hhmm(hour.get("fxTime")),
                "cond": _cond_en(hour.get("icon")),
                "temp": _int_or_none(hour.get("temp")),
            }
        )

    return {
        "type": "weather",
        "city": weather.get("city", ""),
        "update": _hhmm(now.get("obsTime")),
        "now": {
            "temp": _int_or_none(now.get("temp")),
            "feels": _int_or_none(now.get("feelsLike")),
            "cond": _cond_en(now.get("icon")),
            "humidity": _int_or_none(now.get("humidity")),
            "wind": _int_or_none(now.get("windSpeed")),
        },
        "daily": daily,
        "hourly": hourly,
    }


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

        # 4. 24 小时逐时（设备上滑那一页要用；文本报告用不到，但共用同一次请求更省）
        hourly = []
        hourly_resp = requests.get(
            f"{base}/v7/weather/24h",
            params={"location": location_id, "key": api_key},
            timeout=10,
        )
        if hourly_resp.status_code == 200:
            hourly_data = hourly_resp.json()
            if hourly_data.get("code") == "200":
                hourly = hourly_data.get("hourly") or []

        return {
            "city": city,
            # 下面几个扁平字段是给 format_weather_report() 和系统提示词用的（保持原样）
            "weather": now.get("text", "未知"),
            "temperature": now.get("temp", "未知"),
            "feels_like": now.get("feelsLike", ""),
            "winddirection": now.get("windDir", "未知"),
            "windpower": now.get("windScale", "未知"),
            "humidity": now.get("humidity", "未知"),
            "reporttime": now.get("obsTime", ""),
            "forecast": daily,
            # 原始结构：给设备端报文用（build_device_payload）。
            # 缓存里存的就是这一整个 dict —— 文本和报文都从它现算，只请求一次。
            "now": now,
            "daily": daily,
            "hourly": hourly,
        }

    return await asyncio.to_thread(_fetch)


async def resolve_location(conn: "ConnectionHandler", location: str = None) -> str:
    """要查哪个城市。优先用调用方给的名字，其次复用"服务器连上时预热"解析出的位置
    （prompt_manager._get_location_info 会把它写进 CacheType.LOCATION），最后才自己按 IP 查。

    先捡预热那份现成的，是为了让两次查询落到**同一个字符串**上 —— 否则同一次连接里
    会因为"广州"和"广州市"这种差别算成两个缓存键，把同一个城市拉两遍。
    """
    from core.utils.cache.manager import cache_manager, CacheType

    if location:
        return location

    client_ip = conn.client_ip
    if client_ip:
        cached_location = cache_manager.get(CacheType.LOCATION, client_ip)
        if cached_location:
            return cached_location

        cached_ip_info = cache_manager.get(CacheType.IP_INFO, client_ip)
        if cached_ip_info:
            location = cached_ip_info.get("city")
        else:
            ip_info = get_ip_info(client_ip, logger)
            if ip_info:
                cache_manager.set(CacheType.IP_INFO, client_ip, ip_info)
                location = ip_info.get("city")

    if not location:
        weather_config = conn.config.get("plugins", {}).get("get_weather", {})
        location = (
            weather_config.get("default_location")
            or weather_config.get("default_city")
            or "广州"
        )
    return location


async def get_weather_data(conn: "ConnectionHandler", location: str = None):
    """取**结构化**天气数据（带缓存），拿不到返回 None。

    缓存键只有一把（qweather_weather_{城市}），缓存值是 fetch_weather() 的原始 dict：
    给大模型的文本报告由 format_weather_report() 现算，给设备的报文由
    build_device_payload() 现算 —— 两个消费者共用一次和风请求。
    """
    from core.utils.cache.manager import cache_manager, CacheType

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    api_key = weather_config.get("api_key", "")
    api_host = weather_config.get("api_host", "")
    if not api_key or not api_host:
        logger.bind(tag=TAG).warning("天气服务未配置 API Key 或 API Host")
        return None

    resolved = await resolve_location(conn, location)
    cache_key = f"qweather_weather_{resolved}"
    cached = cache_manager.get(CacheType.WEATHER, cache_key)
    if cached:
        return cached

    data = await fetch_weather(api_key, api_host, resolved)
    if not data or data.get("auth_error"):
        return None
    cache_manager.set(CacheType.WEATHER, cache_key, data)
    return data


async def push_weather_to_device(conn: "ConnectionHandler") -> bool:
    """把天气推给设备（设备连接建立后调一次；设备上的天气页显示的就是这一份）。

    走到这里时缓存基本已经热了（服务器连上时会先给系统提示词预热一次天气），
    所以这一步通常不会再打和风接口。
    """
    data = await get_weather_data(conn, None)
    if not data:
        logger.bind(tag=TAG).warning("天气数据不可用，跳过下发设备")
        return False

    payload = build_device_payload(data)
    payload["session_id"] = conn.session_id
    await conn.websocket.send(json.dumps(payload, ensure_ascii=False))
    logger.bind(tag=TAG).info(
        f"已下发天气到设备: {payload['city']} {payload['now']['cond']} "
        f"{payload['now']['temp']}C ({len(payload['daily'])}天/{len(payload['hourly'])}小时)"
    )
    return True


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
    """大模型工具入口：把结构化天气渲染成一段中文文本交给大模型。"""
    data = await get_weather_data(conn, location)
    if not data:
        where = f"「{location}」" if location else "本地"
        return ActionResponse(
            Action.REQLLM,
            f"没查到{where}的天气信息，请确认城市名是否正确，或稍后再试。",
            None,
        )
    return ActionResponse(Action.REQLLM, format_weather_report(data), None)
