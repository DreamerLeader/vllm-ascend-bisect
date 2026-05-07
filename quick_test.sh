#!/bin/bash
# ============================================================
# 快速测试脚本 - 验证工具是否可用
# ============================================================

echo "============================================"
echo "vllm-ascend二分定位工具 - 快速测试"
echo "============================================"

# 检查依赖
echo "\n[1] 检查依赖..."
python3 -c "import yaml, flask, requests" 2>&1
if [ $? -eq 0 ]; then
    echo "✓ 依赖已安装"
else
    echo "✗ 缺少依赖，请安装："
    echo "  pip install pyyaml flask requests"
    exit 1
fi

# 检查配置文件
echo "\n[2] 检查配置文件..."
if [ -f "bisect_config_template/config_single_node.yaml" ]; then
    echo "✓ 单机混部配置文件存在"
else
    echo "✗ 配置文件不存在"
    exit 1
fi

# 检查脚本
echo "\n[3] 检查脚本文件..."
scripts=(
    "bisect_config_template/scripts/setup.sh"
    "bisect_config_template/scripts/start.sh"
    "bisect_config_template/scripts/stop.sh"
    "bisect_config_template/scripts/accuracy.sh"
)

for script in "${scripts[@]}"; do
    if [ -f "$script" ]; then
        echo "  ✓ $script"
    else
        echo "  ✗ $script 不存在"
        exit 1
    fi
done

# 检查Agent服务
echo "\n[4] 检查Agent服务..."
if [ -f "bisect_config_template/agent_server.py" ]; then
    echo "✓ agent_server.py 存在"
else
    echo "✗ agent_server.py 不存在"
    exit 1
fi

# 检查主工具
echo "\n[5] 检查主工具..."
if [ -f "bisect_tool.py" ]; then
    echo "✓ bisect_tool.py 存在"
else
    echo "✗ bisect_tool.py 不存在"
    exit 1
fi

echo "\n============================================"
echo "✓ 所有检查通过！"
echo "============================================"
echo "\n使用方法："
echo "  1. 复制配置文件夹："
echo "     cp -r bisect_config_template my_bisect"
echo "     cd my_bisect"
echo "\n  2. 修改脚本（根据实际场景）："
echo "     vim scripts/start.sh"
echo "     vim scripts/accuracy.sh"
echo "\n  3. 运行工具："
echo "     python3 bisect_tool.py --config config_single_node.yaml"
echo "\n完整使用说明请查看："
echo "  bisect_config_template/README.md"
echo "============================================"