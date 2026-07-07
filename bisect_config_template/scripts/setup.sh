#!/bin/bash
# 用户自己填写安装脚本内容

cd /home/user/vllm-ascend

# 默认情况下，工具会在执行本脚本前自动安装配套vllm版本
# 如需关闭，设置 bisect_options.install_vllm: false
pip install -e . --no-deps -q

# 如果需要额外安装步骤，在这里添加
