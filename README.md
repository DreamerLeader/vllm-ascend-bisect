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

### 方式一: 场景配置 — 脚本模式 (推荐)

实际场景中，拉起 vLLM、跑精度/性能验证都是独立的 shell 脚本。开发只需在 YAML 里填各阶段的脚本路径：

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --cmd "python test_runner.py --config scenes/my_scene.yaml"
```

YAML 配置示例（脚本模式，完整模板见 `scenes/example_script_mode.yaml`）：

```yaml
name: glm5_w4a8_validation
description: "GLM5-w4a8 16卡推理验证"

# 安装脚本 (每个 commit 都会重新安装, 日志中会打印安装过程)
setup_script: ./scripts/setup.sh

# vLLM 服务 — 通过脚本启停
server:
  start_script: ./scripts/start_vllm.sh   # 启动脚本 (内含环境变量和参数, 需保持前台运行)
  stop_script: ./scripts/stop_vllm.sh     # 停止脚本 (可选, 默认 kill 进程)

  # 通过日志关键字判断服务就绪 (推荐)
  ready_keyword: "Uvicorn running on"

  host: 0.0.0.0
  port: 8077
  ready_timeout: 600                      # 大模型加载慢

# 验证任务 — 通过脚本执行
benchmarks:
  - name: accuracy_check
    script: ./scripts/run_accuracy.sh      # 精度验证脚本
    timeout: 600
    result_file: /tmp/accuracy_result.json
    check:
      accuracy: ">= 0.95"

  - name: performance_check
    script: ./scripts/run_performance.sh   # 性能验证脚本
    timeout: 600
    result_file: /tmp/perf_result.json
    check:
      throughput: ">= 100"
      latency_p99: "<= 200"

# 清理脚本
cleanup_script: ./scripts/cleanup.sh
```

### 方式二: 场景配置 — 命令模式

不想单独写脚本文件时，也可以直接在 YAML 里写命令：

```yaml
name: llama_7b_accuracy
setup_cmd: "pip install -e . --no-deps -q"

server:
  start_cmd: >
    python -m vllm.entrypoints.openai.api_server
    --model /data/models/llama-7b
    --device npu --port 8000
  port: 8000
  ready_timeout: 300

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
      accuracy: ">= 0.95"
```

### 方式三: 简单脚本

不需要场景配置时，直接传测试脚本：

```bash
python bisect_pr.py \
    --repo-dir ./vllm-ascend \
    --good v0.7.0 --bad main \
    --test-script ./my_test.sh \
    --setup-cmd "pip install -e . --no-deps -q"
```

### 方式四: 内联命令

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
  │   setup     │  重新安装 vllm-ascend (日志打印安装过程)
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ start vLLM  │  bash 拉起脚本, 实时打印服务日志
  │             │  检测日志关键字 / HTTP 健康检查 → 判定就绪
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  benchmark  │  精度验证脚本/命令
  │  benchmark  │  性能验证脚本/命令
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │   check     │  对比结果与阈值: accuracy >= 0.95 ?
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  cleanup    │  停止服务, 清理脚本/命令
  └─────────────┘
```

### 配置字段说明

每个阶段都支持 `cmd`（内联命令）和 `script`（脚本文件路径）两种方式，二选一。脚本文件支持相对路径（基于仓库目录解析），工具自动设置执行权限并通过 `bash` 执行。

| 字段 | 说明 |
|------|------|
| `name` | 场景名称 |
| `setup_cmd` / `setup_script` | 每个 commit 的安装命令/脚本 |
| `server.start_cmd` / `server.start_script` | vLLM 启动命令/脚本 (脚本内含环境变量和参数) |
| `server.stop_cmd` / `server.stop_script` | vLLM 停止命令/脚本 (可选, 默认 kill 进程) |
| `server.ready_keyword` | 日志关键字检测就绪 (推荐, 如 `"Uvicorn running on"`) |
| `server.health_endpoint` | HTTP 健康检查路径 (与 ready_keyword 二选一, 默认 `/health`) |
| `server.port` | 服务端口 |
| `server.ready_timeout` | 等待就绪超时(秒) |
| `server.env` | 额外环境变量 (如 `ASCEND_RT_VISIBLE_DEVICES`) |
| `benchmarks[].cmd` / `benchmarks[].script` | 验证命令/脚本 |
| `benchmarks[].result_file` | 结果 JSON 文件路径 |
| `benchmarks[].check` | 校验规则 |
| `cleanup_cmd` / `cleanup_script` | 清理命令/脚本 |

### 服务就绪检测 (二选一)

**方式一: 日志关键字 (推荐)**

实时读取 vLLM 进程的 stdout，逐行打印日志，检测到关键字即判定就绪：

```yaml
server:
  start_script: ./start_vllm.sh
  ready_keyword: "Uvicorn running on"   # vLLM 启动完成后会打印此日志
  ready_timeout: 600
```

**方式二: HTTP 健康检查**

轮询 HTTP 接口，返回 200 即就绪：

```yaml
server:
  start_script: ./start_vllm.sh
  health_endpoint: /health              # 默认值
  ready_timeout: 300
```

### 每次安装的日志

因为二分查找每个 commit 都需要重新安装 vllm-ascend，工具会详细打印：
- 当前测试的 commit SHA
- 安装命令/脚本的执行过程（输出最后 30 行）
- 安装耗时
- 安装失败时的 stderr 详情

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

benchmark 脚本/命令中可使用以下环境变量：

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
| `scenes/example_script_mode.yaml` | 全脚本模式 (推荐, 贴近实际使用) |
| `scenes/example_accuracy.yaml` | 单卡精度+性能验证 (命令模式) |
| `scenes/example_tp2.yaml` | 多卡 TP=2 验证 |
| `scenes/example_multimodel.yaml` | 纯脚本模式 (不管理服务) |

## 依赖

```bash
pip install pyyaml             # 场景配置解析
pip install anthropic          # Agent 分析 (可选)
gh auth login                  # GitHub CLI (Agent 分析需要)
```
