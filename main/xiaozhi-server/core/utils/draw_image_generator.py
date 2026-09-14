"""墨水屏 AI 绘画图片生成器（由 tu_ai.py 重构而来）

职责：调用 SiliconFlow（Kolors）文生图接口，把结果处理成 ESP32 墨水屏
200×200 的 1-bit 裸位图（.bin），并同时导出电脑可预览的 .bmp。

生成的 .bin 与 09_LVGL_V8_Test 的 epaper_driver_bsp 的 EPD_DisplayBitmap
完全一致：
  - 每行 200 像素 = 25 字节，共 25 * 200 = 5000 字节
  - MSB 高位在前（bit7 = 该字节最左像素）
  - 1 = 白，0 = 黑
"""

import os

import requests
from io import BytesIO
from PIL import Image, ImageEnhance

# 生图提示词：要求 1-bit 纯黑白线稿，便于二值化后上墨水屏
PROMPT_TEMPLATE = (
    "1-bit pure black and white line art of {item}, "
    "minimalist vector icon style, clean smooth outlines, "
    "solid white background, no border, no frame, "
    "no gray, no shading, no gradient, no texture, "
    "high contrast, centered composition, professional icon design"
)

# 默认配置（可被 config.yaml 的 plugins.draw_image 覆盖）
DEFAULT_CONFIG = {
    "api_key": "",
    "model": "Kwai-Kolors/Kolors",
    "size": (200, 200),
    "contrast_factor": 3.0,
    "threshold": 160,
    "output_dir": "./sd_images_test",
    "api_url": "https://api.siliconflow.cn/v1/images/generations",
    "timeout": 30,
}


def _resolve_api_key(cfg: dict) -> str:
    """优先从环境变量读取 API Key，找不到再回退到配置。"""
    return os.environ.get("SILICONFLOW_API_KEY") or cfg.get("api_key", "")


def convert_to_epaper_raw_bin(img_1bit) -> bytes:
    """将 PIL 的 1-bit ('1') 图像打包为墨水屏原生位图 bytes（无文件头、无调色板）。"""
    width, height = img_1bit.size

    pixel_bytes = bytearray()
    for y in range(height):
        for byte_x in range(0, width, 8):
            byte_val = 0
            for bit in range(8):
                x = byte_x + bit
                if x < width:
                    pixel = img_1bit.getpixel((x, y))
                    if pixel > 0:  # 白色
                        byte_val |= (1 << (7 - bit))
            pixel_bytes.append(byte_val)

    return bytes(pixel_bytes)


def generate_drawing(item_name: str, cfg: dict = None) -> dict:
    """生成 {item_name} 的墨水屏图片，返回结果字典。

    返回:
      {
        "bin_bytes": bytes,   # 墨水屏裸位图数据（可直接发给设备）
        "bin_path":  str,     # .bin 文件路径
        "bmp_path":  str,     # .bmp 预览文件路径
        "width":     int,
        "height":    int,
      }

    失败时抛出异常（由调用方捕获并转换为对用户的提示）。
    """
    config = {**DEFAULT_CONFIG, **(cfg or {})}
    api_key = _resolve_api_key(config)
    if not api_key:
        raise ValueError("缺少 SiliconFlow API Key，请在 config.yaml 的 plugins.draw_image.api_key 配置")

    item_name = (item_name or "").strip()
    if not item_name:
        raise ValueError("绘画对象不能为空")

    size = tuple(config.get("size", (200, 200)))
    output_dir = config.get("output_dir", "./sd_images_test")
    os.makedirs(output_dir, exist_ok=True)

    prompt = PROMPT_TEMPLATE.format(item=item_name)

    # 1. 调用 API 生图
    resp = requests.post(
        config["api_url"],
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.get("model", "Kwai-Kolors/Kolors"),
            "prompt": prompt,
            "image_size": "512x512",
            "num_inference_steps": 20,
            "batch_size": 1,
        },
        timeout=config.get("timeout", 30),
    )

    if resp.status_code != 200:
        raise RuntimeError(f"生图 API 报错 [{resp.status_code}]: {resp.text[:200]}")

    img_url = resp.json()["data"][0]["url"]
    img_data = requests.get(img_url, timeout=20).content
    img_raw = Image.open(BytesIO(img_data))

    # 2. 预处理：灰度 -> 高质量缩放 -> 对比度增强 -> 二值化
    img_gray = img_raw.convert("L")
    img_resized = img_gray.resize(size, Image.Resampling.LANCZOS)

    enhancer = ImageEnhance.Contrast(img_resized)
    img_enhanced = enhancer.enhance(config.get("contrast_factor", 3.0))

    img_1bit = img_enhanced.point(
        lambda x: 0 if x < config.get("threshold", 160) else 255, "1"
    )

    width, height = img_1bit.size

    # 3. 输出 .bmp（电脑预览）与 .bin（墨水屏刷屏）
    bmp_path = os.path.join(output_dir, f"{item_name}.bmp")
    img_1bit.save(bmp_path)

    bin_bytes = convert_to_epaper_raw_bin(img_1bit)
    bin_path = os.path.join(output_dir, f"{item_name}.bin")
    with open(bin_path, "wb") as f:
        f.write(bin_bytes)

    return {
        "bin_bytes": bin_bytes,
        "bin_path": bin_path,
        "bmp_path": bmp_path,
        "width": width,
        "height": height,
    }
