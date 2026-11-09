# mcp_cilent WebSocket 接口使用说明

语音助手服务（09_asr_llm_ws.py）只有一个 WebSocket 通道，音频传输、识别结果、
配置接口全部走这一条连接。

- 直连：`ws://<板子IP>:8060/stream`（同机用 `127.0.0.1`）
- 经 lg-store nginx 反代：`ws://<板子IP>/ws/asr-llm`
- **路径必须恰好是 `/stream`，不能带任何查询参数**（带 `?` 会被 1008 关闭）

连接成功后服务端先发一帧欢迎消息，客户端要先收掉：

```json
{"type":"status","message":"connected, ...","mcp_connected":true,"mcp_tools":["..."],"kws_enabled":true,"tts_enabled":true}
```

之后所有配置消息都是 JSON 文本帧；音频是二进制帧（16kHz int16 单声道 PCM 小端）。

---

## 一、语音交互（两种模式）

### 1. 手动模式（默认）

推 PCM → 发 `{"type":"end"}` 结束 → 服务端走 ASR → LLM/Agent → TTS。

```javascript
ws.send(pcmBytes);                          // 二进制帧,可多次
ws.send(JSON.stringify({type: "end"}));     // 结束本段,开始处理
```

下行消息：`status`(processing audio)→ `asr` → `llm` → `tts`(JSON)+ 二进制 wav → `done`。
短于 0.5s 的音频直接丢弃：`{"type":"status","status":"too_short","message":"语音过短..."}`。

### 2. 唤醒门控模式（wake_config）

开启后只持续推 PCM（静音也要推），命中唤醒词才收音，**不要发 end**。

```json
// 开启 + 调时间(三个字段都可省略)
{"type":"wake_config","enabled":true,"woken_timeout":8,"followup_timeout":30}

// 查询
{"type":"wake_config"}

// 关闭(回手动模式)
{"type":"wake_config","enabled":false}
```

| 字段 | 说明 | 默认 | 范围 |
|---|---|---|---|
| `enabled` | 开关；省略=仅查询 | — | true/false |
| `woken_timeout` | 唤醒后等开口超时（秒） | 5 | 0~120,**0=不限时** |
| `followup_timeout` | 应答后连续对话窗口（秒） | 15 | 0~600,**0=不限时** |

- 配置**按连接生效**，新连接恢复默认
- 状态变化推 `wake_state`：`idle/woken/listening/processing/followup`
- 唤醒应答「我在」:`{"type":"tts",...,"wake_ack":true}` + 二进制 wav
- 旧消息 `{"type":"wake_gate","enabled":...}` 兼容

详细说明见 `wake_config_api.md`。

---

## 二、MCP 服务器管理（mcp_config，全局生效）

```json
// 查询全部服务器
{"type":"mcp_config"}

// 新增(name 不存在)或改地址(name 已存在)
{"type":"mcp_config","action":"set","name":"PGMControl SSE","url":"http://192.168.1.115:8010/sse"}

// 删除
{"type":"mcp_config","action":"remove","name":"灯光2号"}

// 兼容旧用法:只给 url = 改第一台
{"type":"mcp_config","url":"http://192.168.1.200:8010/sse"}
```

变更即时写入 `mcp.json`（重启保留）并热重连连接池，响应带最新 `servers` 列表。
详细说明见 `mcp_config_api.md`。

---

## 三、唤醒词表管理（kws_keywords）

```json
// 查询当前词表
{"type":"kws_keywords"}
// → {"type":"kws_keywords","phrases":["你好军哥","小艺小艺","小迭小迭"],"kws_enabled":true}

// 上传纯中文词表(推荐,一行一词,自动生成拼音)
{"type":"kws_keywords","raw_content":"你好军哥\n小艺小艺\n小迭小迭"}

// 或上传 sherpa 原生格式(每行:拼音token @中文词)
{"type":"kws_keywords","content":"n ǐ h ǎo j ūn g ē @你好军哥\nx iǎo y ì x iǎo y ì @小艺小艺"}
```

- 替换 `keywords.txt` 并同步 `keywords_raw.txt`，**热重载 KWS 引擎**，无需重启
- 已有连接的 KWS session 不受影响，新连接用新词表
- 前端上传文件：读出文件文本放进 `raw_content` 字段即可（小文本，无需 multipart）
- 错误处理：`content` 行缺 `@`、内容为空 → 返回 `{"type":"error","message":"..."}`

原词表备份：模型目录下 `keywords.txt.orig` / `keywords_raw.txt.orig`。

---

## 四、其他辅助消息

| 发送 | 响应 |
|---|---|
| `{"type":"ping"}` | `{"type":"pong"}` |
| `{"type":"mcp_status"}` | MCP 连接状态 + 工具详情 |
| `{"type":"mcp_refresh"}` | 重新拉取工具表 |
| `{"type":"config","tts":false}` | 开关 TTS（该连接） |

---

## 五、完整接入示例

### 浏览器

```javascript
const ws = new WebSocket("ws://192.168.1.115:8060/stream");

ws.onopen = () => {
  // 1. 开启唤醒门控,连续对话不限时
  ws.send(JSON.stringify({
    type: "wake_config", enabled: true, followup_timeout: 0
  }));
};

ws.onmessage = (ev) => {
  if (typeof ev.data === "string") {
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case "status":      console.log("状态:", m.message); break;
      case "wake_state":  ui.setState(m.state); break;   // idle/woken/listening/processing/followup
      case "kws":         console.log("命中:", m.keyword); break;
      case "asr":         ui.showUser(m.text); break;
      case "llm":         ui.showReply(m.text); break;
      case "tts":         audioQueue.expectWav(m.byte_length); break;
      case "error":       console.warn(m.message); break;
    }
  } else {
    audioQueue.play(ev.data);   // 二进制 wav 音频
  }
};

// 麦克风:AudioContext 重采样到 16kHz,按 32ms(1024 字节)一帧持续发送,静音也发
function onPcmFrame(frame) {
  if (ws.readyState === WebSocket.OPEN) ws.send(frame);
}
```

### Python

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8060/stream") as ws:
        print(await ws.recv())                      # 欢迎帧
        await ws.send(json.dumps({
            "type": "wake_config", "enabled": True, "followup_timeout": 30
        }))
        print(await ws.recv())                      # wake_gate=on
        # ... 持续发送 PCM 二进制帧 / 接收 JSON 与 wav
asyncio.run(main())
```

## 注意事项

1. 没有 HTTP 接口，8060 只讲 WebSocket 协议
2. 唤醒门控模式下**不要发 `end`**；手动模式下必须发 `end` 才处理
3. 播放 TTS 期间建议暂停上行，防止喇叭回采误触发
4. `mcp_config` / `kws_keywords` 是全局生效（改文件+热重载），`wake_config` 按连接生效
5. MCP 工具是否调用、调用哪个工具及参数全部由 LLM 根据 `tools/list` 的 name、description、inputSchema 决定；Host 只接受精确工具名并校验 JSON Schema，不做关键词、意图或模糊名称纠错
6. 语音唤醒 KWS 仅控制何时收音，与 MCP 工具路由相互独立
