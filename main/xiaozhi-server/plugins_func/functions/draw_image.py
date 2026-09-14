"""AI 绘画工具（画到墨水屏）

当用户说出「画画 / 画一个 / 画只 / 画一条 …」等意图时，由 LLM 调用本工具：
  1. 调用 SiliconFlow 生图
  2. 转成 200×200 墨水屏 .bin
  3. 通过 WebSocket 发送给设备，由设备端 EPD_DisplayBitmap 刷屏
"""

import asyncio
from typing import TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.handle.sendAudioHandle import send_display_image, send_display_message
from core.utils.draw_image_generator import generate_drawing

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
        # 先告诉设备正在绘制
        try:
            await send_display_message(conn, f"正在画 {item} ...")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"发送绘制提示失败: {e}")

        cfg = conn.config.get("plugins", {}).get("draw_image", {})

        # 生图 + 转 .bin 是阻塞操作（requests + PIL），丢到线程池避免卡事件循环
        result = await asyncio.to_thread(generate_drawing, item, cfg)

        # 把墨水屏 .bin 发给设备
        await send_display_image(
            conn,
            result["bin_bytes"],
            width=result["width"],
            height=result["height"],
            image_format="epaper_bin",
        )

        logger.bind(tag=TAG).info(
            f"已生成并发送图片: {item} -> {result['bin_path']} ({len(result['bin_bytes'])} 字节)"
        )
        return ActionResponse(
            action=Action.REQLLM,
            result=f"已成功把「{item}」画到设备墨水屏上，并已发送到设备屏幕显示。",
            response=f"已经为你画好了{item}，快看看屏幕上吧",
        )

    except Exception as e:
        logger.bind(tag=TAG).error(f"绘画失败: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            result=str(e),
            response=f"画画的时候出了点问题：{e}",
        )
