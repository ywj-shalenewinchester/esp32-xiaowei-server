"""AI 绘画图片生成器

职责：调用 SiliconFlow（Kolors）文生图接口，把结果处理成**设备屏幕尺寸**
的彩色 JPEG，通过 WebSocket 发给设备显示。

目标屏幕由设备在 hello.features.display 里上报（见 core/utils/screen_info.py），
未上报时按 resolve_screen() 的兜底链取配置或内置默认值。
"""

import os
import re
import math

import requests
from io import BytesIO
from PIL import Image, ImageDraw

from core.utils.screen_info import (
    BUILTIN_DEFAULT_SCREEN,
    VALID_SOURCES,
    normalize_screen,
)

# 生图提示词：彩色插画。{round_hint} 会提示模型别把主体放到四角 ——
# 圆屏四角会被圆形遮罩裁掉，这是圆屏上收益最大的一处提示词改动。
COLOR_PROMPT_TEMPLATE = (
    "vivid colorful illustration of {item}, "
    "cute cartoon style, clean bold outlines, bright saturated colors, "
    "soft shading, centered composition, "
    "the subject stays well inside the central area{round_hint}, "
    "simple soft background, "
    "no text, no letters, no words, no watermark, no signature, no logo, "
    "no frame, no border, high quality, polished illustration"
)
ROUND_HINT = ", nothing important in the corners because the display is circular"

# Kolors 官方推荐尺寸（1024x1024 / 960x1280 / 768x1024 / 720x1440 / 720x1280）。
# 旧代码用的 512x512 并不在推荐列表里，属未文档化用法。
SUPPORTED_API_SIZES = ((1024, 1024), (960, 1280), (768, 1024), (720, 1440), (720, 1280))

# 默认配置（可被 config.yaml 的 plugins.draw_image 覆盖）
#
# 注意：这里**故意不放** "default_screen"。resolve_screen() 的兜底链靠
# “配置里有没有这个键”来判优先级，一旦写进 DEFAULT_CONFIG，合并之后
# 配置里的 default_screen 就永远赢不过它，整条链的第 2 级会静默失效。
DEFAULT_CONFIG = {
    "api_key": "",
    "model": "Kwai-Kolors/Kolors",
    "api_url": "https://api.siliconflow.cn/v1/images/generations",
    "timeout": 45,
    "download_timeout": 30,
    "num_inference_steps": 20,
    "output_dir": "./sd_images_test",
    "save_preview": True,
    "jpeg_quality": 85,
    "jpeg_optimize": True,
    # ESP32 上的 JPEG 解码器一般只解 baseline，progressive 解不了，别改
    "jpeg_progressive": False,
    "bg_color": "#000000",  # 圆屏四角填充色（JPEG 没有透明通道）
    "supersample": 4,  # 圆形遮罩抗锯齿超采样倍数
    "api_size": "",  # 留空 = 按目标尺寸自动选
    "max_message_bytes": 512 * 1024,  # 超过则自动降质重编码
}

_API_SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _resolve_api_key(cfg: dict) -> str:
    """优先从环境变量读取 API Key，找不到再回退到配置。"""
    return os.environ.get("SILICONFLOW_API_KEY") or cfg.get("api_key", "")


def _parse_color(value, default=(0, 0, 0)):
    """把 "#000000" / [0,0,0] / (0,0,0) 解析成 RGB 三元组。"""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(c))) for c in value[:3])
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6:
            try:
                return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return default
    return default


def _safe_filename(name: str) -> str:
    """把 LLM 给的绘画对象清洗成安全的文件名片段。

    item 来自大模型的函数调用参数，直接拼进路径会被 `..\\..\\config`
    这类输入穿越到 output_dir 之外。
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", (name or "").strip()).strip(" .")
    cleaned = cleaned[:64].strip(" .")
    return cleaned or "drawing"


def resolve_screen(cfg: dict, device_screen=None) -> dict:
    """确定本次出图的目标屏幕。

    优先级：设备上报 > 配置 default_screen > 内置默认（466 圆屏彩色）。

    幂等：传入本函数之前返回的结果（带 source 键）会原样返回，
    所以可以在插件里先解析一次做日志，再把结果透传给 generate_drawing。
    """
    if isinstance(device_screen, dict) and device_screen.get("source") in VALID_SOURCES:
        return device_screen

    screen = normalize_screen(device_screen)
    if screen:
        return screen

    merged = {**DEFAULT_CONFIG, **(cfg or {})}
    default_screen = merged.get("default_screen")
    if isinstance(default_screen, dict):
        screen = normalize_screen(default_screen)
        if screen:
            screen["source"] = "config"
            return screen

    return dict(BUILTIN_DEFAULT_SCREEN, source="builtin")


def _pick_api_size(width: int, height: int, cfg: dict) -> str:
    """选一个生图接口支持的尺寸，之后再由 _fit_to_target 缩放到目标。"""
    override = cfg.get("api_size")
    if isinstance(override, str) and _API_SIZE_RE.match(override.strip()):
        return override.strip()

    if abs(width - height) <= 1:
        # 466/560 这类方形目标都走这里：生大图再降采样，画质更好
        return "1024x1024"

    target = width / height
    best = min(
        SUPPORTED_API_SIZES,
        key=lambda size: abs(math.log((size[0] / size[1]) / target)),
    )
    return f"{best[0]}x{best[1]}"


def _build_prompt(item: str, shape: str) -> str:
    hint = ROUND_HINT if shape == "round" else ""
    return COLOR_PROMPT_TEMPLATE.format(item=item, round_hint=hint)


def _call_image_api(prompt: str, api_size: str, cfg: dict) -> Image.Image:
    """生图 API 的唯一网络出口（测试时可以整个替换掉）。"""
    api_key = _resolve_api_key(cfg)
    if not api_key:
        raise ValueError(
            "缺少 SiliconFlow API Key，请在 config.yaml 的 plugins.draw_image.api_key 配置，"
            "或设置 SILICONFLOW_API_KEY 环境变量"
        )

    resp = requests.post(
        cfg["api_url"],
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.get("model", "Kwai-Kolors/Kolors"),
            "prompt": prompt,
            "image_size": api_size,
            "num_inference_steps": cfg.get("num_inference_steps", 20),
            "batch_size": 1,
        },
        timeout=cfg.get("timeout", 45),
    )

    if resp.status_code != 200:
        raise RuntimeError(f"生图 API 报错 [{resp.status_code}]: {resp.text[:200]}")

    img_url = resp.json()["data"][0]["url"]
    img_data = requests.get(
        img_url, timeout=cfg.get("download_timeout", 30)
    ).content
    return Image.open(BytesIO(img_data)).convert("RGB")


def _fit_to_target(img: Image.Image, width: int, height: int) -> Image.Image:
    """等比缩放到刚好覆盖目标尺寸，再居中裁剪（不变形）。"""
    if img.size == (width, height):
        return img

    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))

    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def make_round_mask(width: int, height: int, supersample: int = 4) -> Image.Image:
    """生成圆形蒙版（L 模式，圆内 255 / 圆外 0）。

    先按 supersample 倍画大圆再 LANCZOS 缩小，边缘才有中间的 alpha 过渡；
    直接画 ellipse 会得到一条硬台阶，圆屏上看得出来。
    """
    ss = max(1, int(supersample or 1))
    # 超采样缓冲区是平方增长的，4096² 的 L 图已经 16MB，到此为止
    while ss > 1 and max(width, height) * ss > 4096:
        ss -= 1

    if ss == 1:
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, width - 1, height - 1), fill=255)
        return mask

    big = Image.new("L", (width * ss, height * ss), 0)
    ImageDraw.Draw(big).ellipse((0, 0, width * ss - 1, height * ss - 1), fill=255)
    return big.resize((width, height), Image.Resampling.LANCZOS)


def compose_round(img: Image.Image, bg_color=(0, 0, 0), supersample: int = 4):
    """把图套进圆形遮罩，四角填成 bg_color（JPEG 没有透明通道）。"""
    width, height = img.size
    canvas = Image.new("RGB", (width, height), bg_color)
    canvas.paste(img.convert("RGB"), (0, 0), make_round_mask(width, height, supersample))
    return canvas


def encode_jpeg(
    img: Image.Image, quality: int = 85, optimize: bool = True, progressive: bool = False
) -> bytes:
    """编码成 JPEG bytes。

    不要传 subsampling：PIL 在 quality<95 时默认 4:2:0，改成 4:4:4 体积要大 19%。
    """
    buf = BytesIO()
    img.convert("RGB").save(
        buf,
        format="JPEG",
        quality=int(quality),
        optimize=bool(optimize),
        progressive=bool(progressive),
    )
    return buf.getvalue()


def _encode_jpeg_with_budget(img: Image.Image, cfg: dict) -> bytes:
    """编码 JPEG，超出 max_message_bytes 时逐级降质重编码。

    466x466 q85 约 80KB（base64 后 ~105KB），颜色丰富的插画会到 250KB 左右。
    设备端 websocket 收包缓冲有限，超了就分片，设备解不出来，所以这里兜一道。
    """
    data = encode_jpeg(
        img,
        cfg.get("jpeg_quality", 85),
        cfg.get("jpeg_optimize", True),
        cfg.get("jpeg_progressive", False),
    )

    budget = int(cfg.get("max_message_bytes") or 0)
    if not budget:
        return data

    for quality in (78, 68, 58):
        # base64 膨胀 4/3，再给 JSON 外壳留点余量
        if len(data) * 4 // 3 + 512 <= budget:
            break
        data = encode_jpeg(img, quality, True, False)
    return data


def generate_drawing(item_name: str, cfg: dict = None, screen: dict = None) -> dict:
    """生成 {item_name} 的图片，返回可直接发给设备的字节。

    screen 为 resolve_screen() 的结果（或设备上报的原始 dict）；
    传 None 时按配置/内置默认值出图。

    返回:
      {
        "image_bytes":  bytes,   # JPEG，可直接进 WebSocket
        "width":        int,
        "height":       int,
        "round":        bool,    # 面板是否为圆形
        "preview_path": str | None,  # 落盘的 .jpg，供人工排查
      }

    失败时抛出异常（由调用方捕获并转换为对用户的提示）。
    """
    config = {**DEFAULT_CONFIG, **(cfg or {})}
    screen = resolve_screen(config, screen)

    item_name = (item_name or "").strip()
    if not item_name:
        raise ValueError("绘画对象不能为空")

    width, height = screen["width"], screen["height"]
    is_round = screen["shape"] == "round"

    prompt = _build_prompt(item_name, screen["shape"])
    img = _call_image_api(prompt, _pick_api_size(width, height, config), config)

    img = _fit_to_target(img, width, height)
    if is_round:
        img = compose_round(
            img,
            _parse_color(config.get("bg_color", "#000000")),
            config.get("supersample", 4),
        )

    image_bytes = _encode_jpeg_with_budget(img, config)

    preview_path = None
    if config.get("save_preview", True):
        output_dir = config.get("output_dir", "./sd_images_test")
        os.makedirs(output_dir, exist_ok=True)
        preview_path = os.path.join(output_dir, _safe_filename(item_name) + ".jpg")
        img.save(preview_path, format="JPEG", quality=config.get("jpeg_quality", 85))

    return {
        "image_bytes": image_bytes,
        "width": width,
        "height": height,
        "round": is_round,
        "preview_path": preview_path,
    }
