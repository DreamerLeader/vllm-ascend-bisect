# 使用流程详细说明

## 新功能概述

### 功能1：vllm版本自动配套
- checkout后自动读取 `docs/source/conf.py`
- 提取 `pip_vllm_version`（如 `0.17.0`）
- setup时自动安装：`pip install vllm==0.17.0` + `pip install -e . --no-deps`

### 功能2：TTFT性能验证
- 从benchmark日志文件提取关键词 `TTFT: 123.4 ms`
- YAML配置：`check: { ttft: "<= 200" }`（毫秒阈值）
- 无需result_file，工具自动从日志提取

---

## 完整使用流程（单机混部）

### 步骤1：准备配置文件夹

```bash
# 复制配置模板
cp -r bisect_config_template my_bisect
cd my_bisect
```

### 步骤2：配置代理（可选）

**如果需要代理才能安装vllm**：

```bash
vim proxy_env
```

填写代理配置：
```bash
export http_proxy=http://proxy.company.com:8080
export https_proxy=http://proxy.company.com:8080
export no_proxy=localhost,127.0.0.1
```

**说明**：
- Setup时：自动设置代理（安装vllm和vllm-ascend）
- Setup完成后：自动unset代理
- 启动服务和验证时：无代理

### 步骤3：修改脚本（根据实际场景）

#### A. setup.sh（安装脚本）

**改动前**（用户需手动处理vllm版本）：
```bash
#!/bin/bash
cd /home/user/vllm-ascend

# 用户需要知道vllm版本
pip install vllm==0.17.0  
pip install -e . --no-deps -q
```

**改动后**（工具自动处理vllm版本）：
```bash
#!/bin/bash
cd /home/user/vllm-ascend

# 工具自动安装配套的vllm版本
# 用户只需安装vllm-ascend
pip install -e . --no-deps -q
```

#### B. start.sh（启动脚本）

```bash
#!/bin/bash
cd /home/user/vllm-ascend

python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000
```

#### C. accuracy.sh（精度验证脚本）

```bash
#!/bin/bash

aisbench accuracy \
    --url http://127.0.0.1:8000/v1/completions \
    --model llama-7b \
    --dataset /data/datasets/eval.jsonl

# 结果自动保存到：
# outputs/default/20260508_171903/results/vllm-api-stream-chat/xxx.json
```

#### D. performance.sh（性能验证脚本）

```bash
#!/bin/bash

aisbench performance \
    --url http://127.0.0.1:8000/v1/completions \
    --model llama-7b \
    --concurrency 8

# 输出包含：TTFT: 123.4 ms
# 工具自动从日志提取并校验阈值
```

### 步骤4：修改YAML配置

**config_single_node.yaml**：

```yaml
# 二分范围
bisect_range:
  good_commit: "v0.7.0"    # 已知正常
  bad_commit: "main"       # 已知异常

# 二分参数
bisect_options:
  skip_initial_verification: false  # true=确认good/bad边界可靠，直接从中间commit开始
  vllm_version: null       # null=自动读取，或手动指定如"0.17.0"

# 验证任务
benchmarks:
  - name: accuracy_check
    enable: true            # false则跳过
    script: ./scripts/accuracy.sh
    timeout: 600
    result_file: ./outputs  # 自动查找最新时间文件夹
    check:
      accuracy: ">= 0.95"
      
  - name: performance_check
    enable: true            # false则跳过（只验证精度时设为false）
    script: ./scripts/performance.sh
    timeout: 600
    # 无result_file，从日志提取关键词
    check:
      ttft: "<= 200"        # TTFT阈值（毫秒）
```

### 步骤5：一键运行

```bash
python3 ../bisect_tool.py --config config_single_node.yaml
```

---

## 工具自动执行流程

```
┌─────────────────────────────────────────────────────────┐
│ 1. checkout commit                                       │
│    └─→ git checkout {commit}                             │
│    └─→ 自动读取 docs/source/conf.py                      │
│    └→ 提取 pip_vllm_version: "0.17.0"                    │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 2. setup（自动配套vllm）                                  │
│    └→ 设置代理（如果有proxy_env）                         │
│    └→ pip install vllm==0.17.0 -q  ← 工具自动注入        │
│    └→ bash setup.sh（安装vllm-ascend）                   │
│    └→ unset代理                                          │
│    └→ 保存日志：bisect_logs/setup_{commit}.log           │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 3. start vLLM服务                                         │
│    └→ bash start.sh                                      │
│    └→ 等待健康检查：http://127.0.0.1:8000/health         │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 4. accuracy_check（精度验证）                             │
│    └→ bash accuracy.sh                                   │
│    └→ 自动查找：outputs/default/{时间}/.../*.json        │
│    └→ 提取accuracy字段                                    │
│    └→ 校验：accuracy >= 0.95                              │
│    └→ 保存日志：bisect_logs/benchmark_accuracy_*.log     │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 5. performance_check（性能验证）                          │
│    └→ bash performance.sh                                │
│    └→ 保存日志：bisect_logs/benchmark_performance_*.log  │
│    └→ 从日志提取：TTFT: 123.4 ms                          │
│    └→ 校验：ttft <= 200                                   │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 6. stop服务                                               │
│    └→ bash stop.sh                                        │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 7. 二分决策                                               │
│    └→ pass → 右移（测试后面的commit）                     │
│    └→ fail → 左移（测试前面的commit）                     │
│    └→ O(log N)步定位问题PR                                │
└─────────────────────────────────────────────────────────┘
```

---

## 终端输出示例

### Setup阶段

```bash
============================================================
Auto-detected vllm version: 0.17.0
  Source: docs/source/conf.py
============================================================
Loading proxy configuration...
  File: bisect_config_template/proxy_env
============================================================
Proxy config loaded (setup will use proxy)

============================================================
Running setup script at commit abc1234
  Full logs: bisect_logs/setup_abc1234.log
============================================================
[pip install vllm==0.17.0 output]
[setup.sh output]
Still running... (30s elapsed, 570s remaining)
============================================================
✓ Setup SUCCESS (45.3s)
  Logs: bisect_logs/setup_abc1234.log (180 lines)
============================================================
Unsetting proxy (setup completed)
✓ Proxy unset
============================================================
```

### 精度验证阶段

```bash
── Benchmark: accuracy_check ──
  Script: ./scripts/accuracy.sh
  Logs: bisect_logs/benchmark_accuracy_abc123.log
  Duration: 120.5s
  Exit code: 0
  Found: outputs/default/20260508_171903/results/vllm-api-stream-chat/result.json
  Result file: result.json
  Status: PASS
```

### 性能验证阶段

```bash
── Benchmark: performance_check ──
  Script: ./scripts/performance.sh
  Logs: bisect_logs/benchmark_performance_abc123.log
  Duration: 60.3s
  Exit code: 0
  Extracting from log (no result_file)
  Extracted keywords: {'ttft': 123.4}
  Check: ttft <= 200 → PASS
  Status: PASS
```

---

## 关键配置说明

### vllm版本配置

| 配置方式 | YAML设置 | 说明 |
|---------|---------|------|
| 自动读取 | `vllm_version: null` | 工具从docs/source/conf.py读取 |
| 手动指定 | `vllm_version: "0.17.0"` | 用户在YAML中手动指定版本 |
| 不安装vllm | 删除该配置项 | 如果不需要vllm |

### 性能关键词配置

| 关键词 | 格式 | YAML示例 |
|--------|------|---------|
| TTFT | `TTFT: 123.4 ms` | `ttft: "<= 200"` |
| Throughput | `Throughput: 100 tokens/sec` | `throughput: ">= 100"` |
| Latency | `Latency: 150 ms` | `latency: "<= 200"` |

### 只验证精度配置

```yaml
benchmarks:
  - name: accuracy_check
    enable: true
    script: ./scripts/accuracy.sh
    result_file: ./outputs
    check:
      accuracy: ">= 0.95"
      
  - name: performance_check
    enable: false  # 跳过性能验证
```

### 只验证性能配置

```yaml
benchmarks:
  - name: accuracy_check
    enable: false  # 跳过精度验证
      
  - name: performance_check
    enable: true
    script: ./scripts/performance.sh
    check:
      ttft: "<= 200"
```

---

## 常见问题

### Q1: vllm版本自动读取失败？

**检查**：
- docs/source/conf.py文件是否存在
- 文件中是否包含 `pip_vllm_version` 变量

**解决**：
- 手动在YAML指定：`bisect_options.vllm_version: "0.17.0"`

### Q2: TTFT提取失败？

**检查**：
- performance.sh是否运行成功
- benchmark日志文件是否存在
- 日志中是否有 `TTFT: xxx ms` 格式

**解决**：
- 确保aisbench输出包含关键词
- 查看日志文件内容：`bisect_logs/benchmark_performance_*.log`

### Q3: 如何查看完整日志？

**日志文件位置**：
- Setup日志：`bisect_logs/setup_{commit}.log`
- Benchmark日志：`bisect_logs/benchmark_{name}_{commit}.log`
- Python完整日志：`bisect_logs/bisect_full.log`

---

## 输出文件总结

```
bisect_logs/
├── bisect_full.log              # Python完整日志
├── setup_abc123.log             # Setup安装日志（含vllm版本）
├── benchmark_accuracy_abc123.log # 精度验证日志
├── benchmark_performance_abc123.log # 性能验证日志（含TTFT）
└── bisect_result.json           # 二分定位结果

outputs/（aisbench生成）
└── default/
    └── 20260508_171903/
        └── results/
            └── vllm-api-stream-chat/
                └── xxx.json      # 精度结果JSON
```

---

## 对比：改动前后

### 改动前（手动处理）

**setup.sh**：
```bash
# 用户需要知道vllm版本
pip install vllm==0.17.0  # 手动指定版本
pip install -e . --no-deps -q
```

**性能验证**：
- 需要配置result_file
- 需要结果文件是JSON格式

### 改动后（自动处理）

**setup.sh**：
```bash
# 工具自动安装配套vllm
pip install -e . --no-deps -q
```

**性能验证**：
- 无需result_file
- 从日志自动提取关键词
- 简化配置

---

## 推送状态

✅ Commit `e6d81c4` 已推送  
✅ 3个文件改动，新增135行  
✅ vllm版本自动配套  
✅ TTFT关键词提取  
✅ 完整使用流程说明

**改动文件**：
- `bisect_tool.py`：自动vllm版本 + TTFT提取
- `bisect_config_template/scripts/setup.sh`：简化脚本
- `bisect_config_template/scripts/performance.sh`：示例脚本
- `bisect_config_template/config_single_node.yaml`：新增配置项
- `bisect_config_template/config_pd_separated.yaml`：同步更新
