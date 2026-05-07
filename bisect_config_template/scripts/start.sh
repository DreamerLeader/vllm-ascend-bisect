#!/bin/bash
# ============================================================
# 单机混部启动脚本 - 拉起vLLM服务
# ============================================================
# 注意：
#   1. 必须保持前台运行（不要用nohup或&）
#   2. 环境变量 BISECT_REPO_DIR 会自动注入
#   3. 根据实际场景修改启动参数

cd "${BISECT_REPO_DIR:-.}"

# ── 单机混部启动参数 ──
python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000 \
    --trust-remote-code \
    # 其他参数根据实际场景添加：
    # --tensor-parallel-size 2
    # --gpu-memory-utilization 0.9
    # --max-model-len 2048
    
# 注意：脚本会作为后台进程启动，工具会管理进程生命周期