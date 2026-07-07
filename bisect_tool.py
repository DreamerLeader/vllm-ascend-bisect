#!/usr/bin/env python3
"""
vllm-ascend二分定位工具 - 支持单机混部和pd分离

核心特性：
  1. 单机混部优先：本地直接执行命令，最简单
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

# ── 日志配置：终端简洁 + 文件详细 ──
def setup_logging(log_dir):
    """配置双输出日志：终端（简洁）+ 文件（详细）"""
    
    # 创建日志目录
    Path(log_dir).mkdir(exist_ok=True)
    
    # 主日志文件（记录完整日志）
    log_file = Path(log_dir) / "bisect_full.log"
    
    # 配置root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # 全级别日志
    
    # 清除默认handler
    root_logger.handlers.clear()
    
    # ── 终端handler：只显示关键信息 ──
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    
    # 终端过滤器：只显示特定级别的关键信息
    class ConsoleFilter(logging.Filter):
        def filter(self, record):
            # 终端只显示：
            # - INFO级别：包含"===="、"Step"、"✓"、"✗"、"Still waiting"、"Testing commit"
            # - WARNING/ERROR级别
            if record.levelno >= logging.WARNING:
                return True
            if record.levelno == logging.INFO:
                keywords = ['====', 'Step', '✓', '✗', 'Still waiting', 'Testing commit', 
                           'Running setup', 'Starting vLLM', 'Health check', 'Setup SUCCESS',
                           'Setup FAILED', 'Service READY', 'Service NOT READY', 'Bisect DONE']
                return any(kw in record.getMessage() for kw in keywords)
            return False
    
    console_handler.addFilter(ConsoleFilter())
    root_logger.addHandler(console_handler)
    
    # ── 文件handler：记录所有详细日志 ──
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    root_logger.addHandler(file_handler)
    
    # 创建专用logger
    log = logging.getLogger(__name__)
    
    log.info(f"日志配置完成")
    log.info(f"  终端：显示关键进度（简洁）")
    log.info(f"  文件：{log_file}（完整详细日志）")
    
    return log, log_file

# 初始化日志（稍后在__init__中调用）
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
        
        # 配置双输出日志（终端简洁 + 文件详细）
        global log
        log, self.log_file = setup_logging(self.log_dir)
        
        self.mode = self.detect_mode()
        self.agent_process = None
        
        log.info("="*60)
        log.info(f"Mode detected: {self.mode}")
        log.info(f"Config dir: {self.config_dir}")
        log.info(f"Log dir: {self.log_dir}")
        log.info(f"Full log file: {self.log_file}")
        log.info("="*60)
        
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

    def should_skip_initial_verification(self):
        """Return whether to trust configured bisect bounds and start at midpoint."""
        options = self.config.get('bisect_options', {})
        if 'skip_initial_verification' in options:
            return bool(options['skip_initial_verification'])
        return bool(options.get('skip_verify', False))

    def should_install_vllm(self):
        """Return whether setup should inject vLLM installation before user script."""
        return bool(self.config.get('bisect_options', {}).get('install_vllm', True))
    
    def start_agent_if_needed(self):
        """按运行模式检查Agent；单机模式无需Agent。"""
        if self.mode == "single_node":
            # Single-node mode executes checkout/setup/start/benchmark locally.
            # Requiring an Agent here adds an unused Flask service dependency.
            log.info("Single-node mode uses local execution; skipping Agent startup")
            return True
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
        """执行安装脚本（自动配套vllm版本 + 详细日志）"""
        if self.mode == "single_node":
            node = self.nodes[0]
            script = self.resolve_script_path(node['scripts']['setup'])
            repo_path = self.get_repo_path()
            
            commit_short = subprocess.check_output(
                ['git', '-C', repo_path, 'rev-parse', '--short', 'HEAD'],
                timeout=5
            ).decode().strip()
            
            vllm_version = None
            if self.should_install_vllm():
                # ── 自动读取vllm版本（从docs/source/conf.py） ──
                vllm_version = self._extract_vllm_version(repo_path)

                if vllm_version:
                    log.info("="*60)
                    log.info(f"Auto-detected vllm version: {vllm_version}")
                    log.info(f"  Source: docs/source/conf.py")
                    log.info("="*60)

                # YAML可选覆盖（用户手动指定版本）
                manual_vllm_version = self.config.get('bisect_options', {}).get('vllm_version')
                if manual_vllm_version:
                    log.info(f"Using manual vllm version from YAML: {manual_vllm_version}")
                    vllm_version = manual_vllm_version
            else:
                log.info("Skipping vLLM auto-install (bisect_options.install_vllm=false)")
            
            # 创建setup专用日志文件
            setup_log_file = Path(self.log_dir) / f"setup_{commit_short}.log"
            
            # ── 读取代理配置（如果存在） ──
            proxy_env_file = self.config_dir / "proxy_env"
            proxy_env_content = ""
            
            if proxy_env_file.exists():
                log.info("="*60)
                log.info("Loading proxy configuration...")
                log.info(f"  File: {proxy_env_file}")
                log.info("="*60)
                
                with open(proxy_env_file) as f:
                    proxy_env_content = f.read()
                
                # 显示代理配置（终端简洁）
                log.info("Proxy config loaded (setup will use proxy)")
            
            log.info("="*60)
            log.info(f"Running setup script at commit {commit_short}")
            log.info(f"  Full logs: {setup_log_file}")
            log.info("="*60)
            
            env = os.environ.copy()
            env['BISECT_REPO_DIR'] = repo_path
            env['BISECT_COMMIT'] = subprocess.check_output(
                ['git', '-C', repo_path, 'rev-parse', 'HEAD'],
                timeout=5
            ).decode().strip()
            
            setup_start = time.time()
            
            try:
                # 构建setup命令：可选vllm安装 + vllm-ascend安装
                setup_cmd_parts = []
                
                # 1. vllm安装（如果有版本）
                if vllm_version:
                    setup_cmd_parts.append(f"pip install vllm=={vllm_version} -q")
                
                # 2. vllm-ascend安装（用户脚本）
                setup_cmd_parts.append(f"bash {script}")
                
                # 3. 代理配置（如果有）
                if proxy_env_content:
                    setup_cmd = f"""
# 设置代理
{proxy_env_content}

# 安装vllm和vllm-ascend
{'; '.join(setup_cmd_parts)}
"""
                else:
                    setup_cmd = '; '.join(setup_cmd_parts)
                
                # 启动setup进程
                process = subprocess.Popen(
                    setup_cmd, cwd=repo_path, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, shell=True
                )
                
                # 实时读取输出并写入日志文件
                with open(setup_log_file, 'w') as log_f:
                    log_f.write(f"Setup started at {datetime.now().isoformat()}\n")
                    log_f.write(f"Commit: {commit_short}\n")
                    log_f.write(f"Script: {script}\n")
                    if vllm_version:
                        log_f.write(f"vllm version: {vllm_version}\n")
                    if proxy_env_content:
                        log_f.write(f"Proxy config:\n{proxy_env_content}\n")
                    log_f.write("="*60 + "\n\n")
                    
                    import select
                    stdout_lines = []
                    
                    while process.poll() is None:
                        # 检查是否有输出可读
                        try:
                            ready_fds, _, _ = select.select([process.stdout], [], [], 1.0)
                            
                            for fd in ready_fds:
                                if fd == process.stdout:
                                    line = process.stdout.readline()
                                    if line:
                                        line = line.rstrip()
                                        stdout_lines.append(line)
                                        log_f.write(f"{line}\n")  # 写入文件
                                        log_f.flush()
                        except:
                            pass
                        
                        # 定期显示进度（终端简洁）
                        elapsed = time.time() - setup_start
                        if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                            remaining = max(0, 600 - elapsed)
                            log.info(f"Still running... ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining, {len(stdout_lines)} lines logged)")
                    
                    # 读取剩余输出
                    remaining_output = process.stdout.read()
                    if remaining_output:
                        for line in remaining_output.strip().splitlines():
                            stdout_lines.append(line)
                            log_f.write(f"{line}\n")
                    
                    log_f.write("\n" + "="*60 + "\n")
                    log_f.write(f"Setup finished at {datetime.now().isoformat()}\n")
                    log_f.write(f"Duration: {time.time() - setup_start:.1f}s\n")
                    log_f.write(f"Exit code: {process.returncode}\n")
                
                setup_elapsed = time.time() - setup_start
                
                if process.returncode != 0:
                    log.error("="*60)
                    log.error(f"✗ Setup FAILED (exit {process.returncode}) after {setup_elapsed:.1f}s")
                    log.error(f"  Full logs: {setup_log_file}")
                    log.error("="*60)
                    return "fail"
                
                log.info("="*60)
                log.info(f"✓ Setup SUCCESS ({setup_elapsed:.1f}s)")
                log.info(f"  Logs: {setup_log_file} ({len(stdout_lines)} lines)")
                log.info("="*60)
                
                # ── Setup完成后取消代理 ──
                if proxy_env_content:
                    log.info("Unsetting proxy (setup completed)")
                    # 取消代理环境变量
                    for var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy']:
                        if var in os.environ:
                            del os.environ[var]
                    log.info("✓ Proxy unset")
                
                return "ok"
                
            except subprocess.TimeoutExpired:
                setup_elapsed = time.time() - setup_start
                log.error("="*60)
                log.error(f"✗ Setup TIMEOUT after {setup_elapsed:.1f}s")
                log.error(f"  Logs: {setup_log_file}")
                log.error("="*60)
                if process:
                    process.kill()
                return "fail"
                
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
                log.error(f"✗ Setup TIMEOUT after {setup_elapsed:.1f}s")
                log.error(f"  Logs: {setup_log_file}")
                log.error("="*60)
                if process:
                    process.kill()
                return "fail"
    
    def _extract_vllm_version(self, repo_path):
        """从docs/source/conf.py提取vllm版本"""
        conf_file = Path(repo_path) / 'docs' / 'source' / 'conf.py'
        
        if not conf_file.exists():
            log.warning(f"  docs/source/conf.py not found, cannot auto-detect vllm version")
            return None
        
        try:
            with open(conf_file) as f:
                content = f.read()
            
            # 提取 pip_vllm_version = "0.17.0"
            import re
            match = re.search(r'pip_vllm_version["\']?\s*[:=]\s*["\']([\d.]+(?:rc\d+)?)["\']', content)
            
            if match:
                version = match.group(1)
                return version
            else:
                log.warning(f"  Cannot extract pip_vllm_version from conf.py")
                return None
                
        except Exception as e:
            log.warning(f"  Failed to read conf.py: {e}")
            return None
    
    def start_services(self):
        """启动服务（最简化：只执行bash start.sh）"""
        if self.mode == "single_node":
            node = self.nodes[0]
            script = self.resolve_script_path(node['scripts']['start'])
            
            log.info("="*60)
            log.info("Starting vLLM service...")
            log.info(f"  Executing: bash {script}")
            log.info("="*60)
            
            try:
                # 最简化：只执行bash脚本，不注入任何环境变量，不干预
                subprocess.Popen(
                    ['bash', script],
                    stdout=subprocess.DEVNULL,  # 不收集输出
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid       # 后台进程组
                )
                
                log.info("✓ Start script executed (background)")
                log.info(f"  Service: http://{node['service']['host']}:{node['service']['port']}")
                log.info("  Next: health check...")
                
                return {"status": "started"}
                
            except Exception as e:
                log.error(f"✗ Failed to execute start script: {e}")
                return None
        else:
            log.info("="*60)
            log.info("Starting services on all nodes...")
            log.info("="*60)
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
        """等待服务就绪（简化：只做curl健康检查）"""
        log.info("="*60)
        log.info("Waiting for vLLM service to be ready...")
        log.info("="*60)
        
        timeout = self.config['bisect_options']['health_check_timeout']
        interval = self.config['bisect_options']['health_check_interval']
        
        for node in self.nodes:
            service_url = f"http://{node['service']['host']}:{node['service']['port']}"
            health_endpoint = node['service'].get('health_endpoint', '/health')
            url = f"{service_url}{health_endpoint}"
            
            deadline = time.time() + timeout
            log.info(f"Health check URL: {url}")
            
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                
                # 只做HTTP健康检查（curl判断）
                try:
                    response = requests.get(url, timeout=2)
                    if response.status_code == 200:
                        log.info("="*60)
                        log.info(f"✓ Service READY! (after {attempt} checks, {time.time() - (deadline - timeout):.1f}s)")
                        log.info("="*60)
                        return True
                except:
                    pass
                
                # 定期显示进度
                if attempt % 15 == 0:
                    elapsed = time.time() - (deadline - timeout)
                    remaining = deadline - time.time()
                    log.info(f"Still waiting... (attempt {attempt}, {elapsed:.0f}s elapsed, {remaining:.0f}s remaining)")
                
                time.sleep(interval)
            
            log.error("="*60)
            log.error(f"✗ Service NOT READY after {timeout}s ({attempt} attempts)")
            log.error("="*60)
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
        """运行验证任务（日志保存到文件，支持output文件夹）"""
        log.info("="*60)
        log.info("Running benchmarks...")
        log.info("="*60)
        
        all_passed = True
        
        # 获取当前commit（用于日志文件名）
        repo_path = self.get_repo_path()
        commit_short = subprocess.check_output(
            ['git', '-C', repo_path, 'rev-parse', '--short', 'HEAD'],
            timeout=5
        ).decode().strip()
        
        # 设置环境变量（供脚本使用）
        env = os.environ.copy()
        if self.nodes:
            first_node = self.nodes[0]
            env['VLLM_HOST'] = first_node['service']['host']
            env['VLLM_PORT'] = str(first_node['service']['port'])
        
        for bench in self.config['benchmarks']:
            # 检查是否启用（可选配置）
            if bench.get('enable') is False:
                log.info(f"Skipping {bench['name']} (disabled)")
                continue
            
            log.info(f"\n── Benchmark: {bench['name']} ──")
            
            script_path = self.resolve_script_path(bench['script'])
            
            # 创建benchmark日志文件
            bench_log_file = Path(self.log_dir) / f"benchmark_{bench['name']}_{commit_short}.log"
            
            log.info(f"  Script: {script_path}")
            log.info(f"  Logs: {bench_log_file}")
            
            bench_start = time.time()
            
            try:
                # 执行benchmark脚本，保存日志
                result = subprocess.run(
                    ['bash', script_path],
                    env=env,
                    capture_output=True, text=True,
                    timeout=bench['timeout'],
                    cwd=self.config_dir  # 在配置文件夹运行（output文件夹在这里）
                )
                
                bench_elapsed = time.time() - bench_start
                
                # 保存完整日志到文件
                with open(bench_log_file, 'w') as f:
                    f.write(f"Benchmark: {bench['name']}\n")
                    f.write(f"Commit: {commit_short}\n")
                    f.write(f"Script: {script_path}\n")
                    f.write(f"Timeout: {bench['timeout']}s\n")
                    f.write(f"Duration: {bench_elapsed:.1f}s\n")
                    f.write(f"Exit code: {result.returncode}\n")
                    f.write("\n" + "="*60 + "\n")
                    f.write("STDOUT:\n")
                    f.write(result.stdout)
                    f.write("\n" + "="*60 + "\n")
                    f.write("STDERR:\n")
                    f.write(result.stderr)
                    f.write("\n" + "="*60 + "\n")
                
                log.info(f"  Duration: {bench_elapsed:.1f}s")
                log.info(f"  Exit code: {result.returncode}")
                
                if result.returncode != 0:
                    log.warning(f"  ⚠ Benchmark exited with non-zero code")
                    # 显示stderr摘要
                    if result.stderr:
                        log.warning(f"  stderr tail: {result.stderr[-200:]}")
                
                # 解析结果文件（支持复杂目录结构）
                result_file_config = bench['result_file']
                
                # ── 智能查找结果文件 ──
                result_file_abs = None
                
                # 情况1: 配置中明确指定了结果文件路径
                if result_file_config.startswith('/'):
                    # 绝对路径
                    result_file_abs = Path(result_file_config)
                    
                elif result_file_config.startswith('./outputs') or result_file_config == './outputs':
                    # outputs文件夹：自动查找最新的时间文件夹
                    # 真实结构：outputs/default/20260508_171903/results/vllm-api-stream-chat/*.json
                    outputs_dir = self.config_dir / 'outputs'
                    
                    if outputs_dir.exists():
                        # 找到outputs下default文件夹
                        default_dir = outputs_dir / 'default'
                        
                        if default_dir.exists():
                            # 找到default下所有时间文件夹（如 20260508_171903）
                            time_folders = sorted(
                                [d for d in default_dir.iterdir() if d.is_dir()],
                                key=lambda x: x.name,  # 按名称排序（时间格式）
                                reverse=True  # 最新的在前
                            )
                            
                            if time_folders:
                                latest_folder = time_folders[0]  # 取最新的
                                
                                # 在最新文件夹下查找results/vllm-api-stream-chat/*.json
                                results_dir = latest_folder / 'results' / 'vllm-api-stream-chat'
                                
                                if results_dir.exists():
                                    # 找到所有json文件，取最新的
                                    json_files = sorted(
                                        [f for f in results_dir.glob('*.json')],
                                        key=lambda x: x.stat().st_mtime,
                                        reverse=True
                                    )
                                    
                                    if json_files:
                                        result_file_abs = json_files[0]  # 取最新的json文件
                                        log.info(f"  Found: outputs/default/{latest_folder.name}/results/vllm-api-stream-chat/{result_file_abs.name}")
                                    else:
                                        log.error(f"  ✗ No JSON files in {results_dir}")
                                else:
                                    log.error(f"  ✗ Results dir not found: outputs/default/{latest_folder.name}/results/vllm-api-stream-chat/")
                            else:
                                log.error(f"  ✗ No time folders in outputs/default/")
                        else:
                            log.error(f"  ✗ outputs/default/ folder not found")
                    else:
                        log.error(f"  ✗ outputs folder not found")
                        
                else:
                    # 其他相对路径：基于config_dir
                    result_file_abs = self.config_dir / result_file_config
                
                # ── 解析结果并校验 ──
                if result_file_abs and result_file_abs.exists():
                    with open(result_file_abs) as f:
                        data = json.load(f)
                    
                    log.info(f"  Result file: {result_file_abs.name}")
                    
                    # 校验阈值
                    passed = self.check_threshold(data, bench['check'])
                    
                    log.info(f"  Status: {'PASS' if passed else 'FAIL'}")
                    
                    if not passed:
                        all_passed = False
                        log.warning(f"  ⚠ Check failed: {bench['check']}")
                        log.warning(f"  Actual result: {data}")
                elif 'result_file' not in bench or not bench.get('result_file'):
                    # 没有配置result_file，尝试从日志提取关键词
                    log.info(f"  Extracting from log (no result_file)")
                    
                    # 从benchmark日志文件提取关键词（如TTFT）
                    keywords_data = self._extract_keywords_from_log(
                        bench_log_file, bench.get('keywords', [])
                    )
                    
                    if keywords_data:
                        log.info(f"  Extracted keywords: {keywords_data}")
                        
                        # 校验关键词阈值
                        passed = self.check_threshold(keywords_data, bench['check'])
                        
                        log.info(f"  Status: {'PASS' if passed else 'FAIL'}")
                        
                        if not passed:
                            all_passed = False
                            log.warning(f"  ⚠ Check failed: {bench['check']}")
                            log.warning(f"  Actual result: {keywords_data}")
                    else:
                        log.error(f"  ✗ Failed to extract keywords from log")
                        all_passed = False
                else:
                    log.error(f"  ✗ Result file not found")
                    log.error(f"    Expected: outputs/default/{时间}/results/vllm-api-stream-chat/*.json")
                    all_passed = False
                    
            except subprocess.TimeoutExpired:
                bench_elapsed = time.time() - bench_start
                log.error(f"  ✗ TIMEOUT after {bench_elapsed:.1f}s (limit {bench['timeout']}s)")
                
                # 保存超时日志
                with open(bench_log_file, 'w') as f:
                    f.write(f"Benchmark: {bench['name']}\n")
                    f.write(f"Status: TIMEOUT\n")
                    f.write(f"Timeout limit: {bench['timeout']}s\n")
                    f.write(f"Actual duration: {bench_elapsed:.1f}s\n")
                
                all_passed = False
        
        log.info("="*60)
        log.info(f"Benchmarks completed: {'ALL PASS' if all_passed else 'FAILED'}")
        log.info("="*60)
        
        return all_passed
    
    def _extract_keywords_from_log(self, log_file, keywords_config):
        """从benchmark日志文件提取关键词（如TTFT）"""
        if not log_file.exists():
            return None
        
        try:
            with open(log_file) as f:
                log_content = f.read()
            
            extracted_data = {}
            
            # 支持的关键词格式：
            # TTFT: 123.4 ms
            # Throughput: 100.5 tokens/sec
            # Latency: 150.2 ms
            
            import re
            
            # TTFT提取（主要需求）
            ttft_match = re.search(r'TTFT:\s+(\d+\.?\d*)\s+ms', log_content)
            if ttft_match:
                extracted_data['ttft'] = float(ttft_match.group(1))
            
            # Throughput提取（可选）
            throughput_match = re.search(r'Throughput:\s+(\d+\.?\d*)\s+tokens/sec', log_content)
            if throughput_match:
                extracted_data['throughput'] = float(throughput_match.group(1))
            
            # Latency提取（可选）
            latency_match = re.search(r'Latency:\s+(\d+\.?\d*)\s+ms', log_content)
            if latency_match:
                extracted_data['latency'] = float(latency_match.group(1))
            
            return extracted_data if extracted_data else None
            
        except Exception as e:
            log.warning(f"  Failed to extract keywords: {e}")
            return None
    
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
        """停止服务（简化：只执行stop脚本）"""
        log.info("="*60)
        log.info("Stopping vLLM service...")
        log.info("="*60)
        
        if self.mode == "single_node":
            # 执行停止脚本（清理服务）
            node = self.nodes[0]
            stop_script = self.resolve_script_path(node['scripts']['stop'])
            
            try:
                subprocess.run(['bash', stop_script], timeout=30, capture_output=True)
                log.info("✓ Stop script executed")
            except Exception as e:
                log.warning(f"Stop script error: {e}")
            
            log.info("="*60)
            log.info("✓ Service stopped (stop.sh completed)")
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
        
        if not self.should_skip_initial_verification():
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
        else:
            log.info("\n" + "="*60)
            log.info("Skipping initial good/bad verification; starting bisect from midpoint")
            log.info("="*60)
        
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
