#!/bin/bash
# ============================================================
# 精度验证脚本
# ============================================================
# 环境变量（由工具自动注入）：
#   VLLM_HOST    - 服务地址
#   VLLM_PORT    - 服务端口
#   BISECT_REPO_DIR - 仓库路径

# ── 调用精度验证工具（如aisbench） ──
aisbench accuracy \
    --url http://${VLLM_HOST}:${VLLM_PORT}/v1/completions \
    --model llama-7b \
    --dataset /data/datasets/eval.jsonl \
    --output /tmp/accuracy_result.json \
    --timeout 600

# ── 结果必须写入JSON文件（供工具解析） ──
# 格式示例：
# {
#   "accuracy": 0.96,
#   "pass_rate": 0.92,
#   "total_samples": 1000
# }

echo "Accuracy validation completed"