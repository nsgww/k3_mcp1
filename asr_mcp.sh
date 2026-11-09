#!/bin/bash
# /home/user/scripts/start_asr_llm_ws.sh
set -euo pipefail

# 根据你的开发板实际路径修改
PROJECT_DIR="/home/bianbu/Desktop/NLP2"
PYTHON="/home/bianbu/Desktop/.venv/bin/python3"
MAIN_SCRIPT="$PROJECT_DIR/09_asr_llm_ws.py"

# 关键环境变量（按需修改）
export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"
export KWS_MODEL_DIR="/var/lib/sm-sdk/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
export TTS_MODEL_DIR="/var/lib/sm-sdk/models/tts/matcha-icefall-zh-en"
export TTS_BACKEND="matcha"
export MCP_CONFIG="$PROJECT_DIR/mcp.json"
export LLM_BACKEND="llama-server"
export LLM_BASE_URL="http://127.0.0.1:8080"
export LLM_REQUEST_TIMEOUT="300"
export MCP_TOOL_LLM="Qwen3-30B-A3B-Instruct-2507-Q4_0.gguf"
export MCP_TOOL_STYLE="native"
# export MCP_SSE_URL="http://localhost:8000/sse"
# export MODEL_WARMUP="1"
# export WAKE_ACK_TEXT="我在"

# 日志
LOG_DIR="/home/bianbu/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/asr_llm_ws_$(date +%Y%m%d).log"

cd "$PROJECT_DIR"
exec "$PYTHON" "$MAIN_SCRIPT" >> "$LOG_FILE" 2>&1