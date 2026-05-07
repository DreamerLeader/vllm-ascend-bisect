#!/bin/bash
# ============================================================
# 安装依赖脚本
# ============================================================

echo "安装vllm-ascend二分定位工具依赖..."

pip3 install pyyaml flask requests

echo "\n依赖安装完成！"
echo "\n使用方法："
echo "  cp -r bisect_config_template my_bisect"
echo "  cd my_bisect"
echo "  python3 ../bisect_tool.py --config config_single_node.yaml"