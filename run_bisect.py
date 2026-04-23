#!/usr/bin/env python3
"""
一键二分定位 + Agent 分析。

开发提供测试脚本/命令，工具自动完成:
  1. 在 good..bad 之间按 commit 做二分
  2. 每个 commit 运行开发的脚本
  3. 定位到引入问题的 PR
  4. (可选) 调用 Claude 分析根因

Usage:
    python run_bisect.py \
        --repo-dir ./vllm-ascend \
        --good v0.7.0 --bad main \
        --test-script ./dev_test.sh \
        [--setup-cmd "pip install -e . -q"] \
        [--analyze]
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict

from bisect_pr import bisect, resolve_ref, run_git, run_test_at_commit
from agent_analyzer import analyze_with_claude, fetch_pr_info, generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="vllm-ascend 一键二分定位 + 分析")

    parser.add_argument("--repo-dir", required=True, help="vllm-ascend 仓库路径")
    parser.add_argument("--good", required=True, help="已知正常的 commit/tag")
    parser.add_argument("--bad", required=True, help="已知异常的 commit/tag")

    test_group = parser.add_mutually_exclusive_group(required=True)
    test_group.add_argument("--test-script", help="开发提供的测试脚本文件")
    test_group.add_argument("--cmd", help="内联测试命令")

    setup_group = parser.add_mutually_exclusive_group()
    setup_group.add_argument("--setup-script", help="每个 commit 的安装脚本")
    setup_group.add_argument("--setup-cmd", help="每个 commit 的安装命令")

    parser.add_argument("--timeout", type=int, default=600, help="每轮超时秒数")
    parser.add_argument("--github-repo", default="vllm-project/vllm-ascend")
    parser.add_argument("--output-dir", default=".", help="输出目录")
    parser.add_argument("--analyze", action="store_true", help="二分完成后调用 Claude 分析根因")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="分析用的 Claude 模型")
    parser.add_argument("--error-description", default="", help="错误描述 (帮助 Agent 分析)")
    parser.add_argument("--skip-verify", action="store_true", help="跳过 good/bad 验证")

    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    os.makedirs(args.output_dir, exist_ok=True)
    log_dir = os.path.join(os.path.abspath(args.output_dir), "bisect_logs")

    # 解析 test/setup 命令
    if args.test_script:
        test_cmd = os.path.abspath(args.test_script)
        os.chmod(test_cmd, 0o755)
    else:
        test_cmd = args.cmd

    setup_cmd = None
    if args.setup_script:
        setup_cmd = os.path.abspath(args.setup_script)
        os.chmod(setup_cmd, 0o755)
    elif args.setup_cmd:
        setup_cmd = args.setup_cmd

    # fetch
    try:
        run_git(repo_dir, "fetch", "--all", "--prune")
    except RuntimeError as e:
        log.warning("git fetch failed: %s", e)

    # 验证
    if not args.skip_verify:
        log.info("验证 good commit: %s", args.good)
        r, out = run_test_at_commit(
            repo_dir, resolve_ref(repo_dir, args.good),
            test_cmd, setup_cmd, args.timeout,
        )
        if r != "pass":
            log.error("Good commit 未通过测试 (result=%s)", r)
            sys.exit(1)

        log.info("验证 bad commit: %s", args.bad)
        r, out = run_test_at_commit(
            repo_dir, resolve_ref(repo_dir, args.bad),
            test_cmd, setup_cmd, args.timeout,
        )
        if r == "pass":
            log.error("Bad commit 测试通过了, 不需要二分")
            sys.exit(1)

    # ── 二分定位 ──
    result = bisect(
        repo_dir=repo_dir, good=args.good, bad=args.bad,
        test_cmd=test_cmd, setup_cmd=setup_cmd,
        timeout=args.timeout, log_dir=log_dir,
    )

    bisect_file = os.path.join(args.output_dir, "bisect_result.json")
    bisect_data = {
        "status": result.status,
        "bad_commit": asdict(result.bad_commit) if result.bad_commit else None,
        "total_steps": result.total_steps,
        "total_commits": result.total_commits,
        "elapsed_seconds": result.elapsed_seconds,
        "history": result.history,
        "github_repo": args.github_repo,
    }
    with open(bisect_file, "w") as f:
        json.dump(bisect_data, f, indent=2, ensure_ascii=False)

    if result.status != "found" or not result.bad_commit:
        log.error("未找到问题 commit (status=%s)", result.status)
        sys.exit(1)

    pr_number = result.bad_commit.pr_number

    # ── Agent 分析 ──
    if args.analyze and pr_number:
        log.info("启动 Agent 根因分析 ...")
        try:
            pr_info = fetch_pr_info(args.github_repo, pr_number)

            test_log = ""
            for h in reversed(result.history):
                if h["result"] == "fail":
                    test_log = h.get("output_tail", "")
                    break

            analysis = analyze_with_claude(
                pr_info=pr_info, test_log=test_log,
                error_description=args.error_description, model=args.model,
            )
            report = generate_report(
                pr_info=pr_info, analysis=analysis,
                bisect_data=bisect_data, error_description=args.error_description,
            )

            report_file = os.path.join(args.output_dir, "report.md")
            with open(report_file, "w") as f:
                f.write(report)
            log.info("分析报告: %s", report_file)
        except Exception as e:
            log.error("Agent 分析失败: %s", e)


if __name__ == "__main__":
    main()
