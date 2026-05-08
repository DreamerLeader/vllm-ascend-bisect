#!/bin/bash
# 用户自己填写精度验证脚本内容

aisbench accuracy \
    --url http://127.0.0.1:8000/v1/completions \
    --model llama-7b \
    --dataset /data/datasets/eval.jsonl

# aisbench自动创建目录结构：
# outputs/default/20260508_171903/results/vllm-api-stream-chat/xxxx.json