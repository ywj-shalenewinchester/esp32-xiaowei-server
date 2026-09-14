import numpy as np
from funasr import AutoModel
import re
import os
import wave

# ================= 配置区 =================
ASR_MODEL_PATH = "./xiaozhi-server/models/SenseVoiceSmall"
AUDIO_DIR = "F:/5a731-main/5a731-main/C0936/C0936"  # 放 wav 文件的目录
LANGUAGE = "zh"

# =========================================

def clean_sensevoice_text(text: str) -> str:
    """清理 SenseVoice 输出的语言标签"""
    return re.sub(r'<\|[^|]*\|>', '', text).strip()


def read_wav_file(wav_path):
    """读取 wav 文件，返回 float32 数组 [-1, 1]"""
    with wave.open(wav_path, 'rb') as wf:
        # 检查采样率
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()

        print(f"  采样率: {sample_rate}Hz, 声道: {channels}, 位深: {sample_width * 8}bit")

        # 读取音频数据
        audio_data = wf.readframes(wf.getnframes())

        # 根据位深转换为 int16
        if sample_width == 2:  # 16bit
            audio_int16 = np.frombuffer(audio_data, dtype=np.int16)
        elif sample_width == 4:  # 32bit
            audio_int32 = np.frombuffer(audio_data, dtype=np.int32)
            audio_int16 = (audio_int32 >> 16).astype(np.int16)
        else:
            raise ValueError(f"不支持的位深: {sample_width * 8}bit")

        # 如果是立体声，转为单声道
        if channels == 2:
            audio_int16 = audio_int16.reshape(-1, 2).mean(axis=1).astype(np.int16)

        # 如果采样率不是 16kHz，需要重采样
        if sample_rate != 16000:
            print(f"  警告: 采样率不是 16kHz，需要重采样")
            # 简单线性插值重采样
            duration = len(audio_int16) / sample_rate
            old_time = np.linspace(0, duration, len(audio_int16))
            new_time = np.linspace(0, duration, int(duration * 16000))
            audio_int16 = np.interp(new_time, old_time, audio_int16).astype(np.int16)
            print(f"  重采样到 16kHz，长度: {len(audio_int16)}")

        # 转换为 float32 [-1, 1]
        audio_float = audio_int16.astype(np.float32) / 32768.0

        return audio_float, sample_rate


def main():
    if not os.path.exists(os.path.join(ASR_MODEL_PATH, "model.pt")):
        print(f"未找到 ASR 模型文件，请检查路径: {ASR_MODEL_PATH}")
        return

    if not os.path.exists(AUDIO_DIR):
        print(f"未找到音频目录: {AUDIO_DIR}")
        print("请创建 test_audio 文件夹，把 wav 文件放进去")
        return

    # 获取所有 wav 文件
    wav_files = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith('.wav')]
    if not wav_files:
        print(f"在 {AUDIO_DIR} 中没有找到 wav 文件")
        return

    print(f"找到 {len(wav_files)} 个 wav 文件\n")

    # 加载 ASR 模型
    print(f"正在加载 ASR 模型: {ASR_MODEL_PATH}")
    asr_model = AutoModel(
        model=ASR_MODEL_PATH,
        vad_kwargs={"max_single_segment_time": 30000},
        disable_update=True,
        hub="hf",
    )
    print("ASR 模型加载完成！\n")

    # 逐个测试
    for i, wav_file in enumerate(wav_files, 1):
        wav_path = os.path.join(AUDIO_DIR, wav_file)
        print(f"{'=' * 60}")
        print(f"[{i}/{len(wav_files)}] 测试: {wav_file}")
        print(f"{'=' * 60}")

        try:
            # 读取音频
            audio_float, original_sr = read_wav_file(wav_path)
            duration = len(audio_float) / 16000
            print(f"  音频时长: {duration:.2f}s")

            # ASR 推理
            print("  识别中...")
            res = asr_model.generate(
                input=audio_float,
                cache={},
                language=LANGUAGE,
                use_itn=True,
                batch_size_s=60,
            )

            # 解析结果
            raw_text = res[0]["text"]
            clean_text = clean_sensevoice_text(raw_text)

            print(f"  原始输出: {raw_text}")
            print(f"  清理后: {clean_text}")
            print()

        except Exception as e:
            print(f"  错误: {e}")
            import traceback
            traceback.print_exc()
            print()

    print("测试完成！")


if __name__ == "__main__":
    main()