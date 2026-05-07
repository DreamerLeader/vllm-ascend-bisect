#!/bin/bash
# ============================================================
# 停止服务脚本 - 清理vLLM服务进程
# ============================================================

# ── 方式1: 通过PID文件停止（如果启动脚本写了PID） ──
# if [ -f /tmp/vllm.pid ]; then
#     kill $(cat /tmp/vllm.pid)
#     rm -f /tmp/vllm.pid
# fi

# ── 方式2: 通过进程名停止 ──
pkill -f "vllm.entrypoints.openai.api_server"

# ── 方式3: 清理临时文件 ──
rm -f /tmp/accuracy_result.json /tmp/perf_result.json

echo "Service stopped"