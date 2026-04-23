# vllm-ascend PR 二分定位工具

针对 [vllm-ascend](https://github.com/vllm-project/vllm-ascend) 仓库，按 commit (PR) 粒度做二分查找，定位引入问题的具体 PR。

## 工作流程

```
开发提供:
  1. 一个 good commit (已知正常)
  2. 一个 bad commit  (已知异常)
  3. 测试脚本 或 场景配置 YAML

工具自动:
  good ─── PR#1 ─── PR#2 ─── PR#3 ─── PR#4 ─── PR#5 ─── bad
                              ↑
                           二分取中
              checkout → setup → 拉起服务 → 跑 aisbench → 判断结果
                        pass → 右移    fail → 左移
                        
  O(log N) 步定位 → 输出问题 PR
```

## 快速开始

### 方式一: 场景配置 (推荐)

开发只需写一个 YAML 配置，描述：服务怎么启、跑什么验证、阈值是多少。

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --cmd "python test_runner.py --config scenes/my_scene.yaml"
```

YAML 配置示例（完整模板见 `scenes/` 目录）：

```yaml
name: llama_7b_accuracy
description: "LLaMA-7B 精度+性能验证"

# 每个 commit 切换后先安装
setup_cmd: "pip install -e . --no-deps -q"

# vLLM 服务配置 — 工具自动管理启停
server:
  start_cmd: >
    python -m vllm.entrypoints.openai.api_server
    --model /data/models/llama-7b
    --device npu --port 8000
  port: 8000
  ready_timeout: 300

# 验证任务 — 服务就绪后依次运行
benchmarks:
  - name: accuracy
    cmd: >
      aisbench accuracy
      --url $VLLM_BASE_URL/v1/completions
      --model llama-7b
      --dataset /data/datasets/eval.jsonl
      --output /tmp/acc.json
    result_file: /tmp/acc.json
    check:
      accuracy: ">= 0.95"      # 精度 >= 95% 才算通过

  - name: performance
    cmd: >
      aisbench performance
      --url $VLLM_BASE_URL/v1/completions
      --model llama-7b
      --concurrency 16
      --output /tmp/perf.json
    result_file: /tmp/perf.json
    check:
      throughput: ">= 100"     # 吞吐 >= 100 tokens/s
      latency_p99: "<= 200"   # P99 延迟 <= 200ms
```

### 方式二: 简单脚本

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --test-script ./my_test.sh \
    --setup-cmd "pip install -e . --no-deps -q"
```

### 方式三: 内联命令

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good abc1234 --bad main \
    --cmd "python -m pytest tests/e2e/test_llama.py -x"
```

## 场景配置详解 (test_runner.py)

`test_runner.py` 管理完整的多阶段验证流程：

```
  ┌─────────────┐
  │   setup     │  pip install -e . --no-deps
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ start vLLM  │  拉起服务, 轮询 /health 等待就绪
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  benchmark  │  运行 aisbench 精度验证
  │  benchmark  │  运行 aisbench 性能验证
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │   check     │  对比结果与阈值: accuracy >= 0.95 ?
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  cleanup    │  停止服务, 清理临时文件
  └─────────────┘
```

### 配置字段说明

| 字段 | 说明 |
|------|------|
| `name` | 场景名称 |
| `setup_cmd` | 每个 commit 的安装命令 |
| `server.start_cmd` | vLLM 启动命令 |
| `server.port` | 服务端口 |
| `server.ready_timeout` | 等待就绪超时(秒) |
| `server.env` | 额外环境变量 (如 `ASCEND_RT_VISIBLE_DEVICES`) |
| `benchmarks[].cmd` | 验证命令 (可用 `$VLLM_BASE_URL`) |
| `benchmarks[].result_file` | 结果 JSON 文件路径 |
| `benchmarks[].check` | 校验规则 |
| `cleanup_cmd` | 清理命令 |

### 校验规则语法

```yaml
check:
  accuracy: ">= 0.95"       # 大于等于
  throughput: ">= 100"      # 大于等于
  latency_p99: "<= 200"     # 小于等于
  status: "== success"      # 等于
  error_count: "< 5"        # 小于
```

支持嵌套字段: `result.metrics.accuracy: ">= 0.95"`

### 结果提取方式 (三选一)

1. **result_file**: 指定结果 JSON 文件路径
2. **result_cmd**: 运行命令, 取 stdout 作为 JSON
3. **自动**: 从 benchmark stdout 最后一行尝试解析 JSON

### 环境变量

benchmark 命令中可使用以下环境变量：

| 变量 | 值 |
|------|------|
| `$VLLM_BASE_URL` | `http://127.0.0.1:8000` |
| `$VLLM_HOST` | `127.0.0.1` |
| `$VLLM_PORT` | `8000` |
| `$BISECT_REPO_DIR` | 仓库目录 |

## 二分 + Agent 自动分析

```bash
python run_bisect.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --cmd "python test_runner.py --config scenes/my_scene.yaml" \
    --analyze \
    --error-description "LLaMA推理在910B上精度下降"
```

## 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `--repo-dir` | 是 | vllm-ascend 本地仓库路径 |
| `--good` | 是 | 已知正常的 commit/tag |
| `--bad` | 是 | 已知异常的 commit/tag |
| `--test-script` | 二选一 | 测试脚本文件路径 |
| `--cmd` | 二选一 | 内联测试命令 |
| `--setup-script` | 否 | 每轮的环境安装脚本 |
| `--setup-cmd` | 否 | 每轮的环境安装命令 |
| `--timeout` | 否 | 每轮超时秒数 (默认 600) |
| `--skip-verify` | 否 | 跳过 good/bad 验证 |
| `--log-dir` | 否 | 日志目录 (默认 bisect_logs) |
| `--output` | 否 | 结果 JSON (默认 bisect_result.json) |

## 输出

| 文件 | 内容 |
|------|------|
| `bisect_result.json` | 定位结果: commit SHA, PR号, 二分历史 |
| `bisect_logs/` | 每个 commit 的测试日志 |
| `report.md` | Agent 分析报告 (需 `--analyze`) |

## 特殊情况处理

- **Setup/编译失败**: 自动 `skip`, 尝试相邻 commit
- **服务启动失败**: 标记为 `fail`
- **测试超时**: 标记为 `fail`
- **Squash merge / Merge commit**: 都支持

## 场景模板

| 文件 | 适用场景 |
|------|----------|
| `scenes/example_accuracy.yaml` | 单卡精度+性能验证 |
| `scenes/example_tp2.yaml` | 多卡 TP=2 验证 |
| `scenes/example_multimodel.yaml` | 纯脚本模式 (不管理服务) |

## 依赖

```bash
pip install pyyaml             # 场景配置解析
pip install anthropic          # Agent 分析 (可选)
gh auth login                  # GitHub CLI (Agent 分析需要)
```
