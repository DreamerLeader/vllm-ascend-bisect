#!/usr/bin/env python3
"""
vllm-ascend二分定位工具 - 支持单机混部和pd分离

核心特性：
  1. 单机混部优先：自动启动Agent，直接执行命令，最简单
  2. pd分离扩展：通过HTTP API协调多节点
  3. 智能模式检测：自动识别单机/多节点场景
  4. 完整二分流程：checkout -> setup -> start -> verify -> stop
  5. 失败日志收集：自动收集各节点日志

使用方法：
  python bisect_tool.py --config bisect_config/config_single_node.yaml

输出：
  - bisect_logs/bisect_result.json（定位结果）
  - bisect_logs/*.txt（各节点日志）
"""

import argparse, json, yaml, requests, subprocess, time, logging, os, signal
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)


class BisectTool:
    """二分定位工具核心类"""
    
    def __init__(self, config_path):
        self.config_path = Path(config_path).absolute()
        self.config_dir = self.config_path.parent
        
        with open(self.config_path) as f:
            self.config = yaml.safe_load(f)
        
        self.nodes = self.config['nodes']
        self.repo_config = self.config['repo']
        self.log_dir = self.config['log']['output_dir']
        
        if not self.log_dir.startswith('/'):
            self.log_dir = str(self.config_dir / self.log_dir)
        
        Path(self.log_dir).mkdir(exist_ok=True)
        
        self.mode = self.detect_mode()
        self.agent_process = None
        
        log.info(f"Mode detected: {self.mode}")
        log.info(f"Config dir: {self.config_dir}")
        log.info(f"Log dir: {self.log_dir}")
        
    def detect_mode(self):
        """自动检测是单机混部还是pd分离"""
        if len(self.nodes) == 1:
            node = self.nodes[0]
            if node['role'] == 'all' and node['agent']['host'] in ['127.0.0.1', 'localhost']:
                return "single_node"
        return "multi_node"
    
    def get_repo_path(self):
        """解析仓库路径（支持相对路径和绝对路径）"""
        repo_path = self.repo_config['local_path']
        if not repo_path.startswith('/'):
            repo_path = str(self.config_dir / repo_path)
        return repo_path
    
    def start_agent_if_needed(self):
        """单机模式：自动启动Agent"""
        if self.mode == "single_node":
            node = self.nodes[0]
            agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
            
            try:
                response = requests.get(f"{agent_url}/health", timeout=2)
                if response.json()['status'] == 'ok':
                    log.info("Agent already running")
                    return True
            except:
                pass
            
            log.info("Auto-starting local agent...")
            
            agent_script = self.config_dir / "agent_server.py"
            repo_path = self.get_repo_path()
            
            self.agent_process = subprocess.Popen([
                'python', str(agent_script),
                '--port', str(node['agent']['port']),
                '--repo-path', repo_path
            ], cwd=self.config_dir)
            
            time.sleep(3)
            try:
                requests.get(f"{agent_url}/health", timeout=2)
                log.info("Agent started successfully")
                return True
            except:
                log.error("Failed to start Agent")
                return False
        else:
            return self.check_agents_online()
    
    def check_agents_online(self):
        """多节点模式：检查所有Agent是否在线"""
        log.info("Checking if all agents are online...")
        for node in self.nodes:
            agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
            try:
                response = requests.get(f"{agent_url}/health", timeout=5)
                if response.json()['status'] == 'ok':
                    log.info(f"✓ {node['name']} agent online")
                else:
                    log.error(f"✗ {node['name']} agent offline")
                    return False
            except:
                log.error(f"✗ {node['name']} agent unreachable")
                return False
        return True
    
    def resolve_script_path(self, script_path):
        """解析脚本路径（支持相对路径和绝对路径）"""
        if script_path.startswith('./') or not script_path.startswith('/'):
            return str(self.config_dir / script_path)
        return script_path
    
    def checkout_all_nodes(self, commit):
        """切换到指定commit"""
        if self.mode == "single_node":
            repo_path = self.get_repo_path()
            
            log.info(f"Local checkout to {commit}")
            try:
                subprocess.run(['git', '-C', repo_path, 'checkout', '-f', commit], 
                              check=True, capture_output=True)
                subprocess.run(['git', '-C', repo_path, 'clean', '-fd',
                               '--exclude=.venv', '--exclude=venv'], 
                              check=True, capture_output=True)
                return True
            except subprocess.CalledProcessError as e:
                log.error(f"Checkout failed: {e.stderr}")
                return False
        else:
            log.info(f"Checkout commit {commit} on all nodes...")
            for node in self.nodes:
                agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
                timeout = node['agent'].get('timeout', 30)
                
                try:
                    response = requests.post(
                        f"{agent_url}/checkout",
                        json={"commit": commit},
                        timeout=timeout
                    )
                    if response.json()['status'] != 'ok':
                        log.error(f"Checkout failed on {node['name']}")
                        return False
                except Exception as e:
                    log.error(f"Checkout error on {node['name']}: {e}")
                    return False
            return True
    
    def setup_all_nodes(self):
        """执行安装脚本（增强日志输出）"""
        if self.mode == "single_node":
            node = self.nodes[0]
            script = self.resolve_script_path(node['scripts']['setup'])
            repo_path = self.get_repo_path()
            
            log.info("="*60)
            log.info(f"Running setup script: {script}")
            log.info("="*60)
            
            env = os.environ.copy()
            env['BISECT_REPO_DIR'] = repo_path
            env['BISECT_COMMIT'] = subprocess.check_output(
                ['git', '-C', repo_path, 'rev-parse', 'HEAD'],
                timeout=5
            ).decode().strip()
            
            commit_short = subprocess.check_output(
                ['git', '-C', repo_path, 'rev-parse', '--short', 'HEAD'],
                timeout=5
            ).decode().strip()
            log.info(f"Setup at commit: {commit_short}")
            
            setup_start = time.time()
            
            try:
                # 实时显示setup输出（不使用capture_output，直接流式输出）
                process = subprocess.Popen(
                    ['bash', script], cwd=repo_path, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True
                )
                
                # 实时读取stdout和stderr
                stdout_lines = []
                stderr_lines = []
                
                import select
                while process.poll() is None:
                    # 检查是否有输出可读
                    ready_fds, _, _ = select.select([process.stdout, process.stderr], [], [], 1.0)
                    
                    for fd in ready_fds:
                        if fd == process.stdout:
                            line = process.stdout.readline()
                            if line:
                                line = line.rstrip()
                                stdout_lines.append(line)
                                log.info(f"[setup] {line}")
                        elif fd == process.stderr:
                            line = process.stderr.readline()
                            if line:
                                line = line.rstrip()
                                stderr_lines.append(line)
                                log.warning(f"[setup-err] {line}")
                    
                    # 定期显示进度（每10秒）
                    elapsed = time.time() - setup_start
                    if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                        remaining = max(0, 600 - elapsed)
                        log.info(f"[setup] Still running... ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)")
                
                # 读取剩余输出
                remaining_stdout = process.stdout.read()
                remaining_stderr = process.stderr.read()
                if remaining_stdout:
                    for line in remaining_stdout.strip().splitlines():
                        stdout_lines.append(line)
                        log.info(f"[setup] {line}")
                if remaining_stderr:
                    for line in remaining_stderr.strip().splitlines():
                        stderr_lines.append(line)
                        log.warning(f"[setup-err] {line}")
                
                setup_elapsed = time.time() - setup_start
                
                if process.returncode != 0:
                    log.error("="*60)
                    log.error(f"Setup FAILED (exit code {process.returncode}) after {setup_elapsed:.1f}s")
                    log.error("="*60)
                    
                    # 保存完整setup日志
                    setup_log_file = Path(self.log_dir) / f"setup_fail_{commit_short}.log"
                    with open(setup_log_file, 'w') as f:
                        f.write(f"Setup failed at commit {commit_short}\n")
                        f.write(f"Exit code: {process.returncode}\n")
                        f.write(f"Duration: {setup_elapsed:.1f}s\n")
                        f.write("\n=== STDOUT ===\n")
                        f.write('\n'.join(stdout_lines))
                        f.write("\n=== STDERR ===\n")
                        f.write('\n'.join(stderr_lines))
                    log.error(f"Full setup log saved: {setup_log_file}")
                    
                    return "fail"
                
                log.info("="*60)
                log.info(f"Setup SUCCESS ({setup_elapsed:.1f}s)")
                log.info("="*60)
                return "ok"
                
            except subprocess.TimeoutExpired:
                setup_elapsed = time.time() - setup_start
                log.error("="*60)
                log.error(f"Setup TIMEOUT after {setup_elapsed:.1f}s (limit 600s)")
                log.error("="*60)
                if process:
                    process.kill()
                return "fail"
        else:
            log.info("="*60)
            log.info("Running setup on all nodes...")
            log.info("="*60)
            
            for node in self.nodes:
                agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
                timeout = node['agent'].get('timeout', 30)
                
                log.info(f"\n── Setup on {node['name']} ({node['role']}) ──")
                
                try:
                    response = requests.post(
                        f"{agent_url}/setup",
                        json={"script": node['scripts']['setup']},
                        timeout=600
                    )
                    result = response.json()
                    
                    # 显示Agent返回的setup输出
                    if result.get('stdout'):
                        log.info("Setup stdout:")
                        for line in result['stdout'].strip().splitlines()[-20:]:
                            log.info(f"  {line}")
                    
                    if result['status'] != 'ok':
                        log.error("="*60)
                        log.error(f"Setup FAILED on {node['name']}")
                        log.error("="*60)
                        
                        if result.get('stderr'):
                            log.error("Setup stderr:")
                            for line in result['stderr'].strip().splitlines()[-20:]:
                                log.error(f"  {line}")
                        
                        log.error(f"Error: {result.get('error', 'unknown')}")
                        return "fail"
                    
                    log.info(f"✓ Setup SUCCESS on {node['name']}")
                        
                except Exception as e:
                    log.error(f"Setup error on {node['name']}: {e}")
                    return "fail"
            
            log.info("="*60)
            log.info("Setup SUCCESS on all nodes")
            log.info("="*60)
            return "ok"
    
    def start_services(self):
        """启动所有节点的服务（实时显示启动日志）"""
        if self.mode == "single_node":
            node = self.nodes[0]
            script = self.resolve_script_path(node['scripts']['start'])
            repo_path = self.get_repo_path()
            
            log.info("="*60)
            log.info(f"Starting vLLM service: {script}")
            log.info("="*60)
            
            env = os.environ.copy()
            env['BISECT_REPO_DIR'] = repo_path
            
            try:
                process = subprocess.Popen(
                    ['bash', script], cwd=repo_path, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                    text=True
                )
                
                log.info(f"Service started (PID {process.pid})")
                log.info("Service logs will be displayed in real-time...")
                
                # 启动后台线程实时读取并显示服务日志
                import threading
                import queue
                
                log_queue = queue.Queue()
                service_logs = []
                
                def monitor_service_logs():
                    """后台线程：实时读取服务stdout并显示"""
                    try:
                        for raw_line in iter(process.stdout.readline, ""):
                            if raw_line:
                                line = raw_line.rstrip()
                                service_logs.append(line)
                                # 关键日志立即显示
                                if any(keyword in line for keyword in [
                                    "Uvicorn running on",
                                    "Application startup complete",
                                    "Loaded model",
                                    "ERROR",
                                    "WARNING"
                                ]):
                                    log.info(f"[vllm] {line}")
                                # 每隔一定行数显示进度（避免过多输出）
                                elif len(service_logs) % 20 == 0:
                                    log.info(f"[vllm] ... ({len(service_logs)} lines logged)")
                    except:
                        pass
                
                monitor_thread = threading.Thread(target=monitor_service_logs, daemon=True)
                monitor_thread.start()
                
                # 返回PID和监控线程相关信息
                return {
                    "single_node": process.pid,
                    "process": process,
                    "monitor_thread": monitor_thread,
                    "service_logs": service_logs
                }
            except Exception as e:
                log.error(f"Start failed: {e}")
                return None
        else:
            log.info("Starting services on all nodes...")
            pids = {}
            
            for node in self.nodes:
                agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
                timeout = node['agent'].get('timeout', 30)
                
                try:
                    response = requests.post(
                        f"{agent_url}/start",
                        json={"script": node['scripts']['start']},
                        timeout=timeout
                    )
                    result = response.json()
                    
                    if result['status'] != 'ok':
                        log.error(f"Start failed on {node['name']}")
                        return None
                    
                    pids[node['name']] = result.get('pid')
                    log.info(f"{node['name']} started (PID {result.get('pid')})")
                    
                except Exception as e:
                    log.error(f"Start error on {node['name']}: {e}")
                    return None
            
            return pids
    
    def wait_services_ready(self, service_info=None):
        """等待所有服务就绪（显示实时服务日志）"""
        log.info("="*60)
        log.info("Waiting for vLLM service to be ready...")
        log.info("="*60)
        
        timeout = self.config['bisect_options']['health_check_timeout']
        interval = self.config['bisect_options']['health_check_interval']
        
        # 初始化日志追踪变量（避免作用域问题）
        last_log_count = 0
        
        for node in self.nodes:
            service_url = f"http://{node['service']['host']}:{node['service']['port']}"
            health_endpoint = node['service'].get('health_endpoint', '/health')
            url = f"{service_url}{health_endpoint}"
            
            deadline = time.time() + timeout
            log.info(f"Health check URL: {url}")
            
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                
                # 单机模式：显示新日志（直接从service_info获取）
                if self.mode == "single_node" and service_info and "service_logs" in service_info:
                    current_logs = service_info["service_logs"]
                    if len(current_logs) > last_log_count:
                        new_logs = current_logs[last_log_count:]
                        for log_line in new_logs[-10:]:  # 显示最近10条新日志
                            log.info(f"[vllm] {log_line}")
                        last_log_count = len(current_logs)
                
                # 检查服务进程是否意外退出
                if self.mode == "single_node" and service_info:
                    process = service_info.get("process")
                    if process and process.poll() is not None:
                        log.error("="*60)
                        log.error(f"Service process exited prematurely (exit code {process.returncode})")
                        log.error("="*60)
                        # 显示最后的服务日志
                        if service_info.get("service_logs"):
                            log.error("Last 20 lines of service logs:")
                            for log_line in service_info["service_logs"][-20:]:
                                log.error(f"  {log_line}")
                        return False
                
                try:
                    response = requests.get(url, timeout=2)
                    if response.status_code == 200:
                        log.info("="*60)
                        log.info(f"✓ Service READY! (after {attempt} checks)")
                        log.info("="*60)
                        # 显示服务就绪时的关键日志
                        if self.mode == "single_node" and service_info and service_info.get("service_logs"):
                            log.info("Service startup logs (last 10 lines):")
                            for log_line in service_info["service_logs"][-10:]:
                                log.info(f"  {log_line}")
                        return True
                except:
                    pass
                
                # 定期显示进度
                if attempt % 15 == 0:
                    elapsed = time.time() - (deadline - timeout)
                    remaining = deadline - time.time()
                    log.info(f"Still waiting... (attempt {attempt}, {elapsed:.0f}s elapsed, {remaining:.0f}s remaining)")
                    if self.mode == "single_node" and service_info and service_info.get("service_logs"):
                        log.info(f"  Service logs collected: {len(service_info['service_logs'])} lines")
                
                time.sleep(interval)
            
            log.error("="*60)
            log.error(f"✗ Service NOT READY after {timeout}s ({attempt} attempts)")
            log.error("="*60)
            
            # 显示失败时的服务日志
            if self.mode == "single_node" and service_info and service_info.get("service_logs"):
                log.error("Last 20 lines of service logs:")
                for log_line in service_info["service_logs"][-20:]:
                    log.error(f"  {log_line}")
            
            return False
        
        return True
    
    def collect_logs_on_failure(self):
        """失败时收集所有节点的日志"""
        if not self.config['log']['collect_on_failure']:
            return
        
        log.info("Collecting logs from all nodes...")
        max_lines = self.config['log']['max_log_lines']
        
        if self.mode == "single_node":
            log_file = Path(self.log_dir) / "local_logs.txt"
            with open(log_file, 'w') as f:
                f.write(f"Logs collected at {datetime.now().isoformat()}\n")
            log.info(f"Logs saved: {log_file}")
        else:
            for node in self.nodes:
                agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
                
                try:
                    response = requests.get(
                        f"{agent_url}/logs",
                        params={"max_lines": max_lines},
                        timeout=30
                    )
                    logs = response.json()['logs']
                    
                    log_file = Path(self.log_dir) / f"{node['name']}_logs.txt"
                    with open(log_file, 'w') as f:
                        f.write('\n'.join(logs))
                    log.info(f"Logs saved: {log_file} ({len(logs)} lines)")
                    
                except Exception as e:
                    log.warning(f"Failed to collect logs from {node['name']}: {e}")
    
    def run_benchmarks(self):
        """运行验证任务"""
        log.info("Running benchmarks...")
        all_passed = True
        
        env = os.environ.copy()
        if self.nodes:
            first_node = self.nodes[0]
            env['VLLM_HOST'] = first_node['service']['host']
            env['VLLM_PORT'] = str(first_node['service']['port'])
        
        for bench in self.config['benchmarks']:
            log.info(f"\n── Benchmark: {bench['name']} ──")
            
            script_path = self.resolve_script_path(bench['script'])
            
            try:
                result = subprocess.run(
                    ['bash', script_path],
                    env=env,
                    capture_output=True, text=True,
                    timeout=bench['timeout'],
                    cwd=self.config_dir
                )
                
                result_file = bench['result_file']
                if os.path.isfile(result_file):
                    with open(result_file) as f:
                        data = json.load(f)
                    
                    passed = self.check_threshold(data, bench['check'])
                    log.info(f"{bench['name']}: {'PASS' if passed else 'FAIL'}")
                    
                    if not passed:
                        all_passed = False
                        log.warning(f"  Check failed: {bench['check']}")
                        log.warning(f"  Actual: {data}")
                else:
                    log.error(f"Result file not found: {result_file}")
                    all_passed = False
                    
            except subprocess.TimeoutExpired:
                log.error(f"Benchmark {bench['name']} TIMEOUT")
                all_passed = False
        
        return all_passed
    
    def check_threshold(self, data, rules):
        """校验阈值"""
        for field, condition in rules.items():
            value = self._get_nested(data, field)
            if value is None:
                log.warning(f"Field '{field}' missing in result")
                return False
            
            if not self._eval_condition(value, condition):
                log.warning(f"{field}={value} does NOT satisfy {condition}")
                return False
        return True
    
    def _get_nested(self, data, path):
        """获取嵌套字段"""
        keys = path.split('.')
        current = data
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return None
        return current
    
    def _eval_condition(self, value, condition):
        """计算条件"""
        for op in ['>=', '<=', '!=', '==', '>', '<']:
            if condition.startswith(op):
                threshold = float(condition[len(op):].strip())
                value_num = float(value)
                ops = {
                    '>=': value_num >= threshold,
                    '<=': value_num <= threshold,
                    '>' : value_num > threshold,
                    '<' : value_num < threshold,
                    '==': value_num == threshold,
                    '!=': value_num != threshold,
                }
                return ops[op]
        return str(value) == condition.strip()
    
    def stop_all_nodes(self, service_info=None):
        """停止所有节点（改进日志显示）"""
        log.info("="*60)
        log.info("Stopping vLLM service...")
        log.info("="*60)
        
        if self.mode == "single_node":
            if service_info:
                # 显示服务日志摘要
                if service_info.get("service_logs"):
                    log.info(f"Service logs collected: {len(service_info['service_logs'])} lines")
                
                # 停止进程
                pid = service_info.get("single_node")
                process = service_info.get("process")
                
                if pid:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                        log.info(f"Sent SIGTERM to process group (PID {pid})")
                        # 等待进程优雅退出
                        if process:
                            try:
                                process.wait(timeout=15)
                                log.info("Service stopped gracefully")
                            except subprocess.TimeoutExpired:
                                log.warning("SIGTERM timeout, sending SIGKILL...")
                                try:
                                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                                    log.info("Service killed with SIGKILL")
                                except ProcessLookupError:
                                    log.info("Process already exited")
                    except ProcessLookupError:
                        log.info("Process not found (already stopped)")
            
            # 执行停止脚本（可选）
            node = self.nodes[0]
            stop_script = self.resolve_script_path(node['scripts']['stop'])
            try:
                subprocess.run(['bash', stop_script], timeout=30, capture_output=True)
                log.info("Stop script executed")
            except:
                pass
                
            log.info("="*60)
            log.info("Service stopped")
            log.info("="*60)
        else:
            for node in self.nodes:
                agent_url = f"http://{node['agent']['host']}:{node['agent']['port']}"
                
                try:
                    requests.post(
                        f"{agent_url}/stop",
                        json={
                            "script": node['scripts']['stop'],
                            "pid": pids.get(node['name']) if pids else None
                        },
                        timeout=30
                    )
                    log.info(f"{node['name']} stopped")
                except Exception as e:
                    log.warning(f"Stop error on {node['name']}: {e}")
    
    def cleanup(self):
        """清理：关闭自动启动的Agent"""
        if self.mode == "single_node" and self.agent_process:
            log.info("Cleaning up auto-started agent...")
            self.agent_process.terminate()
            try:
                self.agent_process.wait(timeout=5)
            except:
                self.agent_process.kill()
    
    def test_commit(self, commit_sha, commit_info=None):
        """测试单个commit"""
        log.info(f"\n{'='*60}")
        log.info(f"Testing commit: {commit_sha[:10]}")
        if commit_info:
            log.info(f"  Subject: {commit_info.get('subject', '')[:60]}")
            if commit_info.get('pr_number'):
                log.info(f"  PR: #{commit_info['pr_number']}")
        log.info(f"{'='*60}")
        
        if not self.checkout_all_nodes(commit_sha):
            return "fail"
        
        setup_result = self.setup_all_nodes()
        if setup_result == "fail":
            self.collect_logs_on_failure()
            return "fail"
        
        service_info = self.start_services()
        if not service_info:
            self.collect_logs_on_failure()
            return "fail"
        
        try:
            if not self.wait_services_ready(service_info):
                self.collect_logs_on_failure()
                return "fail"
            
            passed = self.run_benchmarks()
            result = "pass" if passed else "fail"
            
            if result == "fail":
                self.collect_logs_on_failure()
            
            return result
            
        finally:
            self.stop_all_nodes(service_info)
    
    def get_commits_between(self, good, bad):
        """获取commit列表"""
        log.info(f"Fetching commits between {good} and {bad}")
        
        repo_path = self.get_repo_path()
        
        if not os.path.isdir(repo_path):
            log.info(f"Cloning repo to {repo_path}")
            subprocess.run(['git', 'clone', self.repo_config['url'], repo_path],
                          check=True)
        
        subprocess.run(['git', '-C', repo_path, 'fetch', '--all'],
                      check=False, capture_output=True)
        
        result = subprocess.run(
            ['git', '-C', repo_path, 'log', '--first-parent', '--reverse',
             '--format=%H|%s|%an|%aI', f"{good}..{bad}"],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            log.error(f"git log failed: {result.stderr}")
            return []
        
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split('|', 3)
            if len(parts) < 4:
                continue
            sha, subject, author, date = parts
            
            pr_number = None
            if '(#' in subject:
                try:
                    pr_str = subject.rsplit('(#', 1)[1].rstrip(')')
                    pr_number = int(pr_str)
                except:
                    pass
            
            commits.append({
                'sha': sha,
                'subject': subject,
                'author': author,
                'date': date,
                'pr_number': pr_number
            })
        
        log.info(f"Found {len(commits)} commits")
        return commits
    
    def bisect(self):
        """核心二分逻辑"""
        if not self.start_agent_if_needed():
            log.error("Agents not ready. Please check configuration.")
            return
        
        good = self.config['bisect_range']['good_commit']
        bad = self.config['bisect_range']['bad_commit']
        
        commits = self.get_commits_between(good, bad)
        if not commits:
            log.error("No commits found")
            self.cleanup()
            return
        
        if not self.config['bisect_options']['skip_verify']:
            log.info("\n" + "="*60)
            log.info("Verifying GOOD commit...")
            log.info("="*60)
            if self.test_commit(good) != "pass":
                log.error("Good commit test FAILED!")
                self.cleanup()
                return
            
            log.info("\n" + "="*60)
            log.info("Verifying BAD commit...")
            log.info("="*60)
            if self.test_commit(bad) == "pass":
                log.error("Bad commit test PASSED! No regression.")
                self.cleanup()
                return
        
        log.info("\n" + "="*60)
        log.info("Starting BISECT...")
        log.info("="*60)
        
        lo, hi = 0, len(commits) - 1
        step = 0
        history = []
        
        while lo < hi:
            mid = (lo + hi) // 2
            commit = commits[mid]
            step += 1
            
            log.info(f"\nStep {step}/{len(commits).bit_length()}: index={mid}")
            
            result = self.test_commit(commit['sha'], commit)
            
            history.append({
                'step': step,
                'index': mid,
                'sha': commit['sha'],
                'subject': commit['subject'],
                'pr_number': commit['pr_number'],
                'result': result
            })
            
            if result == "pass":
                lo = mid + 1
            else:
                hi = mid
        
        bad_commit = commits[lo]
        log.info(f"\n{'='*60}")
        log.info(f"BISECT DONE")
        log.info(f"First bad commit: {bad_commit['sha'][:10]}")
        log.info(f"  Subject: {bad_commit['subject']}")
        if bad_commit['pr_number']:
            log.info(f"  PR: #{bad_commit['pr_number']}")
        log.info(f"  Author: {bad_commit['author']}")
        log.info(f"{'='*60}")
        
        result_file = Path(self.log_dir) / "bisect_result.json"
        with open(result_file, 'w') as f:
            json.dump({
                'status': 'found',
                'bad_commit': bad_commit,
                'total_steps': step,
                'total_commits': len(commits),
                'history': history,
                'timestamp': datetime.now().isoformat()
            }, f, indent=2)
        
        log.info(f"Result saved: {result_file}")
        
        self.cleanup()
        return bad_commit


def main():
    parser = argparse.ArgumentParser(description='vllm-ascend二分定位工具')
    parser.add_argument('--config', required=True, help='配置文件路径')
    args = parser.parse_args()
    
    tool = BisectTool(args.config)
    tool.bisect()


if __name__ == '__main__':
    main()