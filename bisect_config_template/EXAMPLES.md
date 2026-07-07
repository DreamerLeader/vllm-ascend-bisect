# 使用示例

## 示例1：单机混部 - LLaMA推理精度验证

### 场景描述
在单机环境中部署LLaMA-7B模型，验证vllm-ascend从v0.7.0到main版本之间的精度问题。

### 步骤

```bash
# 1. 复制配置文件夹
cp -r bisect_config_template llama_bisect
cd llama_bisect

# 2. 修改启动脚本
vim scripts/start.sh
```

修改内容：
```bash
python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000 \
    --tensor-parallel-size 2 \
    --trust-remote-code
```

```bash
# 3. 修改精度验证脚本
vim scripts/accuracy.sh
```

修改内容：
```bash
aisbench accuracy \
    --url http://${VLLM_HOST}:${VLLM_PORT}/v1/completions \
    --model llama-7b \
    --dataset /data/datasets/llama_eval.jsonl \
    --output /tmp/accuracy_result.json \
    --timeout 600
```

```bash
# 4. 修改配置文件（可选）
vim config_single_node.yaml
```

修改内容：
```yaml
bisect_range:
  good_commit: "v0.6.0"  # 已知正常版本
  bad_commit: "v0.7.0"   # 已知异常版本

benchmarks:
  - name: accuracy_check
    check:
      accuracy: ">= 0.90"  # 根据业务需求调整阈值
```

```bash
# 5. 一键运行
python3 ../bisect_tool.py --config config_single_node.yaml
```

### 输出
- `bisect_logs/bisect_result.json`：定位结果
- `bisect_logs/single_node_logs.txt`：失败日志（如果测试失败）

---

## 示例2：pd分离 - LLaMA分布式推理

### 场景描述
LLaMA-7B模型在2台机器上pd分离部署（p节点：192.168.1.10，d节点：192.168.1.11），验证tensor parallel功能。

### 步骤

#### 步骤1：在p节点（192.168.1.10）部署Agent

```bash
git clone https://github.com/vllm-project/vllm-ascend.git
cd vllm-ascend

# 从控制机器复制Agent脚本
scp control_machine:/path/to/bisect_config_template/agent_server.py ./agent_server.py

# 启动Agent
python3 agent_server.py --port 8080 --repo-path ./vllm-ascend
```

#### 步骤2：在d节点（192.168.1.11）部署Agent

```bash
git clone https://github.com/vllm-project/vllm-ascend.git
cd vllm-ascend

# 从控制机器复制Agent脚本
scp control_machine:/path/to/bisect_config_template/agent_server.py ./agent_server.py

# 启动Agent
python3 agent_server.py --port 8080 --repo-path ./vllm-ascend
```

#### 步骤3：在控制机器配置

```bash
# 复制配置文件夹
cp -r bisect_config_template llama_pd_bisect
cd llama_pd_bisect

# 编辑配置文件
vim config_pd_separated.yaml
```

修改内容：
```yaml
nodes:
  - name: p_node
    role: p
    agent:
      host: 192.168.1.10
      port: 8080
    service:
      host: 192.168.1.10
      port: 8000
    scripts:
      setup: /home/user/scripts/setup.sh
      start: /home/user/scripts/start_p.sh
      stop: /home/user/scripts/stop.sh
      
  - name: d_node
    role: d
    agent:
      host: 192.168.1.11
      port: 8080
    service:
      host: 192.168.1.11
      port: 8000
    scripts:
      setup: /home/user/scripts/setup.sh
      start: /home/user/scripts/start_d.sh
      stop: /home/user/scripts/stop.sh
```

#### 步骤4：编写启动脚本并复制到各节点

```bash
# p节点启动脚本
vim scripts/start_p.sh
```

```bash
python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000 \
    --role p \
    --tensor-parallel-size 2 \
    --distributed-executor-backend ray \
    --trust-remote-code
```

```bash
# d节点启动脚本
vim scripts/start_d.sh
```

```bash
python -m vllm.entrypoints.openai.api_server \
    --model /data/models/llama-7b \
    --device npu \
    --host 0.0.0.0 \
    --port 8000 \
    --role d \
    --tensor-parallel-size 2 \
    --distributed-executor-backend ray \
    --trust-remote-code
```

```bash
# 复制脚本到各节点
scp scripts/setup.sh scripts/start_p.sh scripts/stop.sh 192.168.1.10:/home/user/scripts/
scp scripts/setup.sh scripts/start_d.sh scripts/stop.sh 192.168.1.11:/home/user/scripts/
```

#### 步骤5：运行工具

```bash
python3 ../bisect_tool.py --config config_pd_separated.yaml
```

### 输出
- `bisect_logs/bisect_result.json`：定位结果
- `bisect_logs/p_node_logs.txt`：p节点失败日志
- `bisect_logs/d_node_logs.txt`：d节点失败日志

---

## 示例3：性能验证场景

### 场景描述
验证vllm-ascend的性能是否满足要求（吞吐量 >= 100 tokens/sec）。

### 配置修改

```yaml
benchmarks:
  - name: performance_check
    script: ./scripts/performance.sh
    timeout: 600
    result_file: /tmp/perf_result.json
    check:
      throughput: ">= 100"
      latency_p99: "<= 200"
```

### 验证脚本修改

```bash
vim scripts/performance.sh
```

```bash
aisbench performance \
    --url http://${VLLM_HOST}:${VLLM_PORT}/v1/completions \
    --model llama-7b \
    --concurrency 8 \
    --num-requests 100 \
    --output /tmp/perf_result.json
```

---

## 示例4：跳过验证场景

### 场景描述
已确认good/bad commit边界无误，跳过初始端点验证，第一轮直接测试二分中间commit。

### 配置修改

```yaml
bisect_options:
  skip_initial_verification: true  # 直接从中间commit开始
```

---

## 示例5：混合验证场景

### 场景描述
同时验证精度和性能，两个指标必须同时满足。

### 配置修改

```yaml
benchmarks:
  - name: accuracy_check
    script: ./scripts/accuracy.sh
    timeout: 600
    result_file: /tmp/accuracy_result.json
    check:
      accuracy: ">= 0.95"
      
  - name: performance_check
    script: ./scripts/performance.sh
    timeout: 600
    result_file: /tmp/perf_result.json
    check:
      throughput: ">= 100"
      latency_p99: "<= 200"
```

工具会依次运行两个验证任务，全部通过才算pass。
