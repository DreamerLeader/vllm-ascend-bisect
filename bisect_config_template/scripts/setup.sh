#!/bin/bash
# 用户自己填写安装脚本内容

cd /home/user/vllm-ascend

# vllm版本会自动配套安装（工具从docs/source/conf.py读取）
# 用户只需安装vllm-ascend
pip install -e . --no-deps -q

# 如果需要额外安装步骤，在这里添加