# AI 绘画 → 设备屏幕显示（draw_image 工具）

当用户对设备说「画一只小狗」时，LLM 调用服务端的 `draw_image` 工具：服务端用 SiliconFlow 生图，按**设备上报的屏幕尺寸**缩放、给圆屏加圆形遮罩、编码成 JPEG，再通过 WebSocket 发给设备显示。

设备在连接时的 `hello` 消息里上报自己的屏幕尺寸，所以同一套服务端可以同时带 466×466 / 560×560 等不同尺寸的设备。

## 一、改动记录

| 文件 | 操作 | 说明 |
| --- | --- | --- |
| `core/utils/screen_info.py` | 新增 | 解析/校验设备上报的屏幕信息，纯函数、无 IO |
| `core/utils/draw_image_generator.py` | 重写 | 按目标尺寸生图 + 缩放 + 圆形遮罩 + JPEG 编码，返回 bytes |
| `core/handle/helloHandle.py` | 修改 | 解析 `hello.features.display`，落到 `conn.screen` |
| `core/connection.py` | 修改 | 声明 `self.screen = None` |
| `core/handle/sendAudioHandle.py` | 修改 | `send_display_image()` 发送 `format="jpeg"` + `round` 字段 |
| `plugins_func/functions/draw_image.py` | 修改 | 取 `conn.screen`，透传给生成器 |
| `config.yaml` | 修改 | `plugins.draw_image` 改为 `default_screen` + JPEG 参数 |

> 旧版本走的是 200×200 黑白墨水屏 `.bin`（`EPD_DisplayBitmap`、1-bit、5000 字节裸位图）。该链路已**整体移除**。

## 二、协议

### 设备 → 服务端：hello 里上报屏幕

```json
{"type": "hello",
 "audio_params": {"format": "opus", "sample_rate": 24000, "channels": 1, "frame_duration": 60},
 "features": {
   "mcp": true,
   "display": {"width": 466, "height": 466, "shape": "round", "color": true}}}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `width` / `height` | 是 | 屏幕像素尺寸。单边夹到 `[64, 1024]`，小于 64 视为上报错误并回退默认值 |
| `shape` | 否 | `"round"` \| `"rect"`。缺省时方形按 `round`、非方形按 `rect` |
| `color` | 否 | 仅为向后兼容保留，服务端已不区分。上报 `false` 会记一条 WARNING |

整条 `display` 的容错规则：

- 不带 `width`/`height`（如旧固件发 `{"display": true}`）→ 忽略，用服务端默认屏幕
- 长短边比 > 4:1 → 忽略，用服务端默认屏幕
- 非 dict 或解析不出数字 → 忽略，用服务端默认屏幕

被忽略时服务端会打 WARNING 日志，图照常能画。

### 服务端 → 设备：图片消息

```json
{"type": "display",
 "image": "<base64 JPEG>",
 "format": "jpeg",
 "width": 466,
 "height": 466,
 "round": true,
 "session_id": "..."}
```

`image` 是 **baseline JPEG**（非 progressive），`round=true` 表示面板是圆形——四角已由服务端填成纯色（默认黑）。

## 三、服务端配置

`config.yaml` 的 `plugins.draw_image`：

```yaml
draw_image:
  api_key: ""              # 优先读环境变量 SILICONFLOW_API_KEY
  model: "Kwai-Kolors/Kolors"
  default_screen:          # 设备未上报屏幕信息时的兜底
    width: 466
    height: 466
    shape: "round"
  api_size: ""             # 留空 = 按目标尺寸自动选（方形目标 -> 1024x1024）
  jpeg_quality: 85         # 466x466 约 80KB，base64 后约 105KB
  bg_color: "#000000"      # 圆屏四角填充色（JPEG 没有透明通道）
  supersample: 4           # 圆形遮罩抗锯齿超采样倍数，1 = 关闭
  max_message_bytes: 524288  # 超过则自动降质重编码
  output_dir: "./sd_images_test"
  save_preview: true       # 落盘 .jpg 预览，方便肉眼确认圆形遮罩
```

屏幕优先级：**设备上报 > `default_screen` > 内置默认（466 圆屏）**。

### 启用步骤

1. 装依赖：`pip install Pillow`（`requirements.txt` 里已含 `Pillow==11.1.0`）
2. `config.yaml` 的 `selected_module.Intent` 设为 `function_call`（或 `intent_llm`），否则工具不会注入给 LLM
3. 确认 `Intent.function_call.functions` 里包含 `draw_image`
4. 配好 API Key：`config.yaml` 的 `plugins.draw_image.api_key`，或环境变量 `SILICONFLOW_API_KEY`

> ⚠️ **智控台（manager-api）模式下 `config.yaml` 的 `plugins` 会被整体丢弃**（见 `core/connection.py` 里 `self.config["plugins"] = plugin_from_server`）。此时 API Key 只能来自 `SILICONFLOW_API_KEY` 环境变量。控制台里没有 `draw_image` 条目时，插件配置为 `{}`，会走内置默认屏幕，行为正常。

## 四、设备端接入（待实现）

固件仓库（`my-esp32-main`）需要做三件事：

1. **hello 上报屏幕**——在已有的 hello 消息里带上 `features.display`（见上面的协议）。
2. **处理 `type == "display"`**——对 `image` 做 base64 解码，得到 JPEG 字节流，交给设备上的 **baseline JPEG** 解码器：
   - LVGL 自带解码器（启用 `LV_USE_SJPG` / 注册 `lv_split_jpeg_init()`），或
   - `esp_jpeg` / `esp_new_jpeg` 组件
   - ⚠️ **不要用只支持 baseline 的库去解 progressive JPEG** —— 服务端已固定发 baseline
3. **调大 WebSocket 接收缓冲区**——这条最容易漏：

   | | 旧墨水屏 | 现在 |
   | --- | --- | --- |
   | 原始帧 | 5000 B | 60–250 KB |
   | base64 后 | ~6.7 KB | **80–330 KB** |

   旧墨水屏的帧只有 6.7 KB，而 466×466 的 JPEG base64 后约 105 KB。如果 `esp_websocket_client` 的接收缓冲装不下，帧会分片到达，朴素的 `cJSON_Parse` 会直接失败。**建议配置 ≥256 KB**（`max_message_bytes` 默认 512 KB，服务端会在超限时自动降质）。

## 五、端到端验证

服务端侧（无需真机，`_call_image_api` 是唯一的网络出口，可以直接打桩替换）：

1. `py_compile` 过一遍改动的文件
2. 用 `Image.new("RGB", (1024, 1024), ...)` 当假图，跑 `_fit_to_target` → `compose_round` → `encode_jpeg`，断言尺寸、四角为背景色、中心为画面、JPEG 首尾字节正确
3. 断言圆形遮罩边缘有中间 alpha（平滑过渡），不是 `255 → 0` 硬台阶——否则说明超采样没生效

真机侧：

4. 设备连上 → 服务端日志出现 `客户端屏幕: 466x466 round color(device)`
5. 说「画一只小狗」→ 日志依次出现 `draw_image 目标屏幕: ...` → `执行工具: draw_image` → 发送行含 `466x466`
6. 打开 `sd_images_test/` 下的 `.jpg` 预览，肉眼确认：圆形、四角纯黑、主体没被遮罩切掉、无文字水印
7. 容错：让设备上报 `{"width": 99999, "height": -5}` → 日志显示夹取后的值，图仍能画出来；设备不上报 → 日志显示 `466x466 round color(builtin)`

## 六、已知限制

- **JPEG 没有 alpha**，圆屏四角只能填纯色。物理圆形面板自己做遮罩没问题；方形面板塞进圆边框会露出黑角。
- **服务端不做并发限制**：生图 + 编码是阻塞操作，跑在 `conn.executor` 线程池里，最坏情况（`timeout` 45s + 下载 30s）会占住一个 worker。设备侧表现为这段时间没有响应。
- **提示词已针对圆屏优化**：圆屏的提示词里会追加 "nothing important in the corners because the display is circular"，避免主体被遮罩裁掉。
