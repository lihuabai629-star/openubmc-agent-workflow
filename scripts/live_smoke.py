#!/usr/bin/env python3
"""Run bounded MDB preflight comparisons and optional read-only delivery checks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
MAX_PARALLEL_PROBES = 32


def run(command: list[str], *, environment: dict[str, str]) -> int:
    result = subprocess.run(command, env=environment, check=False)
    return result.returncode


def probe_target(
    target: str,
    *,
    environment: dict[str, str],
    timeout: int,
    deadline: int,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(ROOT / "openubmc-debug/scripts/preflight_remote.py"),
        "--ip",
        target,
        "--mdb-only",
        "--skip-telnet",
        "--ssh-timeout",
        str(timeout),
        "--json",
        "--compact-json",
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1, min(deadline, timeout + 5)),
        )
    except subprocess.TimeoutExpired:
        return {
            "target": target,
            "ok": False,
            "code": "probe_timeout",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "target": target,
            "ok": False,
            "code": "invalid_probe_output" if result.returncode == 0 else "probe_failed",
            "returncode": result.returncode,
            "detail": result.stderr.strip().splitlines()[-1][:300] if result.stderr.strip() else "",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    probe = document.get("result", {}) if isinstance(document, dict) else {}
    capabilities = probe.get("capabilities", {}) if isinstance(probe, dict) else {}
    checks = probe.get("checks", {}) if isinstance(probe, dict) else {}
    ssh = checks.get("SSH", {}) if isinstance(checks, dict) else {}
    return {
        "target": target,
        "ok": bool(document.get("ok")) and result.returncode == 0,
        "code": str(document.get("code", "unknown")),
        "observed_at": str(document.get("observed_at", "")),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "capabilities": {
            "ssh_transport": bool(capabilities.get("ssh_transport")),
            "mdbctl": bool(capabilities.get("mdbctl")),
            "remote_object": bool(capabilities.get("remote_object")),
        },
        "clock": (ssh.get("lines") or [""])[0] if isinstance(ssh, dict) else "",
        "failed_checks": list(probe.get("failed_checks", [])) if isinstance(probe, dict) else [],
    }


def compare_probes(probes: list[dict[str, object]]) -> dict[str, object]:
    capability_names = ("ssh_transport", "mdbctl", "remote_object")
    differing = [
        name
        for name in capability_names
        if len({bool(dict(probe.get("capabilities", {})).get(name)) for probe in probes}) > 1
    ]
    return {
        "schema": "openubmc-agent-workflow/live-smoke.v1",
        "read_only": True,
        "all_targets_ready": all(bool(probe.get("ok")) for probe in probes),
        "differing_capabilities": differing,
        "targets": probes,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--deadline", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--live-patch-local", type=Path)
    parser.add_argument("--live-patch-remote")
    parser.add_argument("--upgrade-artifact", type=Path)
    parser.add_argument("--product-version")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument(
        "--allow-insecure-tls",
        dest="allow_insecure_tls",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    tls.add_argument(
        "--strict-tls",
        dest="allow_insecure_tls",
        action="store_false",
        help="verify the target Redfish certificate with the system trust store",
    )
    args = parser.parse_args(argv)
    if len(args.target) < 2:
        parser.error("at least two --target values are required")
    if args.deadline <= 0 or args.timeout <= 0:
        parser.error("--deadline and --timeout must be positive")
    environment = dict(os.environ)
    credentials = args.credentials_file or Path.home() / ".config/openubmc/credentials.env"
    if credentials.is_file():
        environment["OPENUBMC_CREDENTIALS_FILE"] = str(credentials.resolve())
    with ThreadPoolExecutor(max_workers=min(len(args.target), MAX_PARALLEL_PROBES)) as executor:
        futures = [
            executor.submit(
                probe_target,
                target,
                environment=environment,
                timeout=args.timeout,
                deadline=args.deadline,
            )
            for target in args.target
        ]
        probes = [future.result() for future in futures]
    comparison = compare_probes(probes)
    print(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True))
    if not comparison["all_targets_ready"]:
        return 1
    status = 0
    if bool(args.live_patch_local) != bool(args.live_patch_remote):
        parser.error("--live-patch-local and --live-patch-remote must be used together")
    if args.live_patch_local and args.live_patch_remote:
        status = run(
            [
                sys.executable,
                str(ROOT / "openubmc-live-patch/scripts/deploy_live_file.py"),
                "--ip",
                args.target[0],
                "--local",
                str(args.live_patch_local.resolve()),
                "--remote",
                args.live_patch_remote,
                "--dry-run",
                "--json",
            ],
            environment=environment,
        )
        if status:
            return status
    if bool(args.upgrade_artifact) != bool(args.product_version):
        parser.error("--upgrade-artifact and --product-version must be used together")
    if args.upgrade_artifact:
        command = [
            sys.executable,
            str(ROOT / "openubmc-upgrade/scripts/preflight_upgrade.py"),
            "--target",
            args.target[0],
            "--artifact-path",
            str(args.upgrade_artifact.resolve()),
            "--artifact-sha256",
            sha256(args.upgrade_artifact),
            "--product-version",
            args.product_version,
            "--timeout",
            str(args.timeout),
        ]
        if credentials.is_file():
            command.extend(("--credentials-file", str(credentials.resolve())))
        if args.allow_insecure_tls:
            command.append("--allow-insecure-tls")
        status = run(command, environment=environment)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
