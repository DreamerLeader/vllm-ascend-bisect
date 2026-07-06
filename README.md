# vllm-ascend PR 二分定位工具

针对 [vllm-ascend](https://github.com/vllm-project/vllm-ascend) 仓库，按 commit (PR) 粒度做二分查找，定位引入问题的具体 PR。

## 核心特性

- **单机混部优先**：本地直接执行，一键运行（80%场景）
- **pd分离扩展**：支持多节点部署（20%场景）
- **智能模式检测**：自动识别单机/多节点场景
- **完整二分流程**：checkout → setup → start → verify → stop
- **失败日志收集**：自动收集各节点日志

---

## 快速开始（单机混部 - 最简单）

### 1. 复制配置文件夹

```bash
cp -r bisect_config_template my_bisect
cd my_bisect
```

### 2. 修改脚本（根据实际场景）

```bash
vim scripts/start.sh    # 修改模型路径、启动参数
vim scripts/accuracy.sh # 修改数据集路径、验证参数
```

### 3. 一键运行

```bash
python3 ../bisect_tool.py --config config_single_node.yaml
```

**工具自动完成**：
- ✓ clone仓库（首次）
- ✓ checkout每个commit
- ✓ 执行安装脚本
- ✓ 启动vLLM服务
- ✓ 等待服务就绪
- ✓ 运行验证脚本
- ✓ 二分定位问题commit
- ✓ 输出结果

**无需手动操作！**

---

## 详细文档

- **使用说明**：[bisect_config_template/README.md](bisect_config_template/README.md)
- **使用示例**：[bisect_config_template/EXAMPLES.md](bisect_config_template/EXAMPLES.md)

---

## 依赖

```bash
pip install pyyaml requests
# 多节点Agent服务额外需要：
pip install flask
```

---

## 快速测试

运行快速测试脚本验证工具是否可用：

```bash
./quick_test.sh
```

---

## 项目结构

```
.
├── bisect_tool.py                 # 主工具（单机混部优先）
├── bisect_config_template/        # 配置文件夹模板
│   ├── config_single_node.yaml   # 单机混部配置（默认）
│   ├── config_pd_separated.yaml  # pd分离配置（扩展）
│   ├── scripts/                  # 脚本文件夹
│   │   ├── setup.sh              # 安装脚本
│   │   ├── start.sh              # 单机启动脚本
│   │   ├── start_p.sh            # p服务启动脚本
│   │   ├── start_d.sh            # d服务启动脚本
│   │   ├── stop.sh               # 停止脚本
│   │   ├── accuracy.sh           # 精度验证
│   │   └── performance.sh        # 性能验证
│   ├── agent_server.py           # Agent服务
│   ├── README.md                 # 详细使用说明
│   └── EXAMPLES.md               # 使用示例
├── quick_test.sh                 # 快速测试脚本
└── README.md                     # 本文件
```

---

## 单机混部 vs pd分离

| 特性 | 单机混部 | pd分离 |
|------|---------|--------|
| 使用频率 | 80%场景 | 20%场景 |
| 配置复杂度 | 最简单 | 稍复杂 |
| Agent启动 | 不需要 | 用户手动 |
| 脚本路径 | 相对路径（配置文件夹） | 绝对路径（各机器） |
| 节点数量 | 1个（role=all） | 多个（role=p/d） |

---

## 工作流程

### 单机混部（自动）

```
工具启动 → checkout → setup → start → wait ready → verify → stop
           ↓
       二分循环: pass → 右移, fail → 左移
           ↓
       定位问题commit → 输出结果
```

### pd分离（手动+自动）

```
用户手动启动各机器Agent → 工具检测Agent在线 → checkout所有节点 → setup所有节点
                           ↓
                    start所有节点 → wait ready → verify → stop
                           ↓
                    二分循环 → 定位问题commit → 输出结果
```

---

## 使用建议

1. **首次使用**：建议先用单机混部模式测试（最简单）
2. **脚本调试**：先手动运行脚本，确认参数正确后再配置工具
3. **阈值设置**：根据实际业务需求设置合理的阈值
4. **日志保存**：工具会自动保存失败日志，方便定位问题

---

## 输出

| 文件 | 说明 |
|------|------|
| `bisect_logs/bisect_result.json` | 定位结果（commit、PR号、二分历史） |
| `bisect_logs/*.txt` | 各节点失败日志 |

---

## 常见问题

### Q: Agent启动失败？

单机混部模式不需要启动Agent；如果多节点模式失败，检查Python和Flask是否安装、端口是否被占用

### Q: 健康检查超时？

检查：vLLM服务是否启动、`/health`接口是否正常

### Q: 验证脚本失败？

检查：脚本路径、环境变量注入、结果文件格式

详见：[bisect_config_template/README.md](bisect_config_template/README.md)
