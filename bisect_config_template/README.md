# vllm-ascend二分定位工具

针对 [vllm-ascend](https://github.com/vllm-project/vllm-ascend) 仓库，按 commit (PR) 粒度做二分查找，定位引入问题的具体 PR。

## 核心特性

- **单机混部优先**：本地直接执行，一键运行（80%场景）
- **pd分离扩展**：支持多节点部署（20%场景）
- **智能模式检测**：自动识别单机/多节点场景
- **完整二分流程**：checkout → setup → start → verify → stop
- **失败日志收集**：自动收集各节点日志
- **代理配置支持**：setup时自动设置代理，完成后自动取消

---

## 快速开始（单机混部 - 最简单）

### 1. 复制配置文件夹

```bash
cp -r bisect_config_template my_bisect
cd my_bisect
```

### 2. 配置代理（可选）

如果需要代理才能安装vllm-ascend：

```bash
vim proxy_env
```

填写代理配置：
```bash
export http_proxy=http://proxy.example.com:8080
export https_proxy=http://proxy.example.com:8080
export no_proxy=localhost,127.0.0.1
```

**说明**：
- setup时会自动加载proxy_env并设置代理
- setup完成后会自动unset代理
- start.sh和验证脚本运行时无代理

### 3. 修改脚本（根据实际场景）

```bash
vim scripts/setup.sh    # 修改安装逻辑
vim scripts/start.sh    # 修改模型路径、启动参数
vim scripts/accuracy.sh # 修改数据集路径、验证参数
```

### 4. 一键运行

```bash
python3 ../bisect_tool.py --config config_single_node.yaml
```

**工具自动完成**：
- ✓ clone仓库（首次）
- ✓ checkout每个commit
- ✓ 设置代理（如果提供proxy_env）
- ✓ 执行安装脚本（setup.sh）
- ✓ 取消代理（setup完成后）
- ✓ 启动vLLM服务（start.sh）
- ✓ 等待服务就绪（health check）
- ✓ 运行验证脚本
- ✓ 二分定位问题commit
- ✓ 输出结果

**无需手动操作！**

---

## pd分离场景（扩展）

### 步骤1：在各机器部署Agent

```bash
# p节点机器（192.168.1.10）
git clone https://github.com/vllm-project/vllm-ascend.git
python agent_server.py --port 8080 --repo-path ./vllm-ascend

# d节点机器（192.168.1.11）
git clone https://github.com/vllm-project/vllm-ascend.git
python agent_server.py --port 8080 --repo-path ./vllm-ascend
```

### 步骤2：配置多节点

编辑 `config_pd_separated.yaml`：

```yaml
nodes:
  - name: p_node
    role: p
    agent: {host: 192.168.1.10, port: 8080}
    scripts: {start: /home/user/scripts/start_p.sh}
    
  - name: d_node
    role: d
    agent: {host: 192.168.1.11, port: 8080}
    scripts: {start: /home/user/scripts/start_d.sh}
```

### 步骤3：复制脚本到各机器

```bash
scp scripts/*.sh 192.168.1.10:/home/user/scripts/
scp scripts/*.sh 192.168.1.11:/home/user/scripts/
```

### 步骤4：运行工具

```bash
python3 ../bisect_tool.py --config config_pd_separated.yaml
```

---

## 配置文件说明

### 单机混部 vs pd分离

| 配置项 | 单机混部 | pd分离 |
|--------|---------|--------|
| nodes数量 | 1个 | 多个 |
| node.role | `all` | `p` / `d` |
| agent.host | `127.0.0.1` | 远程IP |
| scripts路径 | 相对路径（在配置文件夹） | 绝对路径（各机器） |
| Agent启动 | 不需要 | 用户手动启动 |

### config.yaml字段说明

| 字段 | 说明 | 示例 |
|------|------|------|
| `repo.url` | Git仓库地址 | `https://github.com/vllm-project/vllm-ascend.git` |
| `repo.local_path` | 本地仓库路径 | `./vllm-ascend`（相对）或 `/home/user/vllm-ascend`（绝对） |
| `bisect_range.good_commit` | 已知正常的commit/tag | `v0.7.0` |
| `bisect_range.bad_commit` | 已知异常的commit/tag | `main` |
| `bisect_options.install_vllm` | 是否在setup前自动安装配套vLLM | `true` / `false` |
| `nodes[].agent.host/port` | Agent服务地址 | `127.0.0.1:8080` |
| `nodes[].service.host/port` | vLLM服务地址（健康检查） | `127.0.0.1:8000` |
| `nodes[].scripts.setup` | 安装脚本路径 | `./scripts/setup.sh` |
| `nodes[].scripts.start` | 启动脚本路径 | `./scripts/start.sh` |
| `benchmarks[].script` | 验证脚本路径 | `./scripts/accuracy.sh` |
| `benchmarks[].result_file` | 结果JSON文件路径 | `/tmp/accuracy_result.json` |
| `benchmarks[].check` | 阈值校验规则 | `{accuracy: ">= 0.95"}` |

---

## 脚本说明

### 脚本用途

| 脚本 | 用途 | 单机混部 | pd分离 |
|------|------|---------|--------|
| setup.sh | 安装vllm-ascend | ✓ 使用 | ✓ 使用 |
| start.sh | 单机启动服务 | ✓ 使用 | - |
| start_p.sh | p服务启动 | - | ✓ p节点 |
| start_d.sh | d服务启动 | - | ✓ d节点 |
| stop.sh | 停止服务 | ✓ 使用 | ✓ 使用 |
| accuracy.sh | 精度验证 | ✓ 使用 | ✓ 使用 |
| performance.sh | 性能验证 | ✓ 使用 | ✓ 使用 |

### 环境变量

脚本中可用的环境变量：

| 变量 | 说明 | 示例值 |
|------|------|--------|
| BISECT_REPO_DIR | 仓库路径 | `/home/user/vllm-ascend` |
| BISECT_COMMIT | 当前commit SHA | `abc1234...` |
| VLLM_HOST | vLLM服务地址（自动注入） | `127.0.0.1` |
| VLLM_PORT | vLLM服务端口（自动注入） | `8000` |

### 脚本编写要点

1. **启动脚本（start.sh/start_p.sh/start_d.sh）**：
   - 必须保持前台运行（不要用 `nohup` 或 `&`）
   - 使用环境变量 `BISECT_REPO_DIR` 获取仓库路径
   - 根据实际场景配置启动参数

2. **验证脚本（accuracy.sh/performance.sh）**：
   - 使用环境变量 `VLLM_HOST` 和 `VLLM_PORT` 访问服务
   - 结果必须写入JSON文件（格式见下方）
   - 工具会自动解析并校验阈值

3. **结果文件格式**：

```json
{
  "accuracy": 0.96,
  "pass_rate": 0.92,
  "throughput": 120.5,
  "latency_p99": 180.3
}
```

---

## 输出

工具会在配置的日志目录输出以下文件：

| 文件 | 说明 |
|------|------|
| `bisect_result.json` | 定位结果（bad commit、PR号、二分历史） |
| `single_node_logs.txt` | 单机模式失败时的日志（本地） |
| `p_node_logs.txt` | pd分离-p节点失败时的日志 |
| `d_node_logs.txt` | pd分离-d节点失败时的日志 |

### bisect_result.json格式

```json
{
  "status": "found",
  "bad_commit": {
    "sha": "abc1234...",
    "subject": "Fix tensor parallel issue (#1234)",
    "pr_number": 1234,
    "author": "user",
    "date": "2024-01-01T12:00:00+08:00"
  },
  "total_steps": 5,
  "total_commits": 100,
  "history": [
    {
      "step": 1,
      "sha": "abc1234...",
      "result": "pass"
    },
    ...
  ],
  "timestamp": "2024-01-01T15:30:00"
}
```

---

## 校验规则语法

```yaml
check:
  accuracy: ">= 0.95"       # 大于等于
  throughput: ">= 100"      # 大于等于
  latency_p99: "<= 200"     # 小于等于
  status: "== success"      # 等于
  error_count: "< 5"        # 小于
```

支持嵌套字段：`result.metrics.accuracy: ">= 0.95"`

---

## 常见问题

### Q1: Agent启动失败？

单机混部模式不需要启动Agent；如果多节点模式失败，检查：
1. Python和Flask是否安装：`pip install flask`
2. 仓库路径是否正确：`--repo-path`
3. 端口是否被占用：`lsof -i:8080`

### Q2: 健康检查超时？

检查：
1. vLLM服务是否启动成功（查看Agent日志）
2. `/health` 接口是否正常（curl测试）
3. 增加 `health_check_timeout` 配置

### Q3: 安装失败？

单机混部模式查看 `bisect_logs/setup_*.log`；多节点模式查看Agent日志（通过 `/logs` 接口或查看控制台输出）

### Q4: 验证脚本失败？

检查：
1. 脚本路径是否正确
2. 环境变量是否注入（VLLM_HOST/PORT）
3. 结果文件是否生成（JSON格式）

### Q5: 单机混部模式为什么不启动Agent？

这是当前预期行为。单机混部模式会在本机直接执行 checkout、setup、start、benchmark，不再启动Agent。

---

## 依赖

```bash
pip install pyyaml requests
# 多节点Agent服务额外需要：
pip install flask
```

---

## 工作流程

```
单机混部流程（自动）:
  1. 工具在本机直接执行
  2. checkout commit → setup → start → wait ready → verify → stop
  3. 二分循环: pass → 右移, fail → 左移
  4. 定位到问题commit → 输出结果

pd分离流程（手动+自动）:
  1. 用户手动启动各机器Agent
  2. 工具通过HTTP API协调各节点
  3. checkout所有节点 → setup所有节点 → start所有节点 → wait ready → verify → stop
  4. 二分循环
  5. 定位到问题commit → 输出结果
```

---

## 使用建议

1. **首次使用**：建议先用单机混部模式测试（最简单）
2. **脚本调试**：先手动运行脚本，确认参数正确后再配置工具
3. **阈值设置**：根据实际业务需求设置合理的阈值
4. **日志保存**：工具会自动保存失败日志，方便定位问题
5. **跳过验证**：如果已确认good/bad边界无误，可设置 `skip_initial_verification: true`，第一轮直接测试二分中间commit
