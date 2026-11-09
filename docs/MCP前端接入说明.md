# MCP 语音助手前端接入说明

`k3_mcp1` 语音助手服务运行后，前端可通过 WebSocket 接入。本文档面向前端开发者，说明各接入方式、消息协议和接口格式。

---

## 1. 接入方式

服务启动后，提供三种 WebSocket 接入路径：

| 路径 | 用途 | 通电后是否自动工作 | 前端需要做什么 |
|------|------|--------------------|----------------|
| `/stream` | 浏览器/前端麦克风推流 | ❌ 否 | 连接 WebSocket + 采集麦克风 + 推送 PCM 音频 |
| `/stream/local_mic` | 板载本地麦克风 | ✅ 是 | 可选连接，仅用于查看结果 |
| `/stream/multicast` | 组播音频输入 | ✅ 是 | 可选连接，仅用于查看结果 |

> 说明：`/stream/local_mic` 和 `/stream/multicast` 在服务端通电启动后已经自动开始采集/接收音频并进行控制。前端连接这两个路径只是订阅事件和播放 TTS，不控制是否开始。

---

## 2. 浏览器麦克风推流（`/stream`）

### 2.1 连接地址

```text
ws://<板子IP>:8060/stream
```

### 2.2 音频要求

- 格式：PCM16、单声道
- 采样率：16 kHz
- 数据：Int16 小端二进制帧
- 连接建立后即可直接推送音频，无需发送 `start`

### 2.3 前端流程

```
1. 获取用户麦克风权限
2. 连接 ws://<IP>:8060/stream
3. 在 ScriptProcessor / AudioWorklet 中把 Float32 转为 Int16
4. 通过 ws.send(int16Buffer) 持续推送
5. 接收服务端 JSON 消息和 TTS 二进制音频
```

### 2.4 示例代码片段

```javascript
const ws = new WebSocket('ws://192.168.1.179:8060/stream');
ws.binaryType = 'blob';

const stream = await navigator.mediaDevices.getUserMedia({
  audio: { sampleRate: 16000, channelCount: 1, echoCancellation: false }
});

const ctx = new AudioContext({ sampleRate: 16000 });
const source = ctx.createMediaStreamSource(stream);
const processor = ctx.createScriptProcessor(2048, 1, 1);

processor.onaudioprocess = (e) => {
  const floatData = e.inputBuffer.getChannelData(0);
  const int16 = new Int16Array(floatData.length);
  for (let i = 0; i < floatData.length; i++) {
    let s = floatData[i] * 32768;
    if (s > 32767) s = 32767;
    if (s < -32768) s = -32768;
    int16[i] = s;
  }
  if (ws.readyState === 1) ws.send(int16.buffer);
};

source.connect(processor);
processor.connect(ctx.destination);
```

---

## 3. 组播音频订阅（`/stream/multicast`）

服务端通电后已自动连接组播，前端可订阅该路径查看实时状态和识别结果。

### 3.1 连接地址

```text
ws://<板子IP>:8060/stream/multicast?group=224.2.1.168&port=6020
```

### 3.2 说明

- 连接后服务端立即返回 `connected` 状态
- 无需推送音频，服务端从组播组自动接收
- 可发送 `set_multicast_group` 切换组播地址（未接收音频时）

---

## 4. 板载麦克风订阅（`/stream/local_mic`）

服务端通电后已自动打开板载麦克风，前端可订阅该路径查看结果。

### 4.1 连接地址

```text
ws://<板子IP>:8060/stream/local_mic
```

### 4.2 说明

- 连接后服务端自动开始从板载麦克风采音
- 前端无需推送音频
- 断开连接不会停止服务端麦克风，仅停止当前前端订阅

---

## 5. WebSocket 控制消息

前端连接后可发送以下 JSON 消息：

| 消息 | 说明 |
|------|------|
| `{"type":"ping"}` | 心跳，服务端返回 `pong` |
| `{"type":"config","sample_rate":16000,"tts":true}` | 配置采样率和 TTS 开关 |
| `{"type":"wake_gate","enabled":false}` | 关闭/开启唤醒门控 |
| `{"type":"set_multicast_group","group":"224.2.1.168","port":6020}` | 切换组播地址（仅组播模式） |
| `{"type":"mcp_status"}` | 查询 MCP 连接状态和工具列表 |

---

## 6. 服务端消息类型

| 类型 | 说明 |
|------|------|
| `status` | 状态信息 |
| `kws` | 唤醒词命中 |
| `wake_state` | 唤醒门控状态变化 |
| `asr` | 语音识别结果 |
| `llm` | LLM 回复 |
| `tts` | TTS 信息（JSON 后紧跟二进制 WAV） |
| `error` | 错误信息 |
| `pong` | ping 响应 |

### 6.1 TTS 音频接收

服务端发送 `tts` JSON 后，紧接着会发送二进制 WAV 音频数据：

```javascript
ws.onmessage = (ev) => {
  if (typeof ev.data !== 'string') {
    // 二进制 WAV，播放
    playAudio(ev.data);
    return;
  }
  const m = JSON.parse(ev.data);
  if (m.type === 'tts') {
    console.log('TTS 文本:', m.text, '大小:', m.byte_length);
  }
};
```

---

## 7. TTS 对外 HTTP 接口

如需让服务合成指定文本并发送 TTS 音频到组播，可调用 HTTP 接口。

### 7.1 接口地址

```http
POST http://<板子IP>:8061/v1/tts
Content-Type: application/json
```

### 7.2 请求参数

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `text` | string | 是 | - | 要合成的中文文本 |
| `group` | string | 否 | `224.2.1.179` | 目标组播地址 |
| `port` | int | 否 | `6020` | 目标组播端口 |

### 7.3 请求示例

```bash
curl -X POST http://192.168.1.179:8061/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "你好，欢迎使用语音助手"}'
```

### 7.4 响应示例

```json
{
  "ok": true,
  "text": "你好，欢迎使用语音助手",
  "group": "224.2.1.179",
  "port": 6020,
  "byte_length": 42544,
  "packets": 200,
  "duration": 1.33
}
```

---

## 8. 注意事项

1. **组播和本地麦克风已自动连接**：服务端通电后已自动开始工作，前端连接仅用于查看结果和播放 TTS。
2. **浏览器麦克风需要用户授权**：`/stream` 必须由用户点击触发 `getUserMedia()`，无法自动连接。
3. **同时订阅多个路径可能收到重复事件**：如果本地麦克风和组播拾取到同一声音，可能触发两次唤醒。
4. **组播输入格式**：输入组播必须是 48 kHz PCM16 单声道，UDP 包 1280 字节（前 640 字节有效）。
5. **TTS 输出默认组播**：`224.2.1.179:6020`，音频播放设备需加入该组播组。
