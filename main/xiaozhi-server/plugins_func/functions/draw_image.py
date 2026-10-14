"""AI 绘画工具（画到设备屏幕）

当用户说出「画画 / 画一个 / 画只 / 画一条 …」等意图时，由 LLM 调用本工具：
  1. 从 conn.screen 取设备上报的屏幕尺寸（未上报则走配置/内置兜底）
  2. 调用 SiliconFlow 生图，缩放到屏幕尺寸，圆屏加圆形遮罩
  3. 编码成 JPEG，通过 WebSocket 发送给设备显示
"""

import asyncio
from typing import TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.handle.sendAudioHandle import send_display_image, send_display_message
from core.utils.draw_image_generator import generate_drawing, resolve_screen
from core.utils.screen_info import describe_screen

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

draw_image_function_desc = {
    "type": "function",
    "function": {
        "name": "draw_image",
        "description": (
            "当用户要求画画、画一个东西、画只小动物、画一条鱼等绘画意图时调用此工具。"
            "例如：用户说“画一只小狗”则 item 取“小狗”；用户说“帮我画个太阳”则 item 取“太阳”。"
            "把用户想画的内容作为 item 参数传入。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "用户想画的东西，例如：小狗、太阳、房子",
                },
            },
            "required": ["item"],
        },
    },
}


@register_function("draw_image", draw_image_function_desc, ToolType.SYSTEM_CTL)
async def draw_image(conn: "ConnectionHandler", item: str):
    try:
        cfg = conn.config.get("plugins", {}).get("draw_image", {})
        # 设备在 hello 里上报的屏幕；None 表示未上报，走配置/内置兜底。
        # resolve_screen 是纯函数且 O(1)，在事件循环里调用没问题。
        screen = resolve_screen(cfg, getattr(conn, "screen", None))
        logger.bind(tag=TAG).info(f"draw_image 目标屏幕: {describe_screen(screen)}")

        # 先告诉设备正在绘制
        try:
            await send_display_message(conn, f"正在画 {item} ...")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"发送绘制提示失败: {e}")

        # 生图 + 缩放 + 编码是阻塞操作（requests + PIL），丢到线程池避免卡事件循环。
        # 注意不要把 conn 传进线程 —— 非事件循环线程碰 asyncio/websocket 状态会出事。
        result = await asyncio.to_thread(generate_drawing, item, cfg, screen)

        await send_display_image(
            conn,
            result["image_bytes"],
            width=result["width"],
            height=result["height"],
            is_round=result["round"],
        )

        logger.bind(tag=TAG).info(
            f"已生成并发送图片: {item} -> {result['preview_path']} "
            f"({result['width']}x{result['height']}, {len(result['image_bytes'])} 字节)"
        )
        return ActionResponse(
            action=Action.REQLLM,
            result=f"已成功把「{item}」画到设备屏幕上并发送显示。",
            response=f"已经为你画好了{item}，快看看屏幕上吧",
        )

    except Exception as e:
        logger.bind(tag=TAG).error(f"绘画失败: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            result=str(e),
            response=f"画画的时候出了点问题：{e}",
        )
