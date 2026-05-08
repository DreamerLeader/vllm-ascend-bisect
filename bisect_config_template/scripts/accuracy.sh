#!/bin/bash
# 用户自己填写精度验证脚本内容

mkdir -p output

aisbench accuracy \
    --url http://127.0.0.1:8000/v1/completions \
    --model llama-7b \
    --dataset /data/datasets/eval.jsonl \
    --output ./output/accuracy_result.json