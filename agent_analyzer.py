#!/usr/bin/env python3
"""
Agent-based automated analysis for vllm-ascend bisect results.

After bisect_pr.py identifies the problematic PR, this module:
1. Fetches the PR diff, description, and comments from GitHub
2. Collects test failure logs
3. Calls Claude API to perform root cause analysis
4. Generates a structured report

Usage:
    # Analyze from bisect result file
    python agent_analyzer.py \
        --bisect-result bisect_result.json \
        --test-log test_output.log \
        --output report.md

    # Analyze a specific PR directly
    python agent_analyzer.py \
        --github-repo vllm-project/vllm-ascend \
        --pr-number 1234 \
        --test-log test_output.log \
        --error-description "Inference produces NaN on Ascend 910B"
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class PRInfo:
    number: int
    title: str
    body: str
    author: str
    url: str
    diff: str
    changed_files: list[str]
    comments: list[str]


# ── GitHub helpers ───────────────────────────────────────────────────────────


def fetch_pr_info(github_repo: str, pr_number: int) -> PRInfo:
    """Fetch PR details using gh CLI."""
    log.info("Fetching PR #%d info from %s ...", pr_number, github_repo)

    # Get PR metadata
    log.info("[github] Fetching PR metadata ...")
    pr_json = subprocess.run(
        [
            "gh", "api",
            f"repos/{github_repo}/pulls/{pr_number}",
            "--jq", "{title: .title, body: .body, user: .user.login, html_url: .html_url}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if pr_json.returncode != 0:
        log.error("[github] Failed to fetch PR metadata: %s", pr_json.stderr)
        raise RuntimeError(f"Failed to fetch PR info: {pr_json.stderr}")

    pr_data = json.loads(pr_json.stdout)
    log.info("[github] PR title: %s, author: %s", pr_data.get("title", "?"), pr_data.get("user", "?"))

    # Get PR diff
    log.info("[github] Fetching PR diff ...")
    diff_result = subprocess.run(
        [
            "gh", "api",
            f"repos/{github_repo}/pulls/{pr_number}",
            "-H", "Accept: application/vnd.github.diff",
        ],
        capture_output=True, text=True, timeout=60,
    )
    diff = diff_result.stdout if diff_result.returncode == 0 else "[diff fetch failed]"
    log.info("[github] Diff size: %d bytes", len(diff))

    # Get changed files
    log.info("[github] Fetching changed files ...")
    files_result = subprocess.run(
        [
            "gh", "api",
            f"repos/{github_repo}/pulls/{pr_number}/files",
            "--jq", ".[].filename",
        ],
        capture_output=True, text=True, timeout=30,
    )
    changed_files = files_result.stdout.strip().splitlines() if files_result.returncode == 0 else []
    log.info("[github] Changed files: %d", len(changed_files))

    # Get review comments
    log.info("[github] Fetching review comments ...")
    comments_result = subprocess.run(
        [
            "gh", "api",
            f"repos/{github_repo}/pulls/{pr_number}/comments",
            "--jq", ".[].body",
        ],
        capture_output=True, text=True, timeout=30,
    )
    comments = comments_result.stdout.strip().splitlines() if comments_result.returncode == 0 else []
    log.info("[github] Review comments: %d", len(comments))

    return PRInfo(
        number=pr_number,
        title=pr_data.get("title", ""),
        body=pr_data.get("body", "") or "",
        author=pr_data.get("user", ""),
        url=pr_data.get("html_url", f"https://github.com/{github_repo}/pull/{pr_number}"),
        diff=diff,
        changed_files=changed_files,
        comments=comments,
    )


# ── Claude API analysis ─────────────────────────────────────────────────────


def analyze_with_claude(
    pr_info: PRInfo,
    test_log: str,
    error_description: str = "",
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """
    Call Claude API to analyze the root cause.
    Requires ANTHROPIC_API_KEY environment variable.
    """
    try:
        import anthropic
    except ImportError:
        log.error("anthropic package not installed. Run: pip install anthropic")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Truncate diff if too large
    diff_text = pr_info.diff
    if len(diff_text) > 50000:
        log.warning("[analyze] Diff too large (%d bytes), truncating to 50000", len(diff_text))
        diff_text = diff_text[:50000] + "\n\n... [diff truncated, too large] ..."

    # Truncate test log if too large
    if len(test_log) > 20000:
        log.warning("[analyze] Test log too large (%d bytes), truncating to last 20000", len(test_log))
        test_log = "... [log truncated] ...\n" + test_log[-20000:]

    log.info("[analyze] Constructing prompt: diff=%d bytes, test_log=%d bytes", len(diff_text), len(test_log))

    prompt = f"""You are an expert in vLLM (a high-throughput LLM serving engine) and Ascend NPU hardware.

A regression has been identified in the vllm-ascend project. Your task is to analyze the PR that introduced the regression and explain the root cause.

## PR Information

**PR #{pr_info.number}: {pr_info.title}**
- Author: {pr_info.author}
- URL: {pr_info.url}
- Changed files: {', '.join(pr_info.changed_files)}

### PR Description
{pr_info.body[:5000]}

### PR Diff
```diff
{diff_text}
```

## Test Failure Information

### Error Description
{error_description or "Not provided — infer from test log below."}

### Test Output / Error Log
```
{test_log}
```

## Analysis Required

Please provide a structured analysis:

1. **Root Cause**: What specific change in this PR caused the regression? Point to exact lines/functions.
2. **Impact Scope**: What scenarios/models/configurations are affected?
3. **Mechanism**: How does the change lead to the failure? Trace the execution path.
4. **Suggested Fix**: Concrete code changes to fix the regression while preserving the PR's intent.
5. **Regression Test**: Suggest a test case that would catch this regression.

Be specific — reference file names, function names, and line numbers from the diff.
"""

    log.info("[analyze] Calling Claude API (model: %s) ...", model)
    api_start = time.time()
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    api_elapsed = time.time() - api_start
    result_text = response.content[0].text
    log.info("[analyze] Claude API responded in %.1fs, output: %d chars", api_elapsed, len(result_text))
    log.info("[analyze] Token usage: input=%d, output=%d",
             response.usage.input_tokens, response.usage.output_tokens)

    return result_text


# ── Report generation ────────────────────────────────────────────────────────


def generate_report(
    pr_info: PRInfo,
    analysis: str,
    bisect_data: Optional[dict] = None,
    error_description: str = "",
) -> str:
    """Generate a Markdown report."""
    report = []
    report.append(f"# Regression Analysis Report")
    report.append("")
    report.append(f"**Generated**: {__import__('datetime').datetime.now().isoformat()}")
    report.append("")

    # Bisect summary
    if bisect_data:
        report.append("## Bisect Summary")
        report.append("")
        report.append(f"- **Total commits searched**: {bisect_data.get('total_commits', '?')}")
        report.append(f"- **Bisect steps**: {bisect_data.get('total_steps', '?')}")
        report.append(f"- **Time elapsed**: {bisect_data.get('elapsed_seconds', 0):.1f}s")
        report.append("")

    # PR info
    report.append("## Identified PR")
    report.append("")
    report.append(f"- **PR**: [#{pr_info.number} — {pr_info.title}]({pr_info.url})")
    report.append(f"- **Author**: {pr_info.author}")
    report.append(f"- **Changed files** ({len(pr_info.changed_files)}):")
    for f in pr_info.changed_files[:20]:
        report.append(f"  - `{f}`")
    if len(pr_info.changed_files) > 20:
        report.append(f"  - ... and {len(pr_info.changed_files) - 20} more")
    report.append("")

    # Error description
    if error_description:
        report.append("## Error Description")
        report.append("")
        report.append(error_description)
        report.append("")

    # AI Analysis
    report.append("## Root Cause Analysis")
    report.append("")
    report.append(analysis)
    report.append("")

    # Bisect history
    if bisect_data and bisect_data.get("history"):
        report.append("## Bisect History")
        report.append("")
        report.append("| Step | Commit | PR | Result |")
        report.append("|------|--------|----|--------|")
        for h in bisect_data["history"]:
            pr = f"#{h.get('pr_number', '?')}" if h.get("pr_number") else "—"
            report.append(
                f"| {h['step']} | `{h['sha'][:10]}` | {pr} | {'PASS' if h['result'] == 'pass' else 'FAIL'} |"
            )
        report.append("")

    return "\n".join(report)


# ── Batch mode: analyze multiple scenarios ───────────────────────────────────


def run_batch(config_file: str):
    """
    Run bisect + analysis for multiple scenarios defined in a JSON config.

    Config format:
    {
        "repo_dir": "/path/to/vllm-ascend",
        "github_repo": "vllm-project/vllm-ascend",
        "good": "v0.7.0",
        "bad": "main",
        "scenarios": [
            {
                "name": "llama_inference",
                "test_script": "./tests/test_llama.sh",
                "setup_script": "./setup.sh",
                "timeout": 600,
                "description": "LLaMA 7B inference on single NPU"
            },
            ...
        ]
    }
    """
    log.info("[batch] Loading config from: %s", config_file)
    with open(config_file) as f:
        config = json.load(f)

    from bisect_pr import bisect

    repo_dir = config["repo_dir"]
    github_repo = config.get("github_repo", "vllm-project/vllm-ascend")
    good = config["good"]
    bad = config["bad"]

    log.info("[batch] Repo: %s, good: %s, bad: %s", repo_dir, good, bad)
    log.info("[batch] Total scenarios: %d", len(config["scenarios"]))

    results = []

    for idx, scenario in enumerate(config["scenarios"], 1):
        name = scenario["name"]
        log.info("")
        log.info("=" * 60)
        log.info("[batch] Scenario %d/%d: %s", idx, len(config["scenarios"]), name)
        log.info("[batch] Description: %s", scenario.get("description", ""))
        log.info("=" * 60)

        test_cmd = scenario.get("cmd")
        if not test_cmd:
            test_cmd = os.path.abspath(scenario["test_script"])
        setup_cmd = scenario.get("setup_cmd")
        if not setup_cmd and scenario.get("setup_script"):
            setup_cmd = os.path.abspath(scenario["setup_script"])
        timeout = scenario.get("timeout", 600)

        # Run bisect
        result = bisect(
            repo_dir=repo_dir,
            good=good,
            bad=bad,
            test_cmd=test_cmd,
            setup_cmd=setup_cmd,
            timeout=timeout,
        )

        scenario_result = {
            "scenario": name,
            "description": scenario.get("description", ""),
            "bisect_status": result.status,
        }

        if result.status == "found" and result.bad_commit:
            pr_number = result.bad_commit.pr_number
            if pr_number:
                try:
                    pr_info = fetch_pr_info(github_repo, pr_number)

                    # Get test log from last failure
                    test_log = ""
                    for h in reversed(result.history):
                        if h["result"] == "fail":
                            test_log = h.get("output_tail", "")
                            break

                    analysis = analyze_with_claude(
                        pr_info=pr_info,
                        test_log=test_log,
                        error_description=scenario.get("description", ""),
                    )

                    report = generate_report(
                        pr_info=pr_info,
                        analysis=analysis,
                        bisect_data={
                            "total_commits": result.total_commits,
                            "total_steps": result.total_steps,
                            "elapsed_seconds": result.elapsed_seconds,
                            "history": result.history,
                        },
                        error_description=scenario.get("description", ""),
                    )

                    report_file = f"report_{name}.md"
                    with open(report_file, "w") as f:
                        f.write(report)
                    log.info("Report saved to %s", report_file)

                    scenario_result["pr_number"] = pr_number
                    scenario_result["pr_url"] = pr_info.url
                    scenario_result["report_file"] = report_file
                except Exception as e:
                    log.error("Analysis failed for scenario %s: %s", name, e)
                    scenario_result["analysis_error"] = str(e)

        results.append(scenario_result)

    # Save batch summary
    summary_file = "batch_summary.json"
    with open(summary_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log.info("Batch summary saved to %s", summary_file)

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Agent-based PR regression analyzer")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Single PR analysis
    analyze_parser = subparsers.add_parser("analyze", help="Analyze a single PR or bisect result")
    analyze_parser.add_argument("--bisect-result", help="Path to bisect_result.json")
    analyze_parser.add_argument("--github-repo", default="vllm-project/vllm-ascend")
    analyze_parser.add_argument("--pr-number", type=int, help="PR number to analyze directly")
    analyze_parser.add_argument("--test-log", help="Path to test failure log file")
    analyze_parser.add_argument("--error-description", default="", help="Description of the error")
    analyze_parser.add_argument("--model", default="claude-sonnet-4-20250514", help="Claude model to use")
    analyze_parser.add_argument("--output", default="report.md", help="Output report file")

    # Batch mode
    batch_parser = subparsers.add_parser("batch", help="Run bisect + analysis for multiple scenarios")
    batch_parser.add_argument("--config", required=True, help="Path to batch config JSON")

    args = parser.parse_args()

    if args.command == "analyze":
        log.info("[cli] Starting single PR analysis ...")

        # Determine PR number
        pr_number = args.pr_number
        bisect_data = None
        github_repo = args.github_repo

        if args.bisect_result:
            log.info("[cli] Loading bisect result from: %s", args.bisect_result)
            with open(args.bisect_result) as f:
                bisect_data = json.load(f)
            if not pr_number and bisect_data.get("bad_commit", {}).get("pr_number"):
                pr_number = bisect_data["bad_commit"]["pr_number"]
                log.info("[cli] PR number from bisect result: #%d", pr_number)
            if bisect_data.get("github_repo"):
                github_repo = bisect_data["github_repo"]

        if not pr_number:
            log.error("[cli] No PR number provided. Use --pr-number or --bisect-result")
            sys.exit(1)

        log.info("[cli] Analyzing PR #%d from %s", pr_number, github_repo)

        # Fetch PR info
        pr_info = fetch_pr_info(github_repo, pr_number)

        # Read test log
        test_log = ""
        if args.test_log:
            log.info("[cli] Reading test log from: %s", args.test_log)
            with open(args.test_log) as f:
                test_log = f.read()
            log.info("[cli] Test log size: %d bytes", len(test_log))
        elif bisect_data and bisect_data.get("history"):
            log.info("[cli] Extracting test log from bisect history ...")
            for h in reversed(bisect_data["history"]):
                if h["result"] == "fail":
                    test_log = h.get("output_tail", "")
                    break
            log.info("[cli] Test log extracted: %d bytes", len(test_log))

        # Analyze
        analysis = analyze_with_claude(
            pr_info=pr_info,
            test_log=test_log,
            error_description=args.error_description,
            model=args.model,
        )

        # Generate report
        report = generate_report(
            pr_info=pr_info,
            analysis=analysis,
            bisect_data=bisect_data,
            error_description=args.error_description,
        )

        with open(args.output, "w") as f:
            f.write(report)
        log.info("[cli] Report saved to %s (%d bytes)", args.output, len(report))
        print(f"\nReport generated: {args.output}")

    elif args.command == "batch":
        log.info("[cli] Starting batch mode ...")
        run_batch(args.config)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
