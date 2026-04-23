# vllm-ascend PR 二分定位工具

针对 [vllm-ascend](https://github.com/vllm-project/vllm-ascend) 仓库，按 commit (PR) 粒度做二分查找，定位引入问题的具体 PR。

## 工作流程

```
开发提供:
  1. 一个 good commit (已知正常)
  2. 一个 bad commit  (已知异常)
  3. 一个测试脚本    (exit 0=通过, 非0=失败)

工具自动:
  good ─── PR#1 ─── PR#2 ─── PR#3 ─── PR#4 ─── PR#5 ─── bad
                              ↑
                           二分取中
                        checkout → setup → 运行脚本
                        pass → 右移    fail → 左移
                        
  O(log N) 步定位 → 输出问题 PR
```

## 快速开始

### 方式一: 开发提供脚本文件

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 \
    --bad main \
    --test-script ./my_test.sh
```

### 方式二: 开发提供内联命令

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good abc1234 \
    --bad main \
    --cmd "python -m pytest tests/e2e/test_llama.py -x"
```

### 带环境安装

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --test-script ./my_test.sh \
    --setup-cmd "pip install -e . --no-deps -q"
```

### 二分 + Agent 自动分析

```bash
python run_bisect.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --test-script ./my_test.sh \
    --setup-cmd "pip install -e . --no-deps -q" \
    --analyze \
    --error-description "LLaMA推理在910B上输出NaN"
```

## 开发如何编写测试脚本

规则很简单: **exit 0 = 通过, exit 非0 = 失败**

环境变量 `BISECT_REPO_DIR` 指向仓库目录, `BISECT_COMMIT` 是当前测试的 commit SHA。

```bash
#!/bin/bash
# my_test.sh — 开发提供的测试脚本
set -e
cd "$BISECT_REPO_DIR"
python -m pytest tests/e2e/test_llama.py -x -v
```

更多示例见 `scenarios/test_example.sh`。

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
| `bisect_logs/` | 每个 commit 的测试日志 (`{sha}_{pass/fail}.log`) |
| `report.md` | Agent 分析报告 (需 `--analyze`) |

## 特殊情况处理

- **Setup 失败 (编译不过等)**: 自动标记为 `skip`, 尝试相邻 commit
- **测试超时**: 标记为 `fail`
- **Squash merge / Merge commit**: 都支持, 用 `--first-parent` 保证一个 commit 对应一个 PR

## 批量场景

多个测试场景可以用 batch 模式:

```bash
python agent_analyzer.py batch --config batch_config.json
```

配置格式见 `batch_config_example.json`。

## 依赖

- Python 3.10+
- git
- `gh` CLI (Agent 分析需要, `brew install gh && gh auth login`)
- `pip install anthropic` (Agent 分析需要)
- `ANTHROPIC_API_KEY` 环境变量 (Agent 分析需要)
