#!/bin/bash
# 用户自己填写d服务启动脚本内容

cd /home/user/vllm-ascend

python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000 \
    --role d