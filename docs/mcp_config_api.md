# MCP 服务器配置接口（mcp_config）

服务：`ws://<板子IP>:8060/stream`（经 lg-store nginx 反代时为 `ws://<板子IP>/ws/asr-llm`）

通过 WS JSON 消息在线管理 MCP 服务器（查询 / 新增 / 改地址 / 删除）。
所有变更**立即写回 `mcp.json`**（重启后保留）并**热重连连接池**，无需重启服务。
配置为全局生效（所有 WS 连接共享同一个 MCP 连接池）。

## 1. 查询全部服务器

```json
{"type": "mcp_config"}
```

响应：

```json
{
  "type": "mcp_config",
  "servers": [
    {"name": "PGMControl SSE", "url": "http://192.168.1.115:8010/sse", "disabled": false, "connected": true, "tools": 15}
  ],
  "connected": true,
  "tools": ["btn_5_push", "btn_6_push", "..."]
}
```

- `servers[]`：每台服务器的名称、地址、是否禁用、当前连接状态、工具数
- `tools`：所有已连接服务器聚合后的完整工具表（Agent 实际可用的）

## 2. 新增服务器 / 修改地址

```json
{"type": "mcp_config", "action": "set", "name": "灯光2号", "url": "http://192.168.1.163:8010/sse"}
```

- `name` 不存在 → **新增**一台服务器（type 默认 sse）
- `name` 已存在 → **修改**该服务器的 url（description 等其他字段保持不变）
- `url` 必须以 `http://` 或 `https://` 开头，否则返回 error

## 3. 删除服务器

```json
{"type": "mcp_config", "action": "remove", "name": "灯光2号"}
```

- `name` 不存在 → 返回 `{"type":"error","message":"mcp_config 应用失败: 服务器不存在: ..."}`

## 4. 兼容旧用法

只给 `url`、不给 `name`/`action` 时，自动修改**第一台**服务器：

```json
{"type": "mcp_config", "url": "http://192.168.1.200:8010/sse"}
```

## 5. 变更响应（set / remove 成功）

```json
{
  "type": "mcp_config",
  "servers": ["...最新列表..."],
  "connected": true,
  "tools": ["...聚合工具表..."],
  "message": "MCP 服务器已更新并重连: 灯光2号"
}
```

新地址连不上时 `connected` 为 `false`，但配置已写入文件；客户端有断线重连，对端恢复后自动连上。

## 6. 多服务器注意事项

- **工具名冲突**：多台服务器暴露同名工具时，先连上的生效，后连上的同名工具不可达（服务日志会打印冲突）。请让各服务器的工具名/description 区分开。
- **LLM 原生路由**：聚合后的完整 name、description、inputSchema 直接交给 LLM；Host 不做关键词或意图匹配，只校验精确工具名和参数 Schema。工具描述应清楚区分动作、对象和参数。
- **连接是全局共享的**：改配置会影响所有正在使用 MCP 的会话，建议在空闲时操作。

## 7. 相关配置

- 配置文件：`/opt/mcp_cilent/mcp.json`（可用环境变量 `MCP_CONFIG` 改路径）
- 服务重启时按 `mcp.json` 重建连接池；`disabled: true` 的条目跳过不连
