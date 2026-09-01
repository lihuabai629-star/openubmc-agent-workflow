#!/usr/bin/env python3
"""Start the installed Runtime MCP from a pinned real Codex process."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import time
from typing import Callable

from scripts.formal_identity import (
    PINNED_CODEX_VERSION,
    normalize_codex_identity,
    normalize_model_identity,
)

CODEX_VERSION = PINNED_CODEX_VERSION


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _process_identity(process_id: int) -> str:
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "unknown"
    command_end = stat.rfind(")")
    fields = stat[command_end + 2 :].split() if command_end >= 0 else []
    return fields[19] if len(fields) > 19 else "unknown"


def pinned_codex_executable(repository_root: Path) -> Path:
    configured = os.environ.get("OPENUBMC_CODEX_EXECUTABLE", "").strip()
    if configured:
        executable = Path(configured).expanduser().absolute()
    else:
        machine = platform.machine().lower()
        package_by_platform = {
            ("linux", "x86_64"): (
                "codex-linux-x64",
                "x86_64-unknown-linux-musl",
                "codex",
            ),
            ("linux", "amd64"): (
                "codex-linux-x64",
                "x86_64-unknown-linux-musl",
                "codex",
            ),
            ("linux", "aarch64"): (
                "codex-linux-arm64",
                "aarch64-unknown-linux-musl",
                "codex",
            ),
            ("linux", "arm64"): (
                "codex-linux-arm64",
                "aarch64-unknown-linux-musl",
                "codex",
            ),
        }
        selected = package_by_platform.get((platform.system().lower(), machine))
        if selected is None:
            raise ValueError("pinned Codex process probe is unsupported on this platform")
        package, target, binary = selected
        executable = (
            repository_root
            / "openubmc-kb-mcp"
            / "node_modules"
            / "@openai"
            / package
            / "vendor"
            / target
            / "bin"
            / binary
        ).absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(
            "pinned Codex executable is unavailable; run npm ci in openubmc-kb-mcp"
        )
    completed = subprocess.run(
        [str(executable), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or version != CODEX_VERSION:
        raise ValueError(
            f"pinned Codex version mismatch: expected {CODEX_VERSION}, got {version}"
        )
    return executable


class _ResponsesServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        super().__init__(("127.0.0.1", 0), _ResponsesHandler)


class _ResponsesHandler(http.server.BaseHTTPRequestHandler):
    server: _ResponsesServer

    def do_POST(self) -> None:  # noqa: N802
        try:
            content_length = int(self.headers.get("content-length", "0"))
            document = json.loads(self.rfile.read(content_length))
        except (TypeError, ValueError, json.JSONDecodeError):
            document = {}
        if isinstance(document, dict):
            self.server.requests.append(document)
        body = (
            'event: response.created\n'
            'data: {"type":"response.created","response":{"id":"resp-openubmc-probe"}}\n\n'
            'event: response.completed\n'
            'data: {"type":"response.completed","response":{"id":"resp-openubmc-probe",'
            '"usage":{"input_tokens":0,"input_tokens_details":null,"output_tokens":0,'
            '"output_tokens_details":null,"total_tokens":0}}}\n\n'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _request_tools(request: Mapping[str, object]) -> list[object]:
    tools: list[object] = []
    top_level = request.get("tools")
    if isinstance(top_level, list):
        tools.extend(top_level)
    input_items = request.get("input")
    if isinstance(input_items, list):
        for item in input_items:
            if not isinstance(item, Mapping) or item.get("type") != "additional_tools":
                continue
            additional = item.get("tools")
            if isinstance(additional, list):
                tools.extend(additional)
    return tools


def _tool_names(requests: list[dict[str, object]]) -> list[str]:
    names: set[str] = set()
    for request in requests:
        for tool in _request_tools(request):
            if isinstance(tool, Mapping) and isinstance(tool.get("name"), str):
                names.add(str(tool["name"]))
    return sorted(names)


def _namespace_tool_contracts(
    requests: list[dict[str, object]],
    *,
    selected: Callable[[Mapping[str, object]], bool],
) -> list[dict[str, object]]:
    contracts: list[dict[str, object]] = []
    for request in requests:
        for tool in _request_tools(request):
            if isinstance(tool, Mapping) and selected(tool):
                nested_tools = tool.get("tools")
                normalized = {
                    "name": str(tool.get("name", "")),
                    "type": str(tool.get("type", "")),
                    "tools": sorted(
                        (
                            {"name": str(item.get("name", ""))}
                            for item in nested_tools
                            if isinstance(item, Mapping)
                            and str(item.get("name", ""))
                        ),
                        key=lambda item: item["name"],
                    )
                    if isinstance(nested_tools, list)
                    else [],
                }
                if normalized not in contracts:
                    contracts.append(normalized)
    return contracts


def _runtime_tool_contracts(
    requests: list[dict[str, object]],
) -> list[dict[str, object]]:
    return _namespace_tool_contracts(
        requests,
        selected=lambda tool: "openubmc_target_runtime"
        in str(tool.get("name", "")),
    )


def _orchestrator_tool_contracts(
    requests: list[dict[str, object]],
) -> list[dict[str, object]]:
    return _namespace_tool_contracts(
        requests,
        selected=lambda tool: tool.get("name") == "functions"
        and tool.get("type") == "namespace",
    )


def _tool_route_evidence(
    requests: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "captured_model_tools": _tool_names(requests),
        "captured_runtime_tool_contracts": _runtime_tool_contracts(requests),
        "captured_orchestrator_tool_contracts": _orchestrator_tool_contracts(
            requests
        ),
    }


def _lifecycle_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and document.get("component") == "target-runtime":
            records.append(document)
    return records


def probe_codex_runtime(
    *,
    repository_root: Path,
    home: Path,
    qualification_root: Path,
    lifecycle_root: Path,
    runtime_state_root: Path,
    model_identity: Mapping[str, object],
    codex_identity: Mapping[str, object],
    source_commit: str,
    task_id: str,
    session_id: str,
) -> dict[str, object]:
    selected_codex_identity = normalize_codex_identity(codex_identity)
    selected_model_identity = normalize_model_identity(model_identity)
    selected_model = str(selected_model_identity["model"])
    executable = pinned_codex_executable(repository_root)
    server = _ResponsesServer()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    process_runs: list[dict[str, object]] = []
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        environment = {
            **os.environ,
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "OPENUBMC_CODEX_PROBE_API_KEY": "local-hermetic-probe",
            "OPENUBMC_MCP_CLIENT": "codex",
            "OPENUBMC_MCP_TASK_ID": task_id,
            "OPENUBMC_MCP_SESSION_ID": session_id,
            "OPENUBMC_MCP_MODEL_IDENTITY": json.dumps(
                selected_model_identity, ensure_ascii=True, sort_keys=True
            ),
            "OPENUBMC_MCP_CODEX_IDENTITY": json.dumps(
                selected_codex_identity, ensure_ascii=True, sort_keys=True
            ),
            "OPENUBMC_MCP_SOURCE_COMMIT": source_commit,
            "OPENUBMC_MCP_FORMAL_RUN": "1",
            "OPENUBMC_MCP_LIFECYCLE_DIR": str(lifecycle_root),
            "OPENUBMC_TARGET_RUNTIME_STATE_DIR": str(runtime_state_root),
        }
        command = [
            str(executable),
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-rules",
            "--color",
            "never",
            "--model",
            selected_model,
            "-C",
            str(qualification_root),
            "-c",
            'model_provider="openubmc_probe"',
            "-c",
            'model_providers.openubmc_probe.name="openUBMC hermetic probe"',
            "-c",
            f'model_providers.openubmc_probe.base_url="{base_url}"',
            "-c",
            'model_providers.openubmc_probe.env_key="OPENUBMC_CODEX_PROBE_API_KEY"',
            "-c",
            'model_providers.openubmc_probe.wire_api="responses"',
            "-c",
            "model_providers.openubmc_probe.supports_websockets=false",
            "-c",
            "mcp_servers.openubmc-kb.enabled=false",
        ]
        runtime_environment = {
            "OPENUBMC_MCP_CLIENT": "codex",
            "OPENUBMC_MCP_TASK_ID": task_id,
            "OPENUBMC_MCP_SESSION_ID": session_id,
            "OPENUBMC_MCP_MODEL_IDENTITY": json.dumps(
                selected_model_identity, ensure_ascii=True, sort_keys=True
            ),
            "OPENUBMC_MCP_CODEX_IDENTITY": json.dumps(
                selected_codex_identity, ensure_ascii=True, sort_keys=True
            ),
            "OPENUBMC_MCP_SOURCE_COMMIT": source_commit,
            "OPENUBMC_MCP_FORMAL_RUN": "1",
            "OPENUBMC_MCP_LIFECYCLE_DIR": str(lifecycle_root),
            "OPENUBMC_TARGET_RUNTIME_STATE_DIR": str(runtime_state_root),
        }
        for key, value in runtime_environment.items():
            command.extend(
                (
                    "-c",
                    "mcp_servers.openubmc-target-runtime.env."
                    + key
                    + "="
                    + json.dumps(value),
                )
            )
        command.append("Return exactly OK without calling a tool.")
        for _ in range(2):
            before = len(_lifecycle_records(lifecycle_root))
            request_start = len(server.requests)
            process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            identity = _process_identity(process.pid)
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired as error:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=5)
                raise ValueError(
                    "real Codex process probe timed out: "
                    + (stderr or stdout).strip()[-2000:]
                ) from error
            if process.returncode != 0:
                raise ValueError(
                    "real Codex process probe failed: "
                    + (stderr or stdout).strip()[-2000:]
                )
            deadline = time.monotonic() + 5
            records = _lifecycle_records(lifecycle_root)
            while len(records) <= before and time.monotonic() < deadline:
                time.sleep(0.05)
                records = _lifecycle_records(lifecycle_root)
            matched = [
                record
                for record in records[before:]
                if record.get("parent_pid") == process.pid
                and record.get("parent_identity") == identity
            ]
            if len(matched) != 1:
                config_path = home / ".codex" / "config.toml"
                try:
                    config_text = config_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    config_text = "<unavailable>"
                bindings = [
                    {
                        "parent_pid": record.get("parent_pid"),
                        "parent_identity": record.get("parent_identity"),
                        "exit_reason": record.get("exit_reason"),
                    }
                    for record in records[before:]
                ]
                raise ValueError(
                    "real Codex process did not own exactly one Runtime MCP: "
                    f"process={process.pid}/{identity}, records={bindings}, "
                    f"tools={_tool_names(server.requests)}, stderr={stderr.strip()[-1000:]}, "
                    f"config={config_text[-2000:]}, "
                    f"mcp_tools={[tool for request in server.requests for tool in request.get('tools', []) if isinstance(tool, Mapping) and 'openubmc_target_runtime' in str(tool.get('name', ''))]}"
                )
            process_requests = server.requests[request_start:]
            process_runs.append(
                {
                    "process_id": process.pid,
                    "process_identity": identity,
                    "parent_pid": matched[0].get("parent_pid"),
                    "parent_identity": matched[0].get("parent_identity"),
                    "executable": str(executable),
                    "executable_sha256": "sha256:" + _sha256(executable),
                    "version": CODEX_VERSION,
                    "requested_model": selected_model,
                    "captured_request_models": [
                        str(request.get("model", ""))
                        for request in process_requests
                        if isinstance(request.get("model"), str)
                    ],
                    **_tool_route_evidence(process_requests),
                    "transport_provenance": {
                        "provider": "local-hermetic-responses",
                        "wire_api": "responses",
                        "network_scope": "loopback",
                    },
                    "returncode": process.returncode,
                }
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    return {
        "codex_process_invocation": len(process_runs) == 2,
        "codex_process_runs": process_runs,
        **_tool_route_evidence(server.requests),
        "restart_verified": len(process_runs) == 2,
        "mcp_lifecycle_records": _lifecycle_records(lifecycle_root),
    }
