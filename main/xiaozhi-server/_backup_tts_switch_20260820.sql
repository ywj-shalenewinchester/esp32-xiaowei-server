-- 备份：切换 TTS 到 6007 index-tts 之前的原始值（2026-08-20）
-- 如需回滚，执行下面两句即可。

-- 原 TTS_CustomTTS 配置（当时指向 GPT-SoVITS 9880）
UPDATE ai_model_config
SET config_json = '{"url": "http://127.0.0.1:9880/tts", "type": "custom", "format": "wav", "params": {}, "headers": {}, "output_dir": "tmp/"}'
WHERE id = 'TTS_CustomTTS';

-- 原两个 agent 的 TTS 指向
UPDATE ai_agent SET tts_model_id = 'TTS_EdgeTTS', tts_voice_id = 'TTS_EdgeTTS0001' WHERE id = '1e06ed05233d4010858bf92d56d3543c';
UPDATE ai_agent SET tts_model_id = 'TTS_EdgeTTS', tts_voice_id = 'TTS_EdgeTTS0003' WHERE id = '419adb2ce0f843db9c28da57aa86cb3a';
