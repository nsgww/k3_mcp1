# 语音助手前端接入说明(k3_mcp1 / 09_asr_llm_ws)

适用：178 板当前运行的 `k3-mcp.service`（项目 `/opt/k3_mcp1`)。
与旧版差异：**唤醒门控默认开启、单发模式（one-shot）默认开启**。

## 1. 连接

| 场景 | 地址 |
|---|---|
| 页面以 https 打开（lg-store) | `wss://192.168.1.178:8443/ws/asr-llm` |
| 页面以 http 打开 | `ws://192.168.1.178/ws/asr-llm` |
| 直连服务（调试） | `ws://192.168.1.178:8060/stream` |

- nginx 会把 `/ws/asr-llm` 改写到 `/stream`；直连时路径必须恰好 `/stream`,**不带任何查询参数**（带 `?` 会被 1008 关闭）
- 浏览器 localStorage 里的 `lg_store_asr_llm_host` 如存有旧 IP，请先清掉（前端优先读它）

连接成功收到欢迎帧：

```json
{"type":"status","message":"connected, ...","mcp_connected":true,
 "mcp_tools":[...],"kws_enabled":true,"tts_enabled":true,"wake_gate":true}
```

`wake_gate: true` 表示当前连接处于门控模式。

## 2. 默认模式：门控 + 单发（推荐交互）

**流程**:

```
持续推 PCM ──→ 说唤醒词 ──→ kws + wake_state:woken + 应答音「我在」
                          → 直接说指令(5秒内开口,说完自动判停)
                          → asr → llm → tts(二进制wav) → done
                          → 回待机(wake_state:idle),须重新唤醒
```

要点：

1. **持续推 PCM**（静音也要推，超时判定靠帧流驱动）;16kHz int16 单声道，建议 32ms(1024 字节）一帧
2. **门控模式不要发 `{"type":"end"}`**（会收到 error)，说完服务端自动判停（静音 700ms)
3. **单发模式**(ONE_SHOT_MODE=1，当前默认）：每轮应答结束立刻回待机，没有连续对话窗口——每次控制/闲聊都要先喊唤醒词
4. 唤醒词（5 个）:**你好军哥 / 小艺小艺 / 小迭小迭 / 小朗小朗 / 你好朗吉**
5. 唤醒后 5 秒没开口会回待机（`woken_timeout`，可经 wake_config 调整）

**下行消息**:

| type | 含义 |
|---|---|
| `kws` | 唤醒词命中，`keyword` 字段是命中的词 |
| `wake_state` | 门控状态：`idle/woken/listening/processing`，可做 UI 状态 |
| `tts`(+二进制） | 语音帧：先是 JSON 描述（含 `byte_length`),紧跟二进制 wav;`wake_ack:true` 是唤醒应答「我在」 |
| `asr` | 识别文本 |
| `llm` | 回复文本（`function` 字段为工具调用标记） |
| `done` | 本轮结束 |
| `status` | 状态/提示（`status:"too_short"` = 语音短于 0.5s 被丢弃） |
| `error` | 错误（错误后也会补发 `done`，前端据此复位 UI) |

## 3. 可选：关闭门控（手动模式）

连接后发送：

```json
{"type":"wake_config","enabled":false}
```

之后回到旧协议：推 PCM → **发 `{"type":"end"}`** → 处理。免唤醒词，但必须发 end。

其他 wake_config 参数（按连接生效，省略即不改）:

```json
{"type":"wake_config","enabled":true,"woken_timeout":8,"followup_timeout":30}
```

| 字段 | 说明 | 默认 | 范围 |
|---|---|---|---|
| `enabled` | 门控开关 | 开 | true/false |
| `woken_timeout` | 唤醒后等开口超时（秒） | 5 | 0~120,0=不限时 |
| `followup_timeout` | 连续对话窗口（秒）,one-shot 开启时不生效 | 15 | 0~600,0=不限时 |

## 4. 浏览器采集示例

```javascript
const ws = new WebSocket("wss://192.168.1.178:8443/ws/asr-llm");
let audioCtx, pcmBuffer = [];

ws.onmessage = (ev) => {
  if (typeof ev.data === "string") {
    const m = JSON.parse(ev.data);
    if (m.type === "wake_state") ui.setState(m.state);
    if (m.type === "asr") ui.showUser(m.text);
    if (m.type === "llm") ui.showReply(m.text);
    if (m.type === "tts") audioQueue.expectWav(m.byte_length);
    if (m.type === "done") ui.turnEnd();
    if (m.type === "error") ui.showError(m.message);
  } else {
    audioQueue.play(ev.data);  // 二进制 wav
  }
};

// 麦克风:AudioContext 重采样到 16k,转 int16,持续发送(静音也发)
function onPcmChunk(int16ArrayBuffer) {
  if (ws.readyState === WebSocket.OPEN) ws.send(int16ArrayBuffer);
}
```

## 5. 常见坑

1. **说了没反应** → 门控开着且没喊唤醒词（先喊 5 个词之一），或唤醒词不在词表
2. **连上马上被关（1008)** → URL 带了查询参数
3. **连的地址不对** → 清 localStorage 的 `lg_store_asr_llm_host`
4. **播放 TTS 期间被自己麦克风回采触发** → 播放时暂停上行
5. **一轮后又要喊唤醒词** → 这是 one-shot 单发模式的预期行为；要连续对话需服务端把 `ONE_SHOT_MODE=0` 并重启
