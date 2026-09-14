import os
import io
import sys
import time
import shutil
import psutil
import asyncio
import numpy as np

from funasr import AutoModel
from config.logger import setup_logging
from typing import Optional, Tuple, List
from core.providers.asr.utils import lang_tag_filter
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()

MAX_RETRIES = 2
RETRY_DELAY = 1  # 重试延迟（秒）


# 捕获标准输出
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        self.output = self._output.getvalue()
        self._output.close()

        # 将捕获到的内容通过 logger 输出
        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        
        # 内存检测，要求大于2G（物理内存 + Swap 虚拟内存合计）
        min_mem_bytes = 2 * 1024 * 1024 * 1024
        try:
            total_mem = psutil.virtual_memory().total + psutil.swap_memory().total
        except RuntimeError as e:
            logger.bind(tag=TAG).warning(f"获取系统内存信息失败，跳过FunASR内存检测: {e}")
        else:
            if total_mem < min_mem_bytes:
                logger.bind(tag=TAG).error(f"可用内存不足2G，当前仅有 {total_mem / (1024*1024):.2f} MB，可能无法启动FunASR")
        
        self.interface_type = InterfaceType.LOCAL
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")  # 修正配置键名
        self.language = config.get("language", "auto")
        self.delete_audio_file = delete_audio_file

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
        with CaptureOutput():
            self.model = AutoModel(
                model=self.model_dir,
                vad_kwargs={"max_single_segment_time": 30000},
                disable_update=False,  # download missing model weights
                hub="hf",
                # device="cuda:0",  # 启用GPU加速
            )

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本主处理逻辑"""
        retry_count = 0
        
        while retry_count < MAX_RETRIES:
            try:
                if artifacts is None:
                    return "", None

                # 语音识别 - 使用线程池避免阻塞事件循环
                start_time = time.time()
                # Convert PCM bytes to float32 array (SenseVoice requires float32)
                pcm_array = np.frombuffer(artifacts.pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                result = await asyncio.to_thread(
                    self.model.generate,
                    input=pcm_array,
                    cache={},
                    language=self.language,
                    use_itn=True,
                    batch_size_s=60,
                )
                logger.bind(tag=TAG).info(
                    f"FunASR result type: {type(result)}, len: {len(result) if hasattr(result, '__len__') else 'N/A'}, "
                    f"result[0] type: {type(result[0]) if len(result) > 0 else 'empty'}"
                )
                # FunASR may return list[dict] or list[str] depending on version
                raw = result[0]
                if isinstance(raw, dict):
                    raw_text = raw.get("text", "")
                else:
                    raw_text = str(raw)
                text = lang_tag_filter(raw_text)
                if isinstance(text, dict):
                    content = text.get("content", raw_text)
                    logger.bind(tag=TAG).debug(
                        f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {content}"
                    )
                else:
                    content = str(text)
                    logger.bind(tag=TAG).debug(
                        f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {content}"
                    )

                return text, artifacts.file_path

            except OSError as e:
                retry_count += 1
                if retry_count >= MAX_RETRIES:
                    logger.bind(tag=TAG).error(
                        f"语音识别失败（已重试{retry_count}次）: {e}", exc_info=True
                    )
                    return "", None
                logger.bind(tag=TAG).warning(
                    f"语音识别失败，正在重试（{retry_count}/{MAX_RETRIES}）: {e}"
                )
                time.sleep(RETRY_DELAY)

            except Exception as e:
                logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
                return "", None
