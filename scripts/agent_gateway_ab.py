#!/usr/bin/env python3
"""Run and evaluate paired AB/BA qualification for the semantic Agent Gateway."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from typing import Iterable, Mapping


SCHEMA = "openubmc-agent-workflow.agent-gateway-ab.v1"
DEFAULT_BASELINE_REF = "35b36efb6503d05a811b51bf09fb5f8dead0e208"
CHECKPOINTS = (10, 20, 30)
METRICS = (
    "total_tokens",
    "noncached_input_plus_output",
    "duration_seconds",
)


def _json_object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _read_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            events.append(value)
    return events


def _tool_output_bytes(item: Mapping[str, object]) -> int:
    if item.get("type") == "command_execution":
        return len(str(item.get("aggregated_output", "")).encode("utf-8"))
    return len(
        json.dumps(
            {"result": item.get("result"), "error": item.get("error")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def metric_from_run(
    *,
    arm: str,
    pair: int,
    order: int,
    events_path: Path,
    final_path: Path,
    exit_code: int,
    duration_seconds: float,
) -> dict[str, object]:
    events = _read_events(events_path)
    completed = [event for event in events if event.get("type") == "turn.completed"]
    usage = _json_object(completed[-1].get("usage")) if completed else {}
    tools = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = _json_object(event.get("item"))
        if item.get("type") in {"command_execution", "mcp_tool_call"}:
            tools.append(item)
    mcp_tools: dict[str, int] = {}
    for item in tools:
        if item.get("type") == "mcp_tool_call":
            name = str(item.get("tool", ""))
            mcp_tools[name] = mcp_tools.get(name, 0) + 1
    final = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached_tokens = int(usage.get("cached_input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    acceptance = semantic_acceptance(final)
    scope_ok = (
        arm != "B"
        or mcp_tools == {"observe": 1}
    )
    return {
        "arm": arm,
        "pair": pair,
        "order": order,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": int(
            usage.get("reasoning_output_tokens", 0) or 0
        ),
        "total_tokens": input_tokens + output_tokens,
        "noncached_input_plus_output": input_tokens - cached_tokens + output_tokens,
        "tool_events": len(tools),
        "command_events": sum(
            item.get("type") == "command_execution" for item in tools
        ),
        "mcp_events": sum(item.get("type") == "mcp_tool_call" for item in tools),
        "tool_output_bytes": sum(_tool_output_bytes(item) for item in tools),
        "mcp_tools": [
            {"tool": name, "count": count}
            for name, count in sorted(mcp_tools.items())
        ],
        "final_chars": len(final),
        "semantic_acceptance": acceptance,
        "scope_acceptance": scope_ok,
        "valid": (
            exit_code == 0
            and input_tokens + output_tokens > 0
            and acceptance["passed"]
            and scope_ok
        ),
    }


def semantic_acceptance(text: str) -> dict[str, object]:
    folded = text.lower().replace("`", "")
    required_groups = {
        "capabilities": ("ssh", "telnet", "mdbctl", "busctl"),
        "drive_fields": (
            "name",
            "protocol",
            "resourceid",
            "slotnumber",
            "presence",
            "temperaturecelsius",
            "type",
            "socketid",
            "health",
        ),
    }
    missing = [
        token
        for tokens in required_groups.values()
        for token in tokens
        if token not in folded
    ]
    conclusion = (
        "不能" in text or "无法" in text
    ) and "resourceid" in folded and "异常" in text
    return {
        "passed": not missing and conclusion,
        "missing": missing,
        "conclusion_supported": conclusion,
    }


def balanced_schedule(pairs: int, *, seed: int) -> list[tuple[int, str, str]]:
    if pairs < 1:
        raise ValueError("pairs must be positive")
    orders = [("A", "B")] * ((pairs + 1) // 2) + [("B", "A")] * (pairs // 2)
    random.Random(seed).shuffle(orders)
    return [(index, first, second) for index, (first, second) in enumerate(orders, 1)]


def _geometric_mean(values: Iterable[float]) -> float:
    samples = list(values)
    if not samples or any(value <= 0 for value in samples):
        raise ValueError("geometric mean requires positive samples")
    return math.exp(statistics.fmean(math.log(value) for value in samples))


def _percentile(values: Iterable[float], percentile: float) -> float:
    samples = sorted(values)
    if not samples:
        raise ValueError("percentile requires samples")
    index = max(0, min(len(samples) - 1, math.ceil(percentile * len(samples)) - 1))
    return samples[index]


def bootstrap_upper(
    ratios: list[float], *, seed: int = 20260819, iterations: int = 20_000
) -> float:
    if not ratios:
        raise ValueError("bootstrap requires paired ratios")
    generator = random.Random(seed)
    size = len(ratios)
    estimates = [
        _geometric_mean(ratios[generator.randrange(size)] for _ in range(size))
        for _ in range(iterations)
    ]
    return _percentile(estimates, 0.95)


def analyze(metrics: list[Mapping[str, object]]) -> dict[str, object]:
    paired: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    pair_ids = sorted({int(item.get("pair", 0)) for item in metrics})
    invalid: list[dict[str, object]] = []
    for pair_id in pair_ids:
        members = [item for item in metrics if int(item.get("pair", 0)) == pair_id]
        by_arm = {str(item.get("arm")): item for item in members}
        if set(by_arm) != {"A", "B"} or not all(
            bool(item.get("valid")) for item in by_arm.values()
        ):
            invalid.append(
                {
                    "pair": pair_id,
                    "arms": sorted(by_arm),
                    "valid": {
                        arm: bool(item.get("valid")) for arm, item in by_arm.items()
                    },
                }
            )
            continue
        paired.append((by_arm["A"], by_arm["B"]))
    summaries: dict[str, object] = {}
    all_pass = True
    for metric in METRICS:
        ratios = [float(candidate[metric]) / float(baseline[metric]) for baseline, candidate in paired]
        if ratios:
            point = _geometric_mean(ratios)
            upper = bootstrap_upper(ratios)
            metric_pass = point <= 1.10 and upper <= 1.15
            p95_ratio = (
                _percentile(
                    (float(candidate[metric]) for _baseline, candidate in paired),
                    0.95,
                )
                / _percentile(
                    (float(baseline[metric]) for baseline, _candidate in paired),
                    0.95,
                )
                if len(paired) >= 30
                else None
            )
            if p95_ratio is not None:
                metric_pass = metric_pass and p95_ratio <= 1.20
            summaries[metric] = {
                "paired_ratios": [round(value, 6) for value in ratios],
                "geometric_mean_ratio": round(point, 6),
                "one_sided_95_upper": round(upper, 6),
                "p95_ratio": round(p95_ratio, 6) if p95_ratio is not None else None,
                "passed": metric_pass,
            }
            all_pass = all_pass and metric_pass
        else:
            summaries[metric] = {"passed": False}
            all_pass = False
    valid_pairs = len(paired)
    if valid_pairs < CHECKPOINTS[0]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[0]
    elif all_pass:
        decision = "passed"
        next_pairs = None
    elif valid_pairs < CHECKPOINTS[1]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[1]
    elif valid_pairs < CHECKPOINTS[2]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[2]
    else:
        decision = "failed"
        next_pairs = None
    return {
        "schema": SCHEMA,
        "valid_pairs": valid_pairs,
        "invalid_pairs": invalid,
        "metrics": summaries,
        "decision": decision,
        "next_pair_target": next_pairs,
    }


def _run(command: list[str], *, cwd: Path, env: Mapping[str, str], stdin: str, stdout: Path, stderr: Path) -> int:
    with stdout.open("w", encoding="utf-8") as output, stderr.open("w", encoding="utf-8") as errors:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            input=stdin,
            text=True,
            stdout=output,
            stderr=errors,
            check=False,
        )
    return completed.returncode


def _prepare_worktree(repo: Path, destination: Path, ref: str) -> None:
    if destination.exists():
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=destination,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        expected = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        if current.returncode == 0 and current.stdout.strip() == expected:
            return
        raise RuntimeError(f"benchmark worktree already exists at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(destination), ref],
        cwd=repo,
        check=True,
    )


def _prompt(skill_path: Path) -> str:
    return "\n".join(
        (
            "这是一次真实 BMC 环境下的 openubmc-debug 配对性能基准。",
            f"完整读取并严格遵循 {skill_path}。",
            "请使用该版本 Skill 的原生默认路径，不得人为禁用或改用另一版本机制。不得使用其他 Skill、知识库、网络搜索、源码或日志。",
            "目标 BMC：10.121.136.200。凭据位于标准私有配置中，不得显示凭据值。",
            "执行只读现场核验：",
            "1. 获取 live 能力快照，确认 SSH、Telnet、MDBCTL、BUSCTL。",
            "2. 对固定对象 Drive_1_010102 执行以下九条精确 MDB getprop，不得枚举其他对象或属性：",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Name",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Protocol",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive ResourceId",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive SlotNumber",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Presence",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive TemperatureCelsius",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo Type",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo SocketId",
            "   - getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.DriveStatus Health",
            "3. 报告原始值，并判断这些证据能否单独证明 ResourceId=0 异常，不得臆测源码语义。",
            "禁止写操作、状态变更、宽查询、日志和源码读取。中文回答，不超过 500 字。",
        )
    ) + "\n"


@dataclass(frozen=True)
class RunConfig:
    arm: str
    source_root: Path
    interface_profile: str


def run_benchmark(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    work_root = args.work_root.resolve()
    baseline_root = work_root / "variants" / "baseline"
    _prepare_worktree(repo, baseline_root, args.baseline_ref)
    candidate_root = repo
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output.resolve() if args.output else work_root / f"results-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    schedule = balanced_schedule(args.pairs, seed=args.seed)
    (output / "schedule.json").write_text(
        json.dumps(schedule, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    configs = {
        "A": RunConfig("A", baseline_root, ""),
        "B": RunConfig("B", candidate_root, "agent"),
    }
    environment = os.environ.copy()
    environment["OPENUBMC_CREDENTIALS_FILE"] = str(args.credentials)
    environment["OPENUBMC_DEBUG_CREDENTIALS_FILE"] = str(args.credentials)
    metrics: list[dict[str, object]] = []
    for pair, first, second in schedule:
        ordered_arms = tuple(
            arm for arm in (first, second) if args.only_arm is None or arm == args.only_arm
        )
        for order, arm in enumerate(ordered_arms, 1):
            config = configs[arm]
            run_dir = output / f"pair-{pair:02d}-{order}-{arm}"
            home = run_dir / "home"
            run_dir.mkdir(parents=True)
            home.mkdir()
            prompt = _prompt(config.source_root / "openubmc-debug" / "SKILL.md")
            (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            final_path = run_dir / "final.md"
            events_path = run_dir / "events.jsonl"
            stderr_path = run_dir / "stderr.log"
            command = [
                args.codex,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--json",
                "--sandbox",
                "danger-full-access",
                "--skip-git-repo-check",
                "-C",
                str(args.codex_cwd),
                "-m",
                args.model,
                "-o",
                str(final_path),
            ]
            for value in args.codex_config:
                command.extend(("-c", value))
            command.extend(
                (
                    "-c",
                    'mcp_servers.openubmc-target-runtime.command="/usr/bin/python3"',
                    "-c",
                    (
                        "mcp_servers.openubmc-target-runtime.args=["
                        + json.dumps(str(config.source_root / "openubmc-debug" / "scripts" / "target_runtime_mcp.py"))
                        + "]"
                    ),
                    "-c",
                    'mcp_servers.openubmc-target-runtime.env_vars=["OPENUBMC_CREDENTIALS_FILE","OPENUBMC_DEBUG_CREDENTIALS_FILE","OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE"]',
                    "-c",
                    "mcp_servers.openubmc-target-runtime.tool_timeout_sec=900",
                )
            )
            run_env = dict(environment)
            run_env["HOME"] = str(home)
            run_env["CODEX_HOME"] = os.environ.get("CODEX_HOME", "/root/.codex")
            if config.interface_profile:
                run_env["OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE"] = config.interface_profile
            else:
                run_env.pop("OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE", None)
            started = time.monotonic()
            exit_code = _run(
                command,
                cwd=args.codex_cwd,
                env=run_env,
                stdin=prompt,
                stdout=events_path,
                stderr=stderr_path,
            )
            duration = time.monotonic() - started
            metric = metric_from_run(
                arm=arm,
                pair=pair,
                order=order,
                events_path=events_path,
                final_path=final_path,
                exit_code=exit_code,
                duration_seconds=duration,
            )
            metrics.append(metric)
            (run_dir / "metrics.json").write_text(
                json.dumps(metric, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (output / "all_metrics.json").write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(metric, ensure_ascii=False, sort_keys=True), flush=True)
            if args.pause_seconds:
                time.sleep(args.pause_seconds)
    summary = analyze(metrics)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False, indent=2))
    if args.only_arm is not None:
        return 0 if metrics and all(bool(item["valid"]) for item in metrics) else 1
    return 0 if summary["decision"] == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("metrics", type=Path)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--work-root", type=Path, required=True)
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    run_parser.add_argument("--pairs", type=int, default=10)
    run_parser.add_argument("--seed", type=int, default=20260819)
    run_parser.add_argument("--credentials", type=Path, required=True)
    run_parser.add_argument("--codex", default="codex")
    run_parser.add_argument("--codex-cwd", type=Path, default=Path("/home/workspace"))
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--codex-config", action="append", default=[])
    run_parser.add_argument("--pause-seconds", type=float, default=5)
    run_parser.add_argument("--only-arm", choices=("A", "B"))
    args = parser.parse_args(argv)
    if args.command == "analyze":
        value = json.loads(args.metrics.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            parser.error("metrics must contain an array")
        print(json.dumps(analyze(value), ensure_ascii=False, indent=2))
        return 0
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
