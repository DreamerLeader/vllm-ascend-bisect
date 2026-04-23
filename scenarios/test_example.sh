#!/bin/bash
# Example test script for bisect.
#
# Rules:
#   - Exit 0  → test PASSED (this commit is good)
#   - Exit 1  → test FAILED (this commit is bad)
#
# Environment variable available:
#   BISECT_REPO_DIR — path to the vllm-ascend repo
#
# You can put ANY validation logic here:
#   - Run a pytest test case
#   - Start vLLM server, send request, check output
#   - Run a benchmark and check if throughput meets threshold
#   - Check if a specific error appears in logs

set -e

cd "${BISECT_REPO_DIR:-.}"

# ──────────────────────────────────────────────────────────────────────
# Example 1: Run a specific pytest test
# ──────────────────────────────────────────────────────────────────────
# python -m pytest tests/e2e/test_llama.py::test_inference -x -v

# ──────────────────────────────────────────────────────────────────────
# Example 2: Start server, send request, validate response
# ──────────────────────────────────────────────────────────────────────
# # Start vLLM server in background
# python -m vllm.entrypoints.openai.api_server \
#     --model /models/llama-7b \
#     --device npu \
#     --port 8000 &
# SERVER_PID=$!
#
# # Wait for server to be ready
# for i in $(seq 1 60); do
#     if curl -s http://localhost:8000/health | grep -q "ok"; then
#         break
#     fi
#     sleep 2
# done
#
# # Send test request
# RESPONSE=$(curl -s http://localhost:8000/v1/completions \
#     -H "Content-Type: application/json" \
#     -d '{"model": "llama-7b", "prompt": "Hello", "max_tokens": 10}')
#
# # Cleanup
# kill $SERVER_PID 2>/dev/null || true
#
# # Validate
# echo "$RESPONSE" | python -c "
# import sys, json
# r = json.load(sys.stdin)
# assert 'choices' in r, 'No choices in response'
# assert len(r['choices'][0]['text']) > 0, 'Empty response'
# print('PASS:', r['choices'][0]['text'][:50])
# "

# ──────────────────────────────────────────────────────────────────────
# Example 3: Benchmark throughput check
# ──────────────────────────────────────────────────────────────────────
# THROUGHPUT=$(python benchmarks/benchmark_throughput.py \
#     --model /models/llama-7b \
#     --device npu \
#     --num-prompts 100 \
#     2>&1 | grep "Throughput:" | awk '{print $2}')
#
# python -c "
# throughput = float('${THROUGHPUT}')
# threshold = 50.0  # tokens/sec
# assert throughput >= threshold, f'Throughput {throughput} < {threshold}'
# print(f'PASS: throughput={throughput:.1f} tokens/sec')
# "

echo "Please replace this with your actual test logic"
exit 1
