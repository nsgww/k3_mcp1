# 语音唤醒门控接口（wake_config）

服务：`ws://<板子IP>:8060/stream`（经 lg-store nginx 反代时为 `ws://<板子IP>/ws/asr-llm`）

唤醒门控开启后，上行 PCM 只在「唤醒词命中 → 说完一段话」后才送入 ASR/LLM/TTS；
其余时间的音频被丢弃。门控**按连接**生效，每个 WS 连接独立配置。

## 1. 连接

- 地址必须恰好是 `/stream`，**不带任何查询参数**（带 `?` 会被 1008 关闭）
- 音频格式：16kHz int16 单声道 PCM 小端，任意分包的二进制帧

连接成功后服务端下发：

```json
{"type":"status","message":"connected, ...","mcp_connected":true,"mcp_tools":["..."],"kws_enabled":true,"tts_enabled":true}
```

## 2. 开启 / 关闭 / 调整

```json
{
  "type": "wake_config",
  "enabled": true,
  "woken_timeout": 8,
  "followup_timeout": 30
}
```

| 字段 | 说明 | 默认 | 范围 |
|---|---|---|---|
| `enabled` | 开启/关闭门控；省略 = 仅查询 | — | true/false |
| `woken_timeout` | 唤醒后等待开口说话的超时（秒），超时未说话回待机 | 5 | 0~120,**0 = 不限时** |
| `followup_timeout` | 每轮应答后的连续对话窗口（秒），窗口内无需再说唤醒词 | 15 | 0~600,**0 = 不限时** |

- 三个字段都可单独省略，只传要改的
- 参数在**下一次状态转换**生效（进行中的倒计时不受影响）
- 配置只在当前连接有效，新连接恢复默认值

响应（同时返回当前生效值）：

```json
{"type":"status","message":"wake_gate=on","wake_gate":true,"woken_timeout":8.0,"followup_timeout":30.0}
```

查询当前状态（不带任何字段）：

```json
{"type":"wake_config"}
```

越界返回：`{"type":"error","message":"followup_timeout 需在 0~600 秒之间（0 = 不限时）"}`

> 旧消息 `{"type":"wake_gate","enabled":true}` 完全兼容，也可带 `woken_timeout` / `followup_timeout`。

## 3. 开启后的消息流

1. **持续推 PCM 二进制帧**（含静音也要推；门控的超时判定由帧流驱动，停推则超时停滞）。门控模式下**不要发 `{"type":"end"}`**，发了会收到 error。
2. 唤醒词命中，服务端依次下发：

```json
{"type":"kws","keyword":"小迭小迭","message":"检测到关键词:小迭小迭"}
{"type":"wake_state","state":"woken"}
```

   若 TTS 开启，紧跟着下发唤醒应答「我在」（先 JSON 描述帧，后二进制 wav）：

```json
{"type":"tts","sample_rate":16000,"format":"pcm_s16le","byte_length":...,"text":"我在","wake_ack":true}
```

3. 状态变化都会推 `wake_state`：`idle → woken → listening → processing → followup`，前端可据此做「待唤醒 / 请说话 / 识别中」的 UI。
4. 说完一段（VAD 静音 700ms 判说完），服务端走 ASR → LLM →（可选 MCP 工具调用）→ TTS：
   - `{"type":"asr","text":"..."}`
   - `{"type":"llm","text":"..."}`（如有工具调用另有 function 字段）
   - `{"type":"tts",...}` + 二进制 wav
5. 应答结束进入 `followup`：窗口内直接说下一句即可，无需唤醒词；窗口到期或 `woken_timeout` 超时自动回 `idle`。

## 4. 前端接入要点

- 麦克风采集统一 16kHz；浏览器端用 AudioContext 重采样到 16k 后按 ~32ms(1024 字节）一帧持续发送，**静音帧也要发**
- 播放服务端 TTS/应答音期间，建议暂停上行（或至少静音上行），防止喇叭回采误触发收音
- 连接建立后第一件事发 `wake_config` 配置，再开始推流
- `wake_state=idle` 时显示「待唤醒」，提示用户说唤醒词；`followup_timeout: 0` 适合「一次唤醒、持续对话」的场景
- 关闭门控回到旧模式：发 `{"type":"wake_config","enabled":false}`，此后回到「推 PCM + 发 `end` 结束」的旧协议
