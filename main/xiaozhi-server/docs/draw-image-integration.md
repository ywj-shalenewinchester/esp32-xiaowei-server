# AI 绘画 → 墨水屏显示（draw_image MCP 工具）

把 `tu_ai.py` 的「文生图 + 转墨水屏 `.bin`」能力做成 xiaozhi-server 里的一个可被大模型调用的工具（本项目里的 MCP/function-call 机制）。当用户对设备说「画一只小狗」时，LLM 调用 `draw_image`，服务端用 SiliconFlow 生图、转成 200×200 墨水屏裸位图，再通过 WebSocket 发给设备刷屏。

## 一、改动记录（本次改了什么）

| 文件 | 操作 | 说明 |
| --- | --- | --- |
| `core/utils/draw_image_generator.py` | 新增 | 把 `tu_ai.py` 的生图逻辑重构为可复用、可配置模块，返回 `.bin` 的 bytes |
| `plugins_func/functions/draw_image.py` | 新增 | 注册 `draw_image` 工具（`ToolType.SYSTEM_CTL`），生图并发送到设备 |
| `core/handle/sendAudioHandle.py` | 修改 | 新增 `send_display_image()`，发送 `type=="display"` 图片消息 |
| `config.yaml` | 修改 | 新增 `plugins.draw_image` 配置；`Intent.intent_llm.functions` / `Intent.function_call.functions` 追加 `draw_image` |
| `requirements.txt` | 修改 | 新增 `Pillow==11.1.0`（生图所需的 PIL） |

### 关键设计
- **图片格式**：与 `09_LVGL_V8_Test` 的 `epaper_driver_bsp::EPD_DisplayBitmap` 完全一致 —— 200×200、1-bit、MSB 高位在前、1=白 0=黑，共 5000 字节（无文件头、无调色板）。
- **发送通道**：新增 WebSocket 消息 `{"type":"display","image":"<base64>","format":"epaper_bin","width":200,"height":200}`。
- **API Key**：优先读环境变量 `SILICONFLOW_API_KEY`，否则读 `config.yaml` 的 `plugins.draw_image.api_key`。

## 二、服务端配置与启用

1. 安装依赖（新增了 Pillow）：
   ```bash
   pip install Pillow==11.1.0
   ```

2. `config.yaml` 的 `plugins.draw_image` 填好 API Key（已默认填入 tu_ai.py 里的 key）：
   ```yaml
   draw_image:
     api_key: "sk-..."
     model: "Kwai-Kolors/Kolors"
     size: [200, 200]
     contrast_factor: 3.0
     threshold: 160
     output_dir: "./sd_images_test"
   ```

3. **启用意图识别**：把 `selected_module.Intent` 从 `nointent` 改成 `function_call`（否则工具不会被注入给 LLM）：
   ```yaml
   selected_module:
     Intent: function_call
   ```

4. 确认 `Intent.function_call.functions` 里包含 `draw_image`（已加入）。

## 三、设备端接入（已实现，代码在 my-esp32-main）

设备固件是 `G:\to2\my-esp32-main`（xiaozhi），墨水屏板卡为 **waveshare ESP32-S3 ePaper 1.54（200×200）**，引脚 BUSY=8 / RST=9 / DC=10 / CS=11 / SCK=12 / MOSI=13，与 `09_LVGL_V8_Test` 一致。

服务端发送 `{"type":"display","image":"<base64 .bin>","width":200,"height":200}`，设备端改动如下：

| 文件 | 改动 |
| --- | --- |
| `main/display/display.h` | 基类 `Display` 新增虚方法 `DisplayEpaperBitmap(const uint8_t* bitmap, int width, int height)`（默认空实现，非墨水屏显示类不受影响） |
| `main/application.cc` | 收包分支新增 `type=="display"`：`image` 做 `mbedtls_base64_decode` → `Schedule` 到显示任务调 `DisplayEpaperBitmap`；新增 `#include <mbedtls/base64.h>` |
| `main/boards/waveshare/esp32-s3-epaper-1.54/custom_lcd_display.h` | `CustomLcdDisplay` 声明 `DisplayEpaperBitmap` 重写 |
| `main/boards/waveshare/esp32-s3-epaper-1.54/custom_lcd_display.cc` | 实现 `DisplayEpaperBitmap`：校验尺寸 → `memcpy(buffer, bitmap, 5000)` → `EPD_DisplayPart()` 局部刷新 |

**格式一致性**：服务端 `.bin`（行优先、MSB 高位在前、1=白）与 `CustomLcdDisplay::buffer` 布局逐字节一致（对照 `EPD_DrawColorPixel` 的 `index = y*25 + x/8`、`bit = 7 - x%8`、白=置位），所以 `memcpy` 后直接刷屏即可，无需二次转换。

**冻结窗口**：刷图后冻结 LVGL 刷屏 5 秒（`EPAPER_IMAGE_DISPLAY_MS`，可改）。期间 `lvgl_flush_cb` 直接跳过（仍调用 `lv_disp_flush_ready`），状态栏/聊天/表情等刷新都不会覆盖图片，窗口结束自动恢复 UI。

> 若你用的是 **C6 版**墨水屏板卡（`waveshare/esp32-c6-epaper-1.54`），需在它的 `custom_lcd_display.{h,cc}` 里按同样方式补上 `DisplayEpaperBitmap`。

## 四、端到端验证

1. `python -m py_compile` 编译 `core/utils/draw_image_generator.py`、`plugins_func/functions/draw_image.py`、`core/handle/sendAudioHandle.py` 确认无语法错误。
2. 单测生图：调用 `generate_drawing("小狗", cfg)`，确认产出 `小狗.bin`（5000 字节）+ `小狗.bmp`。
3. 真机：`Intent: function_call`，对设备说「画一只小狗」→ 服务端日志出现 `执行工具: draw_image` → 设备墨水屏刷出图案。
