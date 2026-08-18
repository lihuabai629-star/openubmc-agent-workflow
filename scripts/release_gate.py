#!/usr/bin/env python3
"""Run the mandatory install, lifecycle, and replay gates for a release."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
RELEASE_GATE_SCHEMA = "openubmc-agent-workflow.release-gate.v1"


def _tail(value: str, *, limit: int = 4000) -> str:
    text = value.strip()
    return text[-limit:] if len(text) > limit else text


def run_process(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _install_command(ref: str, home: Path) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "bootstrap.py"),
        "--ref",
        ref,
        "--home",
        str(home),
        "--clients",
        "codex",
        "--skill-profile",
        "target-runtime",
        "--skip-credentials",
        "--skip-tool-install",
        "--non-interactive",
    ]


def _installed_installer(home: Path) -> Path:
    return (
        home
        / ".local"
        / "share"
        / "openubmc"
        / "skills"
        / "openubmc-environment-setup"
        / "scripts"
        / "install_environment.py"
    )


def gate_commands(
    *,
    current_ref: str,
    previous_ref: str,
    clean_home: Path,
    lifecycle_home: Path,
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    clean_install = tuple(_install_command(current_ref, clean_home))
    previous_install = tuple(_install_command(previous_ref, lifecycle_home))
    current_upgrade = tuple(_install_command(current_ref, lifecycle_home))
    installer = str(_installed_installer(lifecycle_home))
    return (
        ("clean_install", (clean_install,)),
        ("upgrade", (previous_install, current_upgrade)),
        (
            "rollback",
            (
                (
                    sys.executable,
                    installer,
                    "rollback",
                    "--home",
                    str(lifecycle_home),
                    "--non-interactive",
                ),
            ),
        ),
        (
            "replay_smoke",
            (
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "openubmc-target-runtime/tests",
                    "-p",
                    "test_case_replay.py",
                ),
            ),
        ),
    )


def execute_release_gate(
    *,
    current_ref: str,
    previous_ref: str,
    workspace: Path,
    work_root: Path,
    executor: Callable[..., subprocess.CompletedProcess[str]] = run_process,
) -> dict[str, object]:
    clean_home = work_root / "clean-install-home"
    lifecycle_home = work_root / "lifecycle-home"
    results: list[dict[str, object]] = []
    blocked = False
    for name, commands in gate_commands(
        current_ref=current_ref,
        previous_ref=previous_ref,
        clean_home=clean_home,
        lifecycle_home=lifecycle_home,
    ):
        if blocked:
            results.append({"name": name, "status": "skipped", "commands": []})
            continue
        command_results: list[dict[str, object]] = []
        started = time.monotonic()
        for command in commands:
            completed = executor(command, cwd=workspace)
            command_results.append(
                {
                    "argv": list(command),
                    "returncode": completed.returncode,
                    "stdout_tail": _tail(completed.stdout or ""),
                    "stderr_tail": _tail(completed.stderr or ""),
                }
            )
            if completed.returncode:
                blocked = True
                break
        results.append(
            {
                "name": name,
                "status": "passed" if not blocked else "failed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "commands": command_results,
            }
        )
    promotable = all(item["status"] == "passed" for item in results)
    return {
        "schema": RELEASE_GATE_SCHEMA,
        "current_ref": current_ref,
        "previous_ref": previous_ref,
        "promotable": promotable,
        "gates": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-ref", required=True)
    parser.add_argument("--previous-ref", required=True)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.current_ref == args.previous_ref:
        parser.error("--current-ref and --previous-ref must differ")
    if args.work_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="openubmc-release-gate-")
        work_root = Path(temporary.name)
    else:
        temporary = None
        work_root = args.work_root.expanduser().absolute()
        work_root.mkdir(parents=True, exist_ok=True)
    try:
        report = execute_release_gate(
            current_ref=args.current_ref,
            previous_ref=args.previous_ref,
            workspace=args.workspace.expanduser().absolute(),
            work_root=work_root,
        )
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            output = args.output.expanduser().absolute()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0 if report["promotable"] else 1
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
