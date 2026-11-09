# LLM 原生 MCP 路由与板端验收

## 架构边界

生产链路为：

`PCM → KWS/VAD → ASR → LLM(tools schema) → Host 校验 → MCP tools/call → TTS`

KWS 只负责唤醒。MCP 工具选择全部由 LLM 完成，Host 不维护意图、关键词、设备映射或 description 纠错规则，只负责：

1. 解析 llama-server/OpenAI 或 Ollama native tool_calls，也兼容文本 `<tool_call>`；
2. 要求工具名与 `tools/list` 完全一致；
3. 用工具原始 inputSchema 校验 arguments；
4. 限制 Agent 轮次和 MCP 超时；
5. 回灌工具结果或结构化错误。

## MCP Server 描述要求

- name 在所有服务器间唯一、稳定且尽量简短；
- description 明确“动作 + 对象 + 使用条件”，相似工具必须写出区别；
- 参数提供 type、description、required、enum、范围和 additionalProperties；
- 不要依赖 Host 猜测缺失参数或修复拼错的工具名。

工具表变更后发送 `{"type":"mcp_refresh"}`，并重跑板端路由语料。

## 配置

```bash
export LLM_BACKEND=llama-server
export LLM_BASE_URL=http://127.0.0.1:8080
export LLM_REQUEST_TIMEOUT=300
export MCP_TOOL_LLM=Qwen3-30B-A3B-Instruct-2507-Q4_0.gguf
export MCP_TOOL_STYLE=native
export MCP_TOOL_NUM_PREDICT=64
export MCP_TEXT_TOOL_NUM_PREDICT=48
export MCP_MAX_TOOL_ROUNDS=3
export MCP_REQUEST_TIMEOUT=15
export KWS_MODEL_DIR=/var/lib/sm-sdk/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
export TTS_MODEL_DIR=/var/lib/sm-sdk/models/tts/matcha-icefall-zh-en
```

`LLM_BACKEND=ollama` 可切回旧后端；llama-server 使用 OpenAI 兼容
`/v1/chat/completions`。服务定义见 `deploy/llama-server.service`，模型通过
`--jinja` 启用原生工具调用模板。增加 token 预算只用于解决协议截断，
不应通过模糊匹配掩盖模型错误。

```bash
sudo cp deploy/llama-server.service /etc/systemd/system/
sudo cp deploy/mcp-cilent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
curl http://127.0.0.1:8080/health
sudo systemctl restart mcp-cilent
```

## 编码环境闭环

```bash
python -m compileall 09_asr_llm_ws.py spacemit_llm tests
python -m unittest tests.test_llm_tool_agent -v
```

该层不需要模型、ASR、MCP Server 或硬件，验证 Host 不越权参与语义路由。

## 开发板路由评测

先验证真实 llama-server，mock executor 不控制设备：

```bash
python tests/test_tool_routing_eval.py --mode mock \
  --report tests/reports/routing-mock.json
```

再验证只读真实 MCP：

```bash
python tests/test_tool_routing_eval.py --mode live \
  --report tests/reports/routing-live-readonly.json
```

现场允许控制后运行副作用闭环：

```bash
python tests/test_tool_routing_eval.py --mode live --allow-side-effects \
  --report tests/reports/routing-live.json
```

带 `side_effect` 的用例会保存初态、设置相反基线、由 LLM 选择控制工具、查询终态并在 `finally` 恢复初态。

## 完整语音验收

```bash
python tests/gen_test_wavs.py --out tests/test_wavs --noise
python tests/test_09_boundary.py ws://127.0.0.1:8060/stream \
  --samples tests/test_wavs --stress-connect 20 --endurance 100
```

办公室灯用例通过真实语音查询初态、控制、查询终态和恢复，且断言具体工具名与参数。

## 通过标准

- 本地单元测试 100%；
- 板端 mock 路由准确率不低于命令行 `--min-accuracy`（默认 80%）；
- 否定句、闲聊、讨论类用例不产生工具调用；
- 真实控制必须通过状态回读并恢复；
- 完整语音测试无错误，长稳期间无持续内存增长或重连风暴；
- 记录模型版本、工具快照、环境变量、逐例调用、耗时和失败原因。

详细矩阵和排障流程见项目 Skill：`.cursor/skills/llm-native-mcp-routing/`。
