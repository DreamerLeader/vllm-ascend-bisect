#!/bin/bash
# ============================================================
# 性能验证脚本
# ============================================================
# 环境变量（由工具自动注入）：
#   VLLM_HOST    - 服务地址
#   VLLM_PORT    - 服务端口

# ── 调用性能验证工具（如aisbench） ──
aisbench performance \
    --url http://${VLLM_HOST}:${VLLM_PORT}/v1/completions \
    --model llama-7b \
    --concurrency 8 \
    --num-requests 100 \
    --output /tmp/perf_result.json \
    --timeout 600

# ── 结果必须写入JSON文件（供工具解析） ──
# 格式示例：
# {
#   "throughput": 120.5,
#   "latency_p99": 180.3,
#   "latency_mean": 95.2
# }

echo "Performance validation completed"