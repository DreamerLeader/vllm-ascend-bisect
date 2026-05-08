#!/bin/bash
# 用户自己填写性能验证脚本内容

mkdir -p output

aisbench performance \
    --url http://127.0.0.1:8000/v1/completions \
    --model llama-7b \
    --concurrency 8 \
    --num-requests 100 \
    --output ./output/perf_result.json