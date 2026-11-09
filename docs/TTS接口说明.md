# TTS 对外 HTTP 接口说明

`k3_mcp1` 提供独立的 HTTP TTS 接口，接收中文文本后合成语音，并以 UDP 组播方式发送给音频播放设备。

---

## 接口地址

```http
POST http://<板子IP>:8061/v1/tts
```

默认监听端口 `8061`，可通过环境变量 `TTS_HTTP_PORT` 修改。

---

## 请求头

| 头字段 | 值 |
|--------|-----|
| `Content-Type` | `application/json` |

---

## 请求参数

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `text` | string | 是 | - | 要合成的中文文本 |
| `group` | string | 否 | `224.2.1.179` | 目标组播地址 |
| `port` | int | 否 | `6020` | 目标组播端口 |

---

## 请求示例

```bash
curl -X POST http://192.168.1.179:8061/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "你好，欢迎使用语音助手"}'
```

自定义目标组播：

```bash
curl -X POST http://192.168.1.179:8061/v1/tts \
  -H "Content-Type: application/json" \
  -d '{
    "text": "会议即将开始，请做好准备",
    "group": "224.2.1.168",
    "port": 6020
  }'
```

---

## 响应格式

### 成功

```json
{
  "ok": true,
  "text": "你好",
  "group": "224.2.1.179",
  "port": 6020,
  "byte_length": 23956,
  "packets": 113,
  "duration": 0.747
}
```

| 字段 | 说明 |
|------|------|
| `ok` | 是否成功 |
| `text` | 实际合成的文本 |
| `group` / `port` | 实际发送到的组播地址和端口 |
| `byte_length` | 合成的 WAV 文件字节数 |
| `packets` | 发送的 UDP 组播包数量 |
| `duration` | 音频时长（秒，近似值） |

### 失败

```json
{
  "ok": false,
  "error": "text is required"
}
```

常见错误：

| 错误 | 原因 |
|------|------|
| `text is required` | 未传 `text` 或为空 |
| `tts not available` | TTS 未启用或初始化失败 |
| `tts synthesis failed` | 合成失败，未生成音频文件 |

---

## 组播音频格式

发送给目标组播的音频格式与 `multi-stream-asr` 输入协议一致：

| 参数 | 值 |
|------|-----|
| 采样率 | 48 kHz |
| 声道 | 单声道 |
| 采样位宽 | 16 bit（PCM16） |
| UDP 包大小 | 1280 字节 |
| 有效音频 | 每包前 640 字节（320 个采样点） |
| 包间隔 | 约 6.67 毫秒 |

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TTS_HTTP_HOST` | `0.0.0.0` | HTTP 服务监听地址 |
| `TTS_HTTP_PORT` | `8061` | HTTP 服务监听端口 |
| `TTS_MULTICAST_GROUP` | `224.2.1.179` | 默认 TTS 输出组播地址 |
| `TTS_MULTICAST_PORT` | `6020` | 默认 TTS 输出组播端口 |

---

## 注意事项

1. 调用前请确保 `k3-mcp.service` 已启动且 TTS 初始化成功（日志中出现 `TTS: enabled (matcha)`）。
2. 音频播放设备需加入目标组播组（如 `224.2.1.179:6020`）才能收到音频。
3. 该接口为同步阻塞调用，合成和发送完成后才返回，时长取决于文本长度。
