#!/bin/bash
# ============================================================
# p服务启动脚本 - pd分离场景（p节点）
# ============================================================
# 注意：
#   1. 必须保持前台运行（不要用nohup或&）
#   2. 根据实际场景修改启动参数
#   3. p节点专属参数（如分布式通信配置）

cd "${BISECT_REPO_DIR:-.}"

# ── p节点启动参数 ──
python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000 \
    --role p \
    --trust-remote-code \
    # p节点专属参数：
    # --distributed-executor-backend ray
    # --pipeline-parallel-size 2