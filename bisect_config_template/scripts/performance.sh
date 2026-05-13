#!/bin/bash
# 用户自己填写性能验证脚本内容

aisbench performance \
    --url http://127.0.0.1:8000/v1/completions \
    --model llama-7b \
    --concurrency 8

# 输出包含关键词：TTFT: 123.4 ms
# 工具自动从benchmark日志提取并校验阈值