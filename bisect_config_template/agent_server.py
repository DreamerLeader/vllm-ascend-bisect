#!/usr/bin/env python3
"""
Agent服务 - 部署在各机器上，接收主工具的HTTP指令

功能：
  1. 接收主工具的HTTP指令（checkout/setup/start/stop）
  2. 执行对应的shell脚本
  3. 提供日志收集接口（/logs）
  4. 监控服务进程状态

使用方法：
  python agent_server.py --port 8080 --repo-path /home/user/vllm-ascend

依赖：
  pip install flask
"""

from flask import Flask, request, jsonify
import subprocess, os, argparse, logging, signal, threading, queue
from pathlib import Path

app = Flask(__name__)
REPO_PATH = ""
LOG_QUEUE = queue.Queue(maxsize=1000)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)


class LogCollector(threading.Thread):
    """后台线程：收集最近1000行日志"""
    
    def __init__(self):
        super().__init__(daemon=True)
        self.logs = []
        
    def add_log(self, source, message):
        entry = f"[{source}] {message}"
        if len(self.logs) >= 1000:
            self.logs.pop(0)
        self.logs.append(entry)
        
    def get_logs(self, max_lines=100):
        return self.logs[-max_lines:]
        
    def run(self):
        while True:
            try:
                entry = LOG_QUEUE.get(timeout=1)
                self.add_log(*entry)
            except queue.Empty:
                pass


log_collector = LogCollector()


def add_log(source, message):
    log_collector.add_log(source, message)
    log.info(f"[{source}] {message}")


@app.route('/checkout', methods=['POST'])
def checkout():
    """切换到指定commit"""
    commit = request.json['commit']
    add_log('checkout', f"Checkout to {commit}")
    
    try:
        result = subprocess.run(
            ['git', '-C', REPO_PATH, 'checkout', '-f', commit],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            add_log('checkout', f"FAILED: {result.stderr}")
            return jsonify({"status": "fail", "error": result.stderr}), 500
        
        subprocess.run(
            ['git', '-C', REPO_PATH, 'clean', '-fd',
             '--exclude=.venv', '--exclude=venv'],
            capture_output=True, timeout=30
        )
        
        add_log('checkout', f"SUCCESS")
        return jsonify({"status": "ok", "commit": commit})
        
    except Exception as e:
        add_log('checkout', f"ERROR: {e}")
        return jsonify({"status": "fail", "error": str(e)}), 500


@app.route('/setup', methods=['POST'])
def setup():
    """执行安装脚本"""
    script = request.json.get('script', '/home/user/scripts/setup.sh')
    add_log('setup', f"Running: {script}")
    
    env = os.environ.copy()
    env['BISECT_REPO_DIR'] = REPO_PATH
    env['BISECT_COMMIT'] = subprocess.check_output(
        ['git', '-C', REPO_PATH, 'rev-parse', 'HEAD'],
        timeout=5
    ).decode().strip()
    
    try:
        result = subprocess.run(
            ['bash', script], cwd=REPO_PATH, env=env,
            capture_output=True, text=True, timeout=600
        )
        
        for line in result.stdout.splitlines()[-50:]:
            add_log('setup', line)
        if result.stderr:
            for line in result.stderr.splitlines()[-20:]:
                add_log('setup-stderr', line)
        
        if result.returncode != 0:
            add_log('setup', f"FAILED (exit {result.returncode})")
            return jsonify({
                "status": "fail",
                "error": result.stderr[-500:]
            }), 500
        
        add_log('setup', "SUCCESS")
        return jsonify({"status": "ok"})
        
    except subprocess.TimeoutExpired:
        add_log('setup', "TIMEOUT")
        return jsonify({"status": "fail", "error": "timeout"}), 500
    except Exception as e:
        add_log('setup', f"ERROR: {e}")
        return jsonify({"status": "fail", "error": str(e)}), 500


@app.route('/start', methods=['POST'])
def start():
    """启动服务脚本（后台进程）"""
    script = request.json.get('script', '/home/user/scripts/start.sh')
    add_log('start', f"Starting: {script}")
    
    env = os.environ.copy()
    env['BISECT_REPO_DIR'] = REPO_PATH
    
    try:
        process = subprocess.Popen(
            ['bash', script], cwd=REPO_PATH, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid
        )
        
        add_log('start', f"SUCCESS (PID {process.pid})")
        
        def monitor_logs():
            try:
                for raw_line in iter(process.stdout.readline, b""):
                    line = raw_line.decode(errors='replace').rstrip()
                    add_log('vllm', line)
            except:
                pass
        
        threading.Thread(target=monitor_logs, daemon=True).start()
        
        return jsonify({"status": "ok", "pid": process.pid})
        
    except Exception as e:
        add_log('start', f"ERROR: {e}")
        return jsonify({"status": "fail", "error": str(e)}), 500


@app.route('/stop', methods=['POST'])
def stop():
    """停止服务"""
    script = request.json.get('script')
    pid = request.json.get('pid')
    
    add_log('stop', f"Stopping service...")
    
    try:
        if script:
            subprocess.run(['bash', script], cwd=REPO_PATH, timeout=30)
        
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                add_log('stop', f"Sent SIGTERM to PGID {pid}")
            except ProcessLookupError:
                add_log('stop', f"Process {pid} not found")
        
        add_log('stop', "SUCCESS")
        return jsonify({"status": "ok"})
        
    except Exception as e:
        add_log('stop', f"ERROR: {e}")
        return jsonify({"status": "fail", "error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """检查Agent自身健康"""
    try:
        commit = subprocess.check_output(
            ['git', '-C', REPO_PATH, 'rev-parse', '--short', 'HEAD'],
            timeout=5
        ).decode().strip()
        return jsonify({"status": "ok", "repo": REPO_PATH, "commit": commit})
    except:
        return jsonify({"status": "ok", "repo": REPO_PATH})


@app.route('/logs', methods=['GET'])
def get_logs():
    """获取最近的日志"""
    max_lines = request.args.get('max_lines', 100, type=int)
    logs = log_collector.get_logs(max_lines)
    return jsonify({"logs": logs})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Agent服务')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--repo-path', required=True)
    args = parser.parse_args()
    
    REPO_PATH = args.repo_path
    
    if not os.path.isdir(REPO_PATH):
        log.error(f"Repo path not found: {REPO_PATH}")
        exit(1)
    
    log_collector.start()
    
    log.info(f"Agent starting on port {args.port}, repo: {REPO_PATH}")
    app.run(host='0.0.0.0', port=args.port, threaded=True)