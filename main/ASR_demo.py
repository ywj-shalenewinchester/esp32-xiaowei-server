import sounddevice as sd
import numpy as np
from funasr import AutoModel
import onnxruntime as ort
import re
import os
import collections

# ================= 配置区 =================
ASR_MODEL_PATH = "./xiaozhi-server/models/SenseVoiceSmall"
VAD_MODEL_PATH = "./xiaozhi-server/models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
# VAD 参数调整：提高阈值，降低敏感度
THRESHOLD_HIGH = 0.6  # 从 0.5 提高到 0.6
THRESHOLD_LOW = 0.4  # 从 0.3 提高到 0.4
MIN_SILENCE_DURATION_MS = 500  # 从 200ms 增加到 500ms
FRAME_WINDOW_THRESHOLD = 4  # 从 3 提高到 4（5帧里≥4帧有声）
WINDOW_SIZE = 5
MIN_VOICE_DURATION_MS = 500  # 从 1000ms 降低到 500ms，避免丢失短句
LANGUAGE = "zh"


# =========================================

def clean_sensevoice_text(text: str) -> str:
    """清理 SenseVoice 输出的语言标签"""
    return re.sub(r'<\|[^|]*\|>', '', text).strip()


def main():
    if not os.path.exists(os.path.join(ASR_MODEL_PATH, "model.pt")):
        print(f"未找到 ASR 模型文件，请检查路径: {ASR_MODEL_PATH}")
        return
    if not os.path.exists(VAD_MODEL_PATH):
        print(f"未找到 VAD 模型文件，请检查路径: {VAD_MODEL_PATH}")
        return

    print(f"正在加载 ASR 模型: {ASR_MODEL_PATH}")
    asr_model = AutoModel(
        model=ASR_MODEL_PATH,
        vad_kwargs={"max_single_segment_time": 30000},
        disable_update=True,
        hub="hf",
    )
    print("ASR 模型加载完成！")

    print(f"正在加载 VAD 模型: {VAD_MODEL_PATH}")
    sess_options = ort.SessionOptions()
    sess_options.inter_op_num_threads = 1
    sess_options.intra_op_num_threads = 1

    vad_session = ort.InferenceSession(
        VAD_MODEL_PATH,
        sess_options=sess_options,
        providers=['CPUExecutionProvider']
    )
    print("VAD 模型加载完成！")

    # VAD 状态变量
    voice_buffer = bytearray()
    silence_counter = 0
    is_speaking = False
    client_voice_window = collections.deque(maxlen=WINDOW_SIZE)
    is_voice_prev = False
    vad_state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    silence_frames_threshold = int(MIN_SILENCE_DURATION_MS / (FRAME_SAMPLES * 1000 / SAMPLE_RATE))
    min_voice_frames = int(MIN_VOICE_DURATION_MS / (FRAME_SAMPLES * 1000 / SAMPLE_RATE))

    print("开始监听，说话即可识别（按 Ctrl+C 退出）...\n")
    print(
        f"参数：高阈值={THRESHOLD_HIGH}, 低阈值={THRESHOLD_LOW}, 静音={MIN_SILENCE_DURATION_MS}ms, 最短语音={MIN_VOICE_DURATION_MS}ms\n")

    try:
        with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
        ) as stream:
            frame_count = 0
            while True:
                audio_chunk, _ = stream.read(FRAME_SAMPLES)
                chunk_bytes = audio_chunk.tobytes()
                frame_count += 1

                # 转换为 float32 [-1, 1]
                audio_float = audio_chunk.astype(np.float32) / 32768.0
                audio_float = audio_float.reshape(1, -1)

                # 拼接 context
                audio_input = np.concatenate([context, audio_float], axis=1).astype(np.float32)

                # Silero VAD 推理
                ort_inputs = {
                    'input': audio_input,
                    'state': vad_state,
                    'sr': np.array(SAMPLE_RATE, dtype=np.int64),
                }
                ort_outputs = vad_session.run(None, ort_inputs)
                speech_prob = ort_outputs[0][0][0]
                vad_state = ort_outputs[1]

                # 更新 context
                context = audio_input[:, -CONTEXT_SAMPLES:]

                # 双阈值 + 滞回判断
                if speech_prob >= THRESHOLD_HIGH:
                    is_voice = True
                elif speech_prob <= THRESHOLD_LOW:
                    is_voice = False
                else:
                    is_voice = is_voice_prev
                is_voice_prev = is_voice

                # 滑动窗口
                client_voice_window.append(is_voice)
                client_have_voice = sum(client_voice_window) >= FRAME_WINDOW_THRESHOLD

                if client_have_voice:
                    # 有声时累积所有帧（包括之前静音期间的缓存）
                    if not is_speaking:
                        print(f"检测到语音开始... (prob={speech_prob:.3f})")
                        is_speaking = True
                        silence_counter = 0
                    voice_buffer.extend(chunk_bytes)
                else:
                    if is_speaking:
                        # 静音期间也累积，但标记静音帧数
                        silence_counter += 1
                        # 短暂静音也加入缓冲（保留词间停顿）
                        voice_buffer.extend(chunk_bytes)

                        if silence_counter >= silence_frames_threshold:
                            total_frames = len(voice_buffer) // (2 * FRAME_SAMPLES)
                            total_duration = total_frames * FRAME_SAMPLES * 1000 / SAMPLE_RATE
                            print(f"语音结束，总时长 {total_duration:.0f}ms")

                            if total_frames >= min_voice_frames:
                                # 去掉末尾的静音部分
                                audio_bytes = bytes(voice_buffer[:-(silence_counter * 2 * FRAME_SAMPLES)])
                                audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16).copy()
                                audio_float32 = audio_int16.astype(np.float32) / 32768.0

                                print(f"识别中...（{len(audio_float32) / SAMPLE_RATE:.2f}s）")

                                res = asr_model.generate(
                                    input=audio_float32,
                                    cache={},
                                    language=LANGUAGE,
                                    use_itn=True,
                                    batch_size_s=60,
                                )

                                raw_text = res[0]["text"]
                                clean_text = clean_sensevoice_text(raw_text)
                                if clean_text:
                                    print(f"识别结果: {clean_text}\n")
                                else:
                                    print("[无内容]\n")
                            else:
                                print(f"[忽略] 语音片段过短（{total_duration:.0f}ms）\n")

                            # 清空缓冲区
                            voice_buffer.clear()
                            silence_counter = 0
                            is_speaking = False
                            client_voice_window.clear()

    except KeyboardInterrupt:
        print("\n⏹已停止")
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()