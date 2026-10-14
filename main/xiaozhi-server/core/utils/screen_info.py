"""设备屏幕信息解析：把 hello.features.display 归一化成统一结构。

设计要点：
  * 纯函数、无 IO、无 config 依赖，任何非法输入都不抛异常，只回退到默认值；
  * 单独成模块是为了能在不启动服务、不加载 opuslib/numpy 的情况下单测
    （core/utils/util.py 会在 import 时拉起一堆重依赖，不适合放这里）。

设备上报格式（hello.features.display）：
    {"width": 466, "height": 466, "shape": "round", "color": true}
"""

import math

# 单边小于这个值视为设备上报错误，回退到默认值
MIN_SIDE = 64
# 单边上限：466/560 都够用，再大只是白白浪费带宽和设备收包缓冲
MAX_SIDE = 1024
# 长短边比超过这个值视为上报错误，整条忽略
MAX_ASPECT = 4.0

VALID_SHAPES = ("round", "rect")
VALID_SOURCES = ("device", "config", "builtin")

# 内置兜底：设备未上报、配置也没给 default_screen 时使用
BUILTIN_DEFAULT_SCREEN = {"width": 466, "height": 466, "shape": "round"}


def _to_int(value):
    """宽松转 int，转不了返回 None。

    注意 bool 是 int 的子类，True/False 必须先排除，否则会被当成 1/0。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _clamp_side(value, default):
    """把单边长度夹到 [MIN_SIDE, MAX_SIDE]；非法或过小则用 default。"""
    parsed = _to_int(value)
    if parsed is None or parsed < MIN_SIDE:
        return default
    return min(parsed, MAX_SIDE)


def normalize_screen(raw, default=None):
    """校验并归一化一段屏幕描述。

    返回 {"width": int, "height": int, "shape": str, "source": "device"}，
    无法解析时返回 None（调用方据此回退到配置/内置默认值）。

    绝不抛异常。
    """
    if not isinstance(raw, dict):
        return None
    # 只带 color/shape 不带尺寸的 display 没有意义，当作未上报
    if "width" not in raw and "height" not in raw:
        return None

    fallback = {**BUILTIN_DEFAULT_SCREEN, **(default or {})}
    width = _clamp_side(raw.get("width"), fallback["width"])
    height = _clamp_side(raw.get("height"), fallback["height"])

    if max(width, height) / min(width, height) > MAX_ASPECT:
        return None

    shape = raw.get("shape")
    if isinstance(shape, str) and shape.strip().lower() in VALID_SHAPES:
        shape = shape.strip().lower()
    else:
        # 缺省推断：方形按圆屏（466/560 这类面板），非方形按矩形
        shape = "round" if width == height else "rect"

    return {"width": width, "height": height, "shape": shape, "source": "device"}


def declares_monochrome(raw):
    """设备是否声明自己是黑白屏（color: false）。

    墨水屏链路已移除，这个标志不再影响出图，仅用于在日志里提示设备固件过期。
    """
    if not isinstance(raw, dict):
        return False
    value = raw.get("color", True)
    if value is True:
        return False
    return str(value).strip().lower() in ("false", "0", "no")


def describe_screen(screen):
    """给日志用的一行描述。"""
    if not screen:
        return "未上报(使用服务端默认)"
    return (
        f"{screen['width']}x{screen['height']} {screen['shape']} "
        f"color({screen.get('source', 'unknown')})"
    )
