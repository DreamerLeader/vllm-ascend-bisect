#!/bin/bash
# Example setup script — runs before each test iteration.
# This is called at each bisect step after checking out a new commit.
#
# Environment variable available:
#   BISECT_REPO_DIR — path to the vllm-ascend repo
#
# Exit 0 on success, non-0 to mark this commit as "bad" (setup failure).

set -e

cd "${BISECT_REPO_DIR:-.}"

echo ">>> Installing vllm-ascend at commit $(git rev-parse --short HEAD) ..."

# Option 1: pip install in editable mode (fast, good for Python-only changes)
pip install -e . --no-deps -q 2>&1

# Option 2: Full rebuild (if C++ extensions changed)
# pip install -e . -q 2>&1

# Option 3: Docker rebuild (if you use Docker-based testing)
# docker build -t vllm-ascend:bisect -f Dockerfile .

echo ">>> Setup complete"
