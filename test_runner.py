#!/usr/bin/env python3
"""
vllm-ascend 多阶段测试运行器。

处理完整的验证流程:
  1. 安装当前 commit 的 vllm-ascend
  2. 拉起 vLLM 服务
  3. 等待服务就绪
  4. 运行 aisbench 精度验证
  5. 运行 aisbench 性能验证
  6. 根据阈值判断 pass/fail
  7. 停止 vLLM 服务, 清理环境

开发通过 YAML 配置文件描述场景, 无需编写复杂脚本。

Usage:
    # 作为 bisect 的 test-script 使用
    python bisect_pr.py \
        --repo-dir ./vllm-ascend \
        --good v0.7.0 --bad main \
        --test-script "python test_runner.py --config scene.yaml"

    # 单独运行验证
    python test_runner.py --config scene.yaml [--repo-dir ./vllm-ascend]
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── 配置结构 ─────────────────────────────────────────────────────────────────


@dataclass
class ServerConfig:
    """vLLM 服务配置"""
    start_cmd: str              # 启动命令, 如 "python -m vllm.entrypoints.openai.api_server ..."
    host: str = "127.0.0.1"
    port: int = 8000
    health_endpoint: str = "/health"          # 健康检查路径
    ready_timeout: int = 300                  # 等待服务就绪的超时(秒)
    ready_interval: int = 5                   # 健康检查间隔(秒)
    stop_cmd: str = ""                        # 自定义停止命令(可选, 默认 kill 进程)
    env: dict = None                          # 额外环境变量


@dataclass
class BenchConfig:
    """aisbench 验证配置"""
    name: str                                 # 验证名称
    cmd: str                                  # 运行命令
    timeout: int = 600                        # 超时(秒)
    result_file: str = ""                     # 结果文件路径 (JSON)
    result_cmd: str = ""                      # 提取结果的命令 (stdout 为 JSON)
    check: dict = None                        # 校验规则, 见下方说明


@dataclass
class SceneConfig:
    """完整场景配置"""
    name: str
    description: str = ""
    setup_cmd: str = ""                       # 安装命令
    server: ServerConfig = None               # vLLM 服务 (可选, 不需要服务的场景可不填)
    benchmarks: list = None                   # 验证列表
    cleanup_cmd: str = ""                     # 清理命令


def load_config(path: str) -> SceneConfig:
    """从 YAML 文件加载场景配置"""
    with open(path) as f:
        raw = yaml.safe_load(f)

    server = None
    if raw.get("server"):
        s = raw["server"]
        server = ServerConfig(
            start_cmd=s["start_cmd"],
            host=s.get("host", "127.0.0.1"),
            port=s.get("port", 8000),
            health_endpoint=s.get("health_endpoint", "/health"),
            ready_timeout=s.get("ready_timeout", 300),
            ready_interval=s.get("ready_interval", 5),
            stop_cmd=s.get("stop_cmd", ""),
            env=s.get("env"),
        )

    benchmarks = []
    for b in raw.get("benchmarks", []):
        benchmarks.append(BenchConfig(
            name=b["name"],
            cmd=b["cmd"],
            timeout=b.get("timeout", 600),
            result_file=b.get("result_file", ""),
            result_cmd=b.get("result_cmd", ""),
            check=b.get("check"),
        ))

    return SceneConfig(
        name=raw.get("name", "unnamed"),
        description=raw.get("description", ""),
        setup_cmd=raw.get("setup_cmd", ""),
        server=server,
        benchmarks=benchmarks,
        cleanup_cmd=raw.get("cleanup_cmd", ""),
    )


# ── 服务管理 ─────────────────────────────────────────────────────────────────


class VllmServer:
    """管理 vLLM 服务的生命周期"""

    def __init__(self, config: ServerConfig, cwd: str):
        self.config = config
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    def start(self) -> bool:
        """启动 vLLM 服务, 返回是否成功"""
        log.info("[server] Starting: %s", self.config.start_cmd)

        env = os.environ.copy()
        if self.config.env:
            env.update({k: str(v) for k, v in self.config.env.items()})

        try:
            self.process = subprocess.Popen(
                self.config.start_cmd,
                shell=True,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,  # 新进程组, 方便 kill 整棵进程树
            )
        except Exception as e:
            log.error("[server] Failed to start: %s", e)
            return False

        log.info("[server] PID: %d, waiting for ready ...", self.process.pid)
        return self._wait_ready()

    def _wait_ready(self) -> bool:
        """轮询健康检查, 等待服务就绪"""
        url = f"{self.base_url}{self.config.health_endpoint}"
        deadline = time.time() + self.config.ready_timeout

        while time.time() < deadline:
            # 检查进程是否已退出
            if self.process.poll() is not None:
                stdout = self.process.stdout.read().decode(errors="replace") if self.process.stdout else ""
                log.error("[server] Process exited prematurely (rc=%d)", self.process.returncode)
                log.error("[server] Output: %s", stdout[-2000:])
                return False

            try:
                result = subprocess.run(
                    ["curl", "-sf", "--max-time", "5", url],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    log.info("[server] Ready (health check passed)")
                    return True
            except (subprocess.TimeoutExpired, Exception):
                pass

            time.sleep(self.config.ready_interval)

        log.error("[server] Not ready after %ds", self.config.ready_timeout)
        return False

    def stop(self):
        """停止 vLLM 服务"""
        if self.config.stop_cmd:
            log.info("[server] Running stop command: %s", self.config.stop_cmd)
            subprocess.run(
                self.config.stop_cmd, shell=True, cwd=self.cwd,
                capture_output=True, timeout=30,
            )

        if self.process and self.process.poll() is None:
            log.info("[server] Killing process group (PID=%d)", self.process.pid)
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            log.info("[server] Stopped")

    def get_output(self) -> str:
        """获取服务的 stdout/stderr 输出"""
        if self.process and self.process.stdout:
            try:
                # 非阻塞读取
                import select
                if select.select([self.process.stdout], [], [], 0)[0]:
                    return self.process.stdout.read(10000).decode(errors="replace")
            except Exception:
                pass
        return ""


# ── Benchmark 执行 ───────────────────────────────────────────────────────────


def run_benchmark(bench: BenchConfig, cwd: str, env: dict) -> dict:
    """
    运行一个 benchmark, 返回:
    {
        "name": "...",
        "passed": True/False,
        "result": {...},       # 解析出的结果数据
        "output": "...",       # 原始输出
        "check_detail": "...", # 校验详情
    }
    """
    log.info("[bench] Running: %s", bench.name)
    log.info("[bench] Command: %s", bench.cmd)

    try:
        proc = subprocess.run(
            bench.cmd, shell=True, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=bench.timeout,
        )
        output = proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired:
        return {
            "name": bench.name, "passed": False,
            "result": None, "output": f"[TIMEOUT: {bench.timeout}s]",
            "check_detail": "timeout",
        }

    if proc.returncode != 0:
        log.warning("[bench] %s exited with code %d", bench.name, proc.returncode)
        # 命令本身失败, 但不一定直接判 fail, 看有没有 check 规则
        if not bench.check:
            return {
                "name": bench.name, "passed": False,
                "result": None, "output": output[-3000:],
                "check_detail": f"exit code {proc.returncode}",
            }

    # 提取结果数据
    result_data = _extract_result(bench, cwd, output)

    # 校验
    if bench.check and result_data:
        passed, detail = check_result(result_data, bench.check)
    elif bench.check and not result_data:
        passed, detail = False, "cannot extract result data for check"
    else:
        # 无 check 规则, 以命令退出码为准
        passed = proc.returncode == 0
        detail = f"exit code {proc.returncode}"

    log.info("[bench] %s: %s (%s)", bench.name, "PASS" if passed else "FAIL", detail)
    return {
        "name": bench.name, "passed": passed,
        "result": result_data, "output": output[-3000:],
        "check_detail": detail,
    }


def _extract_result(bench: BenchConfig, cwd: str, output: str) -> Optional[dict]:
    """从结果文件或命令提取结构化结果"""
    # 方式1: 从结果文件读取
    if bench.result_file:
        result_path = os.path.join(cwd, bench.result_file)
        if os.path.isfile(result_path):
            try:
                with open(result_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                log.warning("[bench] Cannot parse result file %s: %s", result_path, e)

    # 方式2: 运行提取命令
    if bench.result_cmd:
        try:
            proc = subprocess.run(
                bench.result_cmd, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout.strip())
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            log.warning("[bench] result_cmd failed: %s", e)

    # 方式3: 尝试从 stdout 末尾提取 JSON
    return _try_parse_json_from_output(output)


def _try_parse_json_from_output(output: str) -> Optional[dict]:
    """尝试从输出中找到最后一个 JSON 对象"""
    lines = output.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


# ── 结果校验 ─────────────────────────────────────────────────────────────────


def check_result(result: dict, rules: dict) -> tuple[bool, str]:
    """
    根据规则校验结果。

    规则格式:
        check:
          # 字段 >= 阈值
          accuracy: ">= 0.95"
          throughput: ">= 100"
          # 字段 <= 阈值
          latency_p99: "<= 50"
          # 字段 == 值
          status: "== success"

    支持嵌套字段, 用 . 分隔: "result.accuracy"

    Returns:
        (passed, detail_string)
    """
    details = []
    all_passed = True

    for field, condition in rules.items():
        actual = _get_nested(result, field)
        if actual is None:
            details.append(f"{field}: MISSING in result")
            all_passed = False
            continue

        passed, msg = _eval_condition(actual, condition)
        status = "OK" if passed else "FAIL"
        details.append(f"{field}: {actual} {condition} -> {status}")
        if not passed:
            all_passed = False

    detail_str = "; ".join(details)
    return all_passed, detail_str


def _get_nested(data: dict, path: str) -> Any:
    """支持 . 分隔的嵌套取值: 'result.accuracy' """
    keys = path.split(".")
    current = data
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return None
    return current


def _eval_condition(actual, condition: str) -> tuple[bool, str]:
    """计算条件: '>= 0.95', '<= 50', '== success' """
    condition = condition.strip()

    for op in [">=", "<=", "!=", "==", ">", "<"]:
        if condition.startswith(op):
            expected_str = condition[len(op):].strip()
            break
    else:
        # 无操作符, 默认 ==
        op = "=="
        expected_str = condition

    # 尝试转数字
    try:
        expected = float(expected_str)
        actual_num = float(actual)
        ops = {
            ">=": actual_num >= expected,
            "<=": actual_num <= expected,
            ">": actual_num > expected,
            "<": actual_num < expected,
            "==": actual_num == expected,
            "!=": actual_num != expected,
        }
        return ops[op], f"{actual_num} {op} {expected}"
    except (ValueError, TypeError):
        # 字符串比较
        actual_str = str(actual).strip()
        expected_str = expected_str.strip()
        if op == "==":
            return actual_str == expected_str, f"'{actual_str}' == '{expected_str}'"
        elif op == "!=":
            return actual_str != expected_str, f"'{actual_str}' != '{expected_str}'"
        return False, f"cannot compare '{actual_str}' {op} '{expected_str}'"


# ── 主流程 ───────────────────────────────────────────────────────────────────


def run_scene(config: SceneConfig, repo_dir: str) -> tuple[bool, str]:
    """
    运行完整场景:
      setup → start server → run benchmarks → check results → cleanup

    Returns:
        (passed, detail_output)
    """
    log.info("=" * 60)
    log.info("Scene: %s", config.name)
    log.info("Description: %s", config.description)
    log.info("=" * 60)

    env = os.environ.copy()
    env["BISECT_REPO_DIR"] = repo_dir

    server = None
    results = []

    try:
        # ── Step 1: Setup ──
        if config.setup_cmd:
            log.info("[setup] %s", config.setup_cmd)
            proc = subprocess.run(
                config.setup_cmd, shell=True, cwd=repo_dir, env=env,
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                msg = f"Setup failed (rc={proc.returncode}): {proc.stderr[-1000:]}"
                log.error(msg)
                return False, msg

        # ── Step 2: Start server ──
        if config.server:
            server = VllmServer(config.server, cwd=repo_dir)
            if not server.start():
                msg = "vLLM server failed to start"
                log.error(msg)
                return False, msg

            # 把 server 地址写入环境变量, benchmark 脚本可以用
            env["VLLM_BASE_URL"] = server.base_url
            env["VLLM_HOST"] = config.server.host
            env["VLLM_PORT"] = str(config.server.port)

        # ── Step 3: Run benchmarks ──
        all_passed = True
        detail_lines = []

        for bench in config.benchmarks:
            result = run_benchmark(bench, cwd=repo_dir, env=env)
            results.append(result)

            if not result["passed"]:
                all_passed = False
                detail_lines.append(f"[FAIL] {bench.name}: {result['check_detail']}")
            else:
                detail_lines.append(f"[PASS] {bench.name}: {result['check_detail']}")

        detail = "\n".join(detail_lines)
        log.info("\n--- Results ---\n%s\n", detail)
        return all_passed, detail

    except Exception as e:
        log.error("Scene execution error: %s", e, exc_info=True)
        return False, str(e)

    finally:
        # ── Step 4: Cleanup ──
        if server:
            server.stop()

        if config.cleanup_cmd:
            log.info("[cleanup] %s", config.cleanup_cmd)
            subprocess.run(
                config.cleanup_cmd, shell=True, cwd=repo_dir,
                capture_output=True, timeout=60,
            )


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="vllm-ascend 多阶段测试运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
作为 bisect 的 test-script 使用:
    python bisect_pr.py \\
        --repo-dir ./vllm-ascend \\
        --good v0.7.0 --bad main \\
        --cmd "python test_runner.py --config scene.yaml"

单独运行:
    python test_runner.py --config scene.yaml --repo-dir ./vllm-ascend
        """,
    )
    parser.add_argument("--config", required=True, help="场景配置 YAML 文件")
    parser.add_argument(
        "--repo-dir",
        default=os.environ.get("BISECT_REPO_DIR", "."),
        help="仓库目录 (默认使用 BISECT_REPO_DIR 环境变量或当前目录)",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.config):
        log.error("配置文件不存在: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)
    repo_dir = os.path.abspath(args.repo_dir)

    passed, detail = run_scene(config, repo_dir)

    print(f"\n{'='*60}")
    print(f"Scene: {config.name}")
    print(f"Result: {'PASS' if passed else 'FAIL'}")
    print(f"{'='*60}")
    print(detail)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
