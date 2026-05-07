#!/bin/bash
# ============================================================
# 安装脚本 - 每个commit都会重新执行
# ============================================================
# 环境变量：
#   BISECT_REPO_DIR - 仓库路径
#   BISECT_COMMIT   - 当前commit SHA

set -e

cd "${BISECT_REPO_DIR:-.}"

echo "============================================"
echo "Installing vllm-ascend at commit: $(git rev-parse --short HEAD)"
echo "============================================"

# ── 方式1: 快速安装（适合Python代码变更） ──
pip install -e . --no-deps -q

# ── 方式2: 全量安装（适合C++扩展变更） ──
# pip install -e .

# ── 方式3: Docker镜像构建（如果使用Docker） ──
# docker build -t vllm-ascend:bisect .

echo ">>> Setup completed"