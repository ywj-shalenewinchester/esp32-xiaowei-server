import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

TAG = __name__

class DirectTextMessageHandler(TextMessageHandler):
    """直接文本消息处理器：ESP32 串口探针直接发送文本，跳过 ASR 直接送入 LLM"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.TEXT

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        text = msg_json.get("text", "")
        if not text:
            conn.logger.bind(tag=TAG).warning(f"收到空text消息：{msg_json}")
            return

        conn.last_activity_time = time.time() * 1000
        conn.just_woken_up = True
        enqueue_asr_report(conn, text, [])
        await startToChat(conn, text)
