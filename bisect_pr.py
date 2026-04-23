#!/usr/bin/env python3
"""
vllm-ascend PR-level bisect tool.

开发提供测试脚本，工具在 good..bad 之间按 commit 做二分查找，
每个 commit checkout 后运行开发的脚本，定位引入问题的 PR。

支持两种方式提供测试逻辑:
  1. 脚本文件:  --test-script ./my_test.sh
  2. 内联命令:  --cmd "python -m pytest tests/test_foo.py -x"

Usage:
    python bisect_pr.py \
        --repo-dir /path/to/vllm-ascend \
        --good <good_commit> \
        --bad <bad_commit> \
        --test-script ./dev_test.sh

    python bisect_pr.py \
        --repo-dir /path/to/vllm-ascend \
        --good abc1234 --bad main \
        --cmd "python -m pytest tests/e2e/test_llama.py -x" \
        --setup-cmd "pip install -e . --no-deps -q"
"""

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class CommitInfo:
    sha: str
    subject: str
    pr_number: Optional[int] = None
    author: str = ""
    date: str = ""


@dataclass
class BisectResult:
    status: str  # "found", "error", "no_regression"
    bad_commit: Optional[CommitInfo] = None
    total_steps: int = 0
    total_commits: int = 0
    history: list = field(default_factory=list)
    elapsed_seconds: float = 0.0


# ── Git helpers ──────────────────────────────────────────────────────────────


def run_git(repo_dir: str, *args: str) -> str:
    cmd = ["git", "-C", repo_dir] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"git failed: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def resolve_ref(repo_dir: str, ref: str) -> str:
    """Resolve a ref (branch/tag/sha) to full SHA."""
    return run_git(repo_dir, "rev-parse", ref)


def get_commits_between(repo_dir: str, good: str, bad: str) -> list[CommitInfo]:
    """
    获取 good(不含) 到 bad(含) 之间的所有 first-parent commits，
    按时间正序排列（最早的在前）。
    first-parent 保证每个 commit 对应一个合入的 PR（无论 squash merge 还是 merge commit）。
    """
    log_output = run_git(
        repo_dir,
        "log", "--first-parent", "--reverse",
        "--format=%H|%s|%an|%aI",
        f"{good}..{bad}",
    )
    if not log_output:
        return []

    commits = []
    for line in log_output.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        sha, subject, author, date = parts

        # 从 commit message 提取 PR 号, 格式: "... (#1234)"
        pr_number = _extract_pr_number(subject)

        commits.append(CommitInfo(
            sha=sha, subject=subject,
            pr_number=pr_number, author=author, date=date,
        ))
    return commits


def _extract_pr_number(subject: str) -> Optional[int]:
    if "(#" not in subject:
        return None
    try:
        pr_str = subject.rsplit("(#", 1)[1].rstrip(")")
        return int(pr_str)
    except (ValueError, IndexError):
        return None


def checkout_commit(repo_dir: str, sha: str) -> None:
    run_git(repo_dir, "checkout", "--force", sha)
    run_git(repo_dir, "clean", "-fd", "--exclude=.venv", "--exclude=venv")


def save_current_ref(repo_dir: str) -> str:
    try:
        ref = run_git(repo_dir, "symbolic-ref", "--short", "HEAD")
    except RuntimeError:
        ref = run_git(repo_dir, "rev-parse", "HEAD")
    return ref


# ── Test execution ───────────────────────────────────────────────────────────


def run_command(
    cmd: str | list[str],
    cwd: str,
    timeout: int,
    env: dict | None = None,
    label: str = "command",
) -> tuple[int, str]:
    """
    运行命令，返回 (returncode, combined_output)。
    支持 shell 字符串和列表两种形式。
    """
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    use_shell = isinstance(cmd, str)
    log.info("  [%s] %s", label, cmd if use_shell else " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, shell=use_shell, cwd=cwd, env=run_env,
            capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, f"[TIMEOUT: exceeded {timeout}s]"
    except Exception as e:
        return -2, f"[EXECUTION ERROR: {e}]"


def run_test_at_commit(
    repo_dir: str,
    sha: str,
    test_cmd: str | list[str],
    setup_cmd: str | list[str] | None,
    timeout: int,
    log_dir: str | None = None,
) -> tuple[str, str]:
    """
    在指定 commit 上运行测试。

    Returns:
        (result, output) where result is "pass", "fail", or "skip"
        - pass: test script exit 0
        - fail: test script exit non-0
        - skip: setup failed (编译失败等), 该 commit 无法测试
    """
    env = {"BISECT_REPO_DIR": repo_dir, "BISECT_COMMIT": sha}

    # checkout
    checkout_commit(repo_dir, sha)

    # setup (if provided)
    if setup_cmd:
        rc, output = run_command(setup_cmd, cwd=repo_dir, timeout=timeout, env=env, label="setup")
        if rc != 0:
            log.warning("  Setup failed (rc=%d), SKIP this commit", rc)
            _save_log(log_dir, sha, "setup_fail", output)
            return "skip", output

    # run test
    rc, output = run_command(test_cmd, cwd=repo_dir, timeout=timeout, env=env, label="test")
    result = "pass" if rc == 0 else "fail"

    _save_log(log_dir, sha, result, output)
    return result, output


def _save_log(log_dir: str | None, sha: str, result: str, output: str):
    if not log_dir:
        return
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{sha[:10]}_{result}.log")
    with open(path, "w") as f:
        f.write(output)


# ── Core bisect ──────────────────────────────────────────────────────────────


def bisect(
    repo_dir: str,
    good: str,
    bad: str,
    test_cmd: str | list[str],
    setup_cmd: str | list[str] | None = None,
    timeout: int = 600,
    log_dir: str | None = None,
) -> BisectResult:
    """
    核心二分查找。

    对 good..bad 之间的 first-parent commits 做二分:
    - checkout 到某个 commit
    - 运行 setup_cmd (可选, 如 pip install)
    - 运行 test_cmd (开发提供的测试脚本/命令)
    - exit 0 = pass, 非0 = fail
    - setup 失败 = skip (跳过该 commit, 尝试相邻的)
    """
    start_time = time.time()
    original_ref = save_current_ref(repo_dir)

    commits = get_commits_between(repo_dir, good, bad)
    if not commits:
        return BisectResult(status="no_regression", total_commits=0)

    n = len(commits)
    log.info("Found %d commits between good..bad, need ~%d steps", n, n.bit_length())

    history = []
    lo, hi = 0, n - 1
    step = 0

    try:
        while lo < hi:
            mid = (lo + hi) // 2
            commit = commits[mid]
            step += 1

            log.info(
                "── Step %d: [%d..%d] testing index %d ── %s  %s",
                step, lo, hi, mid, commit.sha[:10], commit.subject[:60],
            )

            result, output = run_test_at_commit(
                repo_dir, commit.sha, test_cmd, setup_cmd, timeout, log_dir,
            )

            record = {
                "step": step, "index": mid,
                "sha": commit.sha, "subject": commit.subject,
                "pr_number": commit.pr_number, "author": commit.author,
                "result": result,
                "output_tail": output[-1000:] if output else "",
            }
            history.append(record)

            if result == "skip":
                # setup 失败, 尝试向两侧寻找可测试的 commit
                log.info("  SKIP — trying neighbors")
                resolved = _resolve_skip(
                    commits, lo, hi, mid, repo_dir,
                    test_cmd, setup_cmd, timeout, log_dir, history, step,
                )
                if resolved is None:
                    log.error("  All neighbors also skip, cannot continue")
                    break
                new_mid, new_result, step = resolved
                if new_result == "pass":
                    lo = new_mid + 1
                else:
                    hi = new_mid
            elif result == "pass":
                log.info("  PASS → search right half")
                lo = mid + 1
            else:
                log.info("  FAIL → search left half")
                hi = mid

        bad_commit = commits[lo]
        elapsed = time.time() - start_time

        log.info("=" * 60)
        log.info("BISECT DONE in %d steps (%.0fs)", step, elapsed)
        log.info("First bad commit: %s", bad_commit.sha[:10])
        log.info("  Subject: %s", bad_commit.subject)
        log.info("  PR: #%s  Author: %s", bad_commit.pr_number or "?", bad_commit.author)
        log.info("=" * 60)

        return BisectResult(
            status="found", bad_commit=bad_commit,
            total_steps=step, total_commits=n,
            history=history, elapsed_seconds=elapsed,
        )

    except Exception as e:
        log.error("Bisect error: %s", e, exc_info=True)
        return BisectResult(
            status="error", total_steps=step, total_commits=n,
            history=history, elapsed_seconds=time.time() - start_time,
        )

    finally:
        try:
            run_git(repo_dir, "checkout", "--force", original_ref)
        except RuntimeError:
            pass


def _resolve_skip(
    commits, lo, hi, mid, repo_dir,
    test_cmd, setup_cmd, timeout, log_dir, history, step,
):
    """当 mid 被 skip 时, 尝试相邻 commit, 交替向左右扩展。"""
    for offset in range(1, hi - lo + 1):
        for candidate in [mid + offset, mid - offset]:
            if candidate < lo or candidate > hi:
                continue
            commit = commits[candidate]
            step += 1
            log.info("  Trying neighbor index %d: %s", candidate, commit.sha[:10])

            result, output = run_test_at_commit(
                repo_dir, commit.sha, test_cmd, setup_cmd, timeout, log_dir,
            )
            history.append({
                "step": step, "index": candidate,
                "sha": commit.sha, "subject": commit.subject,
                "pr_number": commit.pr_number, "author": commit.author,
                "result": result,
                "output_tail": output[-1000:] if output else "",
            })

            if result != "skip":
                return candidate, result, step

    return None


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="vllm-ascend PR 级别二分定位",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 开发提供测试脚本文件
  python bisect_pr.py \\
      --repo-dir ./vllm-ascend \\
      --good v0.7.0 --bad main \\
      --test-script ./dev_test.sh

  # 开发提供内联测试命令
  python bisect_pr.py \\
      --repo-dir ./vllm-ascend \\
      --good abc1234 --bad def5678 \\
      --cmd "python -m pytest tests/e2e/test_llama.py -x" \\
      --setup-cmd "pip install -e . --no-deps -q"

  # 指定 setup 脚本 + 超时时间
  python bisect_pr.py \\
      --repo-dir ./vllm-ascend \\
      --good v0.6.0 --bad v0.7.0 \\
      --test-script ./test_tp2.sh \\
      --setup-script ./install.sh \\
      --timeout 1200
        """,
    )
    parser.add_argument("--repo-dir", required=True, help="vllm-ascend 仓库路径")
    parser.add_argument("--good", required=True, help="已知正常的 commit/tag")
    parser.add_argument("--bad", required=True, help="已知异常的 commit/tag")

    test_group = parser.add_mutually_exclusive_group(required=True)
    test_group.add_argument("--test-script", help="开发提供的测试脚本文件路径 (exit 0=pass)")
    test_group.add_argument("--cmd", help="内联测试命令 (exit 0=pass)")

    setup_group = parser.add_mutually_exclusive_group()
    setup_group.add_argument("--setup-script", help="每个 commit 的环境安装脚本")
    setup_group.add_argument("--setup-cmd", help="每个 commit 的环境安装命令")

    parser.add_argument("--timeout", type=int, default=600, help="每轮超时秒数 (默认 600)")
    parser.add_argument("--output", default="bisect_result.json", help="结果输出文件")
    parser.add_argument("--log-dir", default="bisect_logs", help="每个 commit 的日志目录")
    parser.add_argument("--github-repo", default="vllm-project/vllm-ascend", help="GitHub 仓库")
    parser.add_argument(
        "--skip-verify", action="store_true",
        help="跳过 good/bad 验证 (确认无误时使用, 更快)",
    )

    args = parser.parse_args()

    # 解析 test 命令
    if args.test_script:
        test_script = os.path.abspath(args.test_script)
        if not os.path.isfile(test_script):
            log.error("测试脚本不存在: %s", test_script)
            sys.exit(1)
        os.chmod(test_script, 0o755)
        test_cmd = test_script
    else:
        test_cmd = args.cmd

    # 解析 setup 命令
    setup_cmd = None
    if args.setup_script:
        setup_cmd = os.path.abspath(args.setup_script)
        if not os.path.isfile(setup_cmd):
            log.error("安装脚本不存在: %s", setup_cmd)
            sys.exit(1)
        os.chmod(setup_cmd, 0o755)
    elif args.setup_cmd:
        setup_cmd = args.setup_cmd

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        log.error("仓库目录不存在: %s", repo_dir)
        sys.exit(1)

    log_dir = os.path.abspath(args.log_dir)

    # fetch latest
    log.info("Fetching latest commits ...")
    try:
        run_git(repo_dir, "fetch", "--all", "--prune")
    except RuntimeError as e:
        log.warning("git fetch failed: %s", e)

    # 验证 good/bad
    if not args.skip_verify:
        log.info("验证 good commit: %s", args.good)
        result, output = run_test_at_commit(
            repo_dir, resolve_ref(repo_dir, args.good),
            test_cmd, setup_cmd, args.timeout,
        )
        if result != "pass":
            log.error("Good commit 未通过测试! result=%s", result)
            log.error("输出: %s", output[-500:])
            sys.exit(1)
        log.info("Good commit 验证通过")

        log.info("验证 bad commit: %s", args.bad)
        result, output = run_test_at_commit(
            repo_dir, resolve_ref(repo_dir, args.bad),
            test_cmd, setup_cmd, args.timeout,
        )
        if result == "pass":
            log.error("Bad commit 测试通过了! 不需要二分。")
            sys.exit(1)
        log.info("Bad commit 验证通过 (确实失败)")

    # 二分
    bisect_result = bisect(
        repo_dir=repo_dir,
        good=args.good,
        bad=args.bad,
        test_cmd=test_cmd,
        setup_cmd=setup_cmd,
        timeout=args.timeout,
        log_dir=log_dir,
    )

    # 保存结果
    output_data = {
        "status": bisect_result.status,
        "bad_commit": asdict(bisect_result.bad_commit) if bisect_result.bad_commit else None,
        "total_steps": bisect_result.total_steps,
        "total_commits": bisect_result.total_commits,
        "elapsed_seconds": bisect_result.elapsed_seconds,
        "history": bisect_result.history,
        "github_repo": args.github_repo,
        "timestamp": datetime.now().isoformat(),
        "test_cmd": test_cmd if isinstance(test_cmd, str) else " ".join(test_cmd),
        "setup_cmd": setup_cmd if isinstance(setup_cmd, str) else (" ".join(setup_cmd) if setup_cmd else None),
    }

    if bisect_result.bad_commit and bisect_result.bad_commit.pr_number:
        output_data["pr_url"] = (
            f"https://github.com/{args.github_repo}/pull/{bisect_result.bad_commit.pr_number}"
        )

    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    # 输出结果
    if bisect_result.status == "found":
        c = bisect_result.bad_commit
        print(f"\n{'='*60}")
        print(f"  定位完成: {bisect_result.total_steps} 步 / {bisect_result.total_commits} 个 commit")
        print(f"  耗时: {bisect_result.elapsed_seconds:.0f}s")
        print(f"")
        print(f"  问题 commit: {c.sha[:10]}")
        print(f"  描述: {c.subject}")
        print(f"  作者: {c.author}")
        if c.pr_number:
            print(f"  PR: https://github.com/{args.github_repo}/pull/{c.pr_number}")
        print(f"")
        print(f"  结果已保存: {args.output}")
        print(f"  日志目录: {log_dir}")
        print(f"{'='*60}")
        sys.exit(0)
    else:
        log.error("未能定位到问题 commit (status=%s)", bisect_result.status)
        sys.exit(1)


if __name__ == "__main__":
    main()
