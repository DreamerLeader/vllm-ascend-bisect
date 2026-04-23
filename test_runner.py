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
    start_cmd: str = ""             # 启动命令, 如 "python -m vllm.entrypoints.openai.api_server ..."
    start_script: str = ""          # 启动脚本文件路径, 如 "./start_vllm.sh" (与 start_cmd 二选一)
    host: str = "127.0.0.1"
    port: int = 8000
    health_endpoint: str = "/health"          # 健康检查路径
    ready_timeout: int = 300                  # 等待服务就绪的超时(秒)
    ready_interval: int = 5                   # 健康检查间隔(秒)
    stop_cmd: str = ""                        # 自定义停止命令(可选, 默认 kill 进程)
    stop_script: str = ""                     # 自定义停止脚本(可选)
    env: dict = None                          # 额外环境变量


@dataclass
class BenchConfig:
    """aisbench 验证配置"""
    name: str                                 # 验证名称
    cmd: str = ""                             # 运行命令 (与 script 二选一)
    script: str = ""                          # 运行脚本文件路径 (与 cmd 二选一)
    timeout: int = 600                        # 超时(秒)
    result_file: str = ""                     # 结果文件路径 (JSON)
    result_cmd: str = ""                      # 提取结果的命令 (stdout 为 JSON)
    check: dict = None                        # 校验规则, 见下方说明


@dataclass
class SceneConfig:
    """完整场景配置"""
    name: str
    description: str = ""
    setup_cmd: str = ""                       # 安装命令 (与 setup_script 二选一)
    setup_script: str = ""                    # 安装脚本文件 (与 setup_cmd 二选一)
    server: ServerConfig = None               # vLLM 服务 (可选, 不需要服务的场景可不填)
    benchmarks: list = None                   # 验证列表
    cleanup_cmd: str = ""                     # 清理命令
    cleanup_script: str = ""                  # 清理脚本文件


def load_config(path: str) -> SceneConfig:
    """从 YAML 文件加载场景配置"""
    log.info("[config] Loading scene config from: %s", path)
    with open(path) as f:
        raw = yaml.safe_load(f)

    server = None
    if raw.get("server"):
        s = raw["server"]
        if not s.get("start_cmd") and not s.get("start_script"):
            raise ValueError("server 配置必须提供 start_cmd 或 start_script")
        server = ServerConfig(
            start_cmd=s.get("start_cmd", ""),
            start_script=s.get("start_script", ""),
            host=s.get("host", "127.0.0.1"),
            port=s.get("port", 8000),
            health_endpoint=s.get("health_endpoint", "/health"),
            ready_timeout=s.get("ready_timeout", 300),
            ready_interval=s.get("ready_interval", 5),
            stop_cmd=s.get("stop_cmd", ""),
            stop_script=s.get("stop_script", ""),
            env=s.get("env"),
        )

    benchmarks = []
    for b in raw.get("benchmarks", []):
        if not b.get("cmd") and not b.get("script"):
            raise ValueError(f"benchmark '{b.get('name', '?')}' 必须提供 cmd 或 script")
        benchmarks.append(BenchConfig(
            name=b["name"],
            cmd=b.get("cmd", ""),
            script=b.get("script", ""),
            timeout=b.get("timeout", 600),
            result_file=b.get("result_file", ""),
            result_cmd=b.get("result_cmd", ""),
            check=b.get("check"),
        ))

    config = SceneConfig(
        name=raw.get("name", "unnamed"),
        description=raw.get("description", ""),
        setup_cmd=raw.get("setup_cmd", ""),
        setup_script=raw.get("setup_script", ""),
        server=server,
        benchmarks=benchmarks,
        cleanup_cmd=raw.get("cleanup_cmd", ""),
        cleanup_script=raw.get("cleanup_script", ""),
    )
    log.info("[config] Scene: %s (%s)", config.name, config.description or "no description")
    log.info("[config] Setup: %s", config.setup_cmd or "(none)")
    log.info("[config] Server: %s", "configured" if config.server else "(none)")
    log.info("[config] Benchmarks: %d task(s) — %s",
             len(config.benchmarks),
             ", ".join(b.name for b in config.benchmarks))
    log.info("[config] Cleanup: %s", config.cleanup_cmd or "(none)")
    return config


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

    def _resolve_start_cmd(self) -> tuple[str | list[str], bool]:
        """
        解析启动方式, 返回 (cmd, use_shell).
        - start_script: 脚本文件路径, 自动处理权限和绝对路径
        - start_cmd: shell 命令字符串
        """
        if self.config.start_script:
            script = self.config.start_script
            # 相对路径基于 cwd 解析
            if not os.path.isabs(script):
                script = os.path.join(self.cwd, script)
            script = os.path.abspath(script)

            if not os.path.isfile(script):
                raise FileNotFoundError(f"启动脚本不存在: {script}")

            os.chmod(script, 0o755)
            log.info("[server] Using start script: %s", script)
            # 用 bash 显式执行, 避免 shebang 问题
            return f"bash {script}", True

        log.info("[server] Using start command: %s", self.config.start_cmd)
        return self.config.start_cmd, True

    def _resolve_stop_cmd(self) -> Optional[str]:
        """解析停止命令/脚本"""
        if self.config.stop_script:
            script = self.config.stop_script
            if not os.path.isabs(script):
                script = os.path.join(self.cwd, script)
            script = os.path.abspath(script)
            if os.path.isfile(script):
                os.chmod(script, 0o755)
                log.info("[server] Using stop script: %s", script)
                return f"bash {script}"
            else:
                log.warning("[server] Stop script not found: %s, will use kill", script)
                return None

        if self.config.stop_cmd:
            return self.config.stop_cmd

        return None

    def start(self) -> bool:
        """启动 vLLM 服务, 返回是否成功"""
        log.info("[server] Starting vLLM service ...")
        log.info("[server] Endpoint: %s, health: %s", self.base_url, self.config.health_endpoint)
        log.info("[server] Ready timeout: %ds, check interval: %ds",
                 self.config.ready_timeout, self.config.ready_interval)

        try:
            cmd, use_shell = self._resolve_start_cmd()
        except FileNotFoundError as e:
            log.error("[server] %s", e)
            return False

        env = os.environ.copy()
        if self.config.env:
            env.update({k: str(v) for k, v in self.config.env.items()})
            log.info("[server] Extra env: %s", ", ".join(f"{k}={v}" for k, v in self.config.env.items()))

        try:
            self.process = subprocess.Popen(
                cmd,
                shell=use_shell,
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
        attempt = 0
        start_time = time.time()

        log.info("[server] Health check URL: %s", url)

        while time.time() < deadline:
            attempt += 1
            # 检查进程是否已退出
            if self.process.poll() is not None:
                stdout = self.process.stdout.read().decode(errors="replace") if self.process.stdout else ""
                log.error("[server] Process exited prematurely (rc=%d) after %.1fs",
                          self.process.returncode, time.time() - start_time)
                log.error("[server] Output tail:\n%s", stdout[-2000:])
                return False

            try:
                result = subprocess.run(
                    ["curl", "-sf", "--max-time", "5", url],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    elapsed = time.time() - start_time
                    log.info("[server] Ready! Health check passed after %d attempts (%.1fs)", attempt, elapsed)
                    return True
            except (subprocess.TimeoutExpired, Exception):
                pass

            remaining = deadline - time.time()
            if attempt % 6 == 0:  # 每 30 秒左右打印一次等待状态
                log.info("[server] Still waiting for ready ... (attempt %d, %.0fs remaining)", attempt, remaining)

            time.sleep(self.config.ready_interval)

        log.error("[server] Not ready after %ds (%d attempts)", self.config.ready_timeout, attempt)
        return False

    def stop(self):
        """停止 vLLM 服务"""
        log.info("[server] Stopping vLLM service ...")
        stop_cmd = self._resolve_stop_cmd()
        if stop_cmd:
            log.info("[server] Running stop command: %s", stop_cmd)
            result = subprocess.run(
                stop_cmd, shell=True, cwd=self.cwd,
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.warning("[server] Stop command exited with code %d: %s",
                            result.returncode, result.stderr[:200])

        if self.process and self.process.poll() is None:
            log.info("[server] Sending SIGTERM to process group (PID=%d)", self.process.pid)
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=15)
                log.info("[server] Process terminated gracefully")
            except (ProcessLookupError, subprocess.TimeoutExpired):
                log.warning("[server] SIGTERM timeout, sending SIGKILL ...")
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    log.info("[server] Process killed with SIGKILL")
                except ProcessLookupError:
                    log.info("[server] Process already exited")
        elif self.process:
            log.info("[server] Process already exited (rc=%d)", self.process.returncode)
        else:
            log.debug("[server] No process to stop")

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


def _resolve_script(script_path: str, cwd: str, label: str) -> str:
    """将脚本路径解析为可执行的 shell 命令, 处理相对路径和权限"""
    if not os.path.isabs(script_path):
        script_path = os.path.join(cwd, script_path)
    script_path = os.path.abspath(script_path)

    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"{label} 脚本不存在: {script_path}")

    os.chmod(script_path, 0o755)
    log.info("[%s] Using script: %s", label, script_path)
    return f"bash {script_path}"


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
    # 解析 benchmark 的执行命令
    run_cmd = bench.cmd
    if bench.script:
        script_path = bench.script
        if not os.path.isabs(script_path):
            script_path = os.path.join(cwd, script_path)
        script_path = os.path.abspath(script_path)
        if not os.path.isfile(script_path):
            log.error("[bench] Script not found: %s", script_path)
            return {
                "name": bench.name, "passed": False,
                "result": None, "output": f"[SCRIPT NOT FOUND: {script_path}]",
                "check_detail": f"script not found: {script_path}",
            }
        os.chmod(script_path, 0o755)
        run_cmd = f"bash {script_path}"
        log.info("[bench] ─── Running benchmark: %s (script) ───", bench.name)
        log.info("[bench] Script: %s", script_path)
    else:
        log.info("[bench] ─── Running benchmark: %s (cmd) ───", bench.name)
        log.info("[bench] Command: %s", run_cmd)

    log.info("[bench] Timeout: %ds, result_file: %s, check rules: %s",
             bench.timeout,
             bench.result_file or "(none)",
             ", ".join(f"{k} {v}" for k, v in bench.check.items()) if bench.check else "(exit code)")

    bench_start = time.time()
    try:
        proc = subprocess.run(
            run_cmd, shell=True, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=bench.timeout,
        )
        output = proc.stdout + "\n" + proc.stderr
        bench_elapsed = time.time() - bench_start
        log.info("[bench] %s finished in %.1fs, exit code: %d", bench.name, bench_elapsed, proc.returncode)
    except subprocess.TimeoutExpired:
        bench_elapsed = time.time() - bench_start
        log.error("[bench] %s TIMEOUT after %.1fs (limit: %ds)", bench.name, bench_elapsed, bench.timeout)
        return {
            "name": bench.name, "passed": False,
            "result": None, "output": f"[TIMEOUT: {bench.timeout}s]",
            "check_detail": "timeout",
        }

    if proc.returncode != 0:
        log.warning("[bench] %s exited with code %d", bench.name, proc.returncode)
        log.debug("[bench] stderr tail: %s", proc.stderr[-500:] if proc.stderr else "")
        # 命令本身失败, 但不一定直接判 fail, 看有没有 check 规则
        if not bench.check:
            log.info("[bench] %s: FAIL (no check rules, using exit code)", bench.name)
            return {
                "name": bench.name, "passed": False,
                "result": None, "output": output[-3000:],
                "check_detail": f"exit code {proc.returncode}",
            }

    # 提取结果数据
    log.info("[bench] Extracting result data ...")
    result_data = _extract_result(bench, cwd, output)
    if result_data:
        log.info("[bench] Result data extracted: %s", json.dumps(result_data, ensure_ascii=False)[:500])
    else:
        log.warning("[bench] No result data extracted")

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
        result_path = bench.result_file if os.path.isabs(bench.result_file) else os.path.join(cwd, bench.result_file)
        log.debug("[bench] Trying result file: %s", result_path)
        if os.path.isfile(result_path):
            try:
                with open(result_path) as f:
                    data = json.load(f)
                log.info("[bench] Result extracted from file: %s", result_path)
                return data
            except (json.JSONDecodeError, IOError) as e:
                log.warning("[bench] Cannot parse result file %s: %s", result_path, e)
        else:
            log.warning("[bench] Result file not found: %s", result_path)

    # 方式2: 运行提取命令
    if bench.result_cmd:
        log.debug("[bench] Trying result_cmd: %s", bench.result_cmd)
        try:
            proc = subprocess.run(
                bench.result_cmd, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout.strip())
                log.info("[bench] Result extracted from result_cmd")
                return data
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            log.warning("[bench] result_cmd failed: %s", e)

    # 方式3: 尝试从 stdout 末尾提取 JSON
    log.debug("[bench] Trying to parse JSON from command output ...")
    data = _try_parse_json_from_output(output)
    if data:
        log.info("[bench] Result extracted from command output (auto-detect)")
    else:
        log.debug("[bench] No JSON found in command output")
    return data


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

    log.info("[check] Checking %d rule(s) ...", len(rules))
    for field, condition in rules.items():
        actual = _get_nested(result, field)
        if actual is None:
            details.append(f"{field}: MISSING in result")
            all_passed = False
            log.warning("[check] %s: field MISSING in result data", field)
            continue

        passed, msg = _eval_condition(actual, condition)
        status = "OK" if passed else "FAIL"
        details.append(f"{field}: {actual} {condition} -> {status}")
        if not passed:
            all_passed = False
            log.warning("[check] FAIL: %s = %s (expected %s)", field, actual, condition)
        else:
            log.info("[check] OK: %s = %s %s", field, actual, condition)

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

    scene_start = time.time()

    try:
        # ── Step 1: Setup ──
        setup_run_cmd = None
        if config.setup_script:
            try:
                setup_run_cmd = _resolve_script(config.setup_script, repo_dir, "setup")
            except FileNotFoundError as e:
                return False, str(e)
        elif config.setup_cmd:
            setup_run_cmd = config.setup_cmd

        if setup_run_cmd:
            log.info("[step 1/4] Running setup ...")
            log.info("[setup] Command: %s", setup_run_cmd)
            setup_start = time.time()
            proc = subprocess.run(
                setup_run_cmd, shell=True, cwd=repo_dir, env=env,
                capture_output=True, text=True, timeout=600,
            )
            setup_elapsed = time.time() - setup_start
            if proc.returncode != 0:
                msg = f"Setup failed (rc={proc.returncode}) after {setup_elapsed:.1f}s"
                log.error("[setup] %s", msg)
                log.error("[setup] stderr: %s", proc.stderr[-1000:])
                return False, msg
            log.info("[setup] Completed in %.1fs", setup_elapsed)
        else:
            log.info("[step 1/4] No setup command/script, skipping")

        # ── Step 2: Start server ──
        if config.server:
            log.info("[step 2/4] Starting vLLM server ...")
            server = VllmServer(config.server, cwd=repo_dir)
            if not server.start():
                msg = "vLLM server failed to start"
                log.error("[server] %s", msg)
                return False, msg

            # 把 server 地址写入环境变量, benchmark 脚本可以用
            env["VLLM_BASE_URL"] = server.base_url
            env["VLLM_HOST"] = config.server.host
            env["VLLM_PORT"] = str(config.server.port)
            log.info("[server] Environment set: VLLM_BASE_URL=%s", server.base_url)
        else:
            log.info("[step 2/4] No server configured, skipping")

        # ── Step 3: Run benchmarks ──
        log.info("[step 3/4] Running %d benchmark(s) ...", len(config.benchmarks))
        all_passed = True
        detail_lines = []

        for i, bench in enumerate(config.benchmarks, 1):
            log.info("[bench] (%d/%d) Starting: %s", i, len(config.benchmarks), bench.name)
            result = run_benchmark(bench, cwd=repo_dir, env=env)
            results.append(result)

            if not result["passed"]:
                all_passed = False
                detail_lines.append(f"[FAIL] {bench.name}: {result['check_detail']}")
            else:
                detail_lines.append(f"[PASS] {bench.name}: {result['check_detail']}")

        scene_elapsed = time.time() - scene_start
        detail = "\n".join(detail_lines)
        log.info("")
        log.info("─── Scene Results (%.1fs) ───", scene_elapsed)
        for line in detail_lines:
            log.info("  %s", line)
        log.info("─── Overall: %s ───", "PASS" if all_passed else "FAIL")
        return all_passed, detail

    except Exception as e:
        log.error("[scene] Execution error after %.1fs: %s",
                  time.time() - scene_start, e, exc_info=True)
        return False, str(e)

    finally:
        # ── Step 4: Cleanup ──
        log.info("[step 4/4] Cleanup ...")
        if server:
            server.stop()

        cleanup_run_cmd = None
        if config.cleanup_script:
            try:
                cleanup_run_cmd = _resolve_script(config.cleanup_script, repo_dir, "cleanup")
            except FileNotFoundError as e:
                log.warning("[cleanup] %s", e)
        elif config.cleanup_cmd:
            cleanup_run_cmd = config.cleanup_cmd

        if cleanup_run_cmd:
            log.info("[cleanup] Running: %s", cleanup_run_cmd)
            cleanup_result = subprocess.run(
                cleanup_run_cmd, shell=True, cwd=repo_dir,
                capture_output=True, text=True, timeout=60,
            )
            if cleanup_result.returncode != 0:
                log.warning("[cleanup] Command exited with code %d", cleanup_result.returncode)
            else:
                log.info("[cleanup] Done")
        else:
            log.info("[cleanup] No cleanup command/script configured")


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
