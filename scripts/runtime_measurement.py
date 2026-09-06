#!/usr/bin/env python3
"""Measure legal Runtime MCP Turns without a model or target connection."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import platform
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class FixtureTask:
    def __init__(self, task_id):
        self.task_id = task_id


class FixtureBackend:
    """Only local fixture facts are available; no target transport is composed."""
    open_task = staticmethod(FixtureTask)
    close_task = staticmethod(lambda task: None)
    maintain_task = staticmethod(lambda task: 0)
    task_status = staticmethod(lambda task: {"task_id": task.task_id})

    def debug_run(self, task, arguments, context):
        context.raise_if_stopped()
        return {"ok": True, "schema": "openubmc-debug.v1", "task": task.task_id,
                "summary": "fixture component consumed outdated state",
                "root_cause": "fixture state was outdated",
                "observed_at": "2026-09-06T00:00:00Z", "freshness": {"status": "fresh"}}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def gate_response(turn, stage):
    gate = turn["gate"]
    if gate["name"] != stage:
        raise ValueError("expected Gate " + stage + ", got " + gate["name"])
    if stage == "diagnosis.acceptance":
        payload = {"root_cause": "fixture state was outdated",
                   "evidence_ids": [item["evidence_id"] for item in turn["diagnostic_receipt"]["evidence"]],
                   "causal_chain": ["fixture state was outdated", "component consumed that state"],
                   "code_owner": "fixture/component.lua", "contradictions": [], "remaining_gaps": [],
                   "verification_status": "verified"}
    else:
        payload = {"source_revision": "fixture-revision", "authored_files": ["fixture/component.lua"],
                   "verification_plan": ["test fixture state refresh"]}
    return {"kind": "respond", "run_id": turn["run_id"],
            **{key: gate[key] for key in ("gate_id", "gate_version", "schema_digest")},
            "response": {"status": "completed", "summary": "fixture phase complete", "payload": payload}}


def source_identity(source):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()
    # A dirty development result must never masquerade as immutable release evidence.
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
            "runtime_tree_sha256": hashlib.sha256(b"".join(
                str(path.relative_to(source)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest()
                for path in sorted((source/"openubmc-target-runtime/openubmc_target_runtime").glob("*.py")))).hexdigest()}


def aggregate(records):
    result = {}
    for stage in sorted({row["stage"] for row in records}):
        for cache in sorted({row["cache"] for row in records}):
            rows = [row for row in records if row["stage"] == stage and row["cache"] == cache and row["failure_class"] is None]
            metrics = {}
            for key in ("runtime_wall_seconds", "runtime_cpu_seconds", "serialization_seconds", "response_bytes",
                        "text_bytes", "structured_bytes", "storage_delta_bytes"):
                values = sorted(row[key] for row in rows)
                if values:
                    metrics[key] = {"median": statistics.median(values), "p95": values[math.ceil(.95*len(values))-1]}
            result[stage+":"+cache] = {"valid": len(rows), "metrics": metrics}
    return result


def run(source, repetitions, warm_repetitions):
    sys.path.insert(0, str(source/"openubmc-target-runtime"))
    from openubmc_target_runtime import FilesystemBlobRepository, JsonRpcMcpEndpoint, RuntimeMcpService, SQLiteRuntimeRepository
    batch = str(uuid.uuid4())
    records, constructions, examples = [], [], {}
    with tempfile.TemporaryDirectory(prefix="openubmc-measurement-") as temporary:
        base = Path(temporary)
        def create(name):
            started = time.perf_counter()
            repository = SQLiteRuntimeRepository(base/(name+".sqlite3"))
            blobs = FilesystemBlobRepository(base/(name+"-blobs"))
            service = RuntimeMcpService(FixtureBackend(), context_repository=repository, blob_repository=blobs)
            constructions.append({"name": name, "seconds": time.perf_counter()-started})
            return service, repository, blobs

        def flow(index, cache, resources):
            service, repository, blobs = resources
            endpoint = JsonRpcMcpEndpoint(service, session_task_id=f"{batch}-{index}")
            turn = {}
            for sequence, stage in enumerate(("mcp_tools_list", "start", "diagnosis.acceptance", "developer.change"), 1):
                row = {"run_id": f"{batch}-{index}", "index": index, "cache": cache, "stage": stage, "failure_class": None}
                try:
                    if stage == "mcp_tools_list":
                        request = {"jsonrpc": "2.0", "id": sequence, "method": "tools/list", "params": {}}
                    else:
                        arguments = ({"kind": "start", "target": "198.51.100.10", "intent": "diagnose-and-fix",
                                      "delivery_strategy": "source-only", "purpose": "validate fixture state refresh"}
                                     if stage == "start" else gate_response(turn, stage))
                        request = {"jsonrpc": "2.0", "id": sequence, "method": "tools/call",
                                   "params": {"name": "execute", "arguments": arguments}}
                    row["request_bytes"] = len(encoded(request))
                    row["storage_bytes_before"] = repository.size_bytes()+blobs.size_bytes()
                    started = time.perf_counter(); cpu = time.process_time()
                    response = endpoint.handle(request)
                    row["runtime_cpu_seconds"] = time.process_time()-cpu
                    row["runtime_wall_seconds"] = time.perf_counter()-started
                    started = time.perf_counter()
                    body = encoded(response)
                    row["serialization_seconds"] = time.perf_counter()-started
                    row["response_bytes"] = len(body)
                    result = response.get("result", {})
                    row["text_bytes"] = sum(len(item.get("text", "").encode()) for item in result.get("content", []))
                    structured = result.get("structuredContent", {})
                    row["structured_bytes"] = len(encoded(structured)) if structured else 0
                    row["field_bytes"] = {key: len(encoded(value)) for key, value in structured.items()}
                    row["storage_bytes_after"] = repository.size_bytes()+blobs.size_bytes()
                    row["storage_delta_bytes"] = row["storage_bytes_after"]-row["storage_bytes_before"]
                    if "error" in response or result.get("isError"):
                        raise ValueError("MCP request rejected: " + str(result.get("structuredContent", response)))
                    if stage == "mcp_tools_list":
                        if {item["name"] for item in result["tools"]} != {"observe", "execute"}:
                            raise ValueError("unexpected tools")
                    else:
                        turn = structured
                        row["event_count"] = len(repository.events(turn["run_id"]))
                        row["turn_state"] = turn["state"]
                        if stage == "developer.change" and (turn["state"] != "completed" or not turn.get("outcome_recorded")):
                            raise ValueError("source delivery did not form a terminal Outcome")
                    if index == 0:
                        examples[stage] = response
                except Exception as exc:
                    row.update(failure_class="runtime" if "runtime_wall_seconds" in row else "harness", error=str(exc))
                    records.append(row)
                    break
                records.append(row)

        resources = None
        try:
            for index in range(repetitions):
                if resources is not None:
                    resources[0].close()
                resources = create(f"cold-{index}")
                flow(index, "fresh-service", resources)
            # Reuse the final measured service, which already completed a Run.
            for index in range(repetitions, repetitions+warm_repetitions):
                flow(index, "reused-service", resources)
        finally:
            if resources is not None:
                resources[0].close()
    report = {"schema": "openubmc.runtime-measurement.v1", "batch_id": batch,
              "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "source": source_identity(source), "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "condition": {"network": "disabled", "model": None, "client": "public-jsonrpc-endpoint",
                            "python": platform.python_version(), "platform": platform.platform(),
                            "cache": "module-imports-warm; fresh versus reused service; OS cache uncontrolled",
                            "runtime_time_scope": "endpoint dispatch, Runtime, storage and MCP text projection; excludes final JSON encoding"},
              "requested_calls": 4*(repetitions+warm_repetitions), "attempted": len(records), "not_attempted": 4*(repetitions+warm_repetitions)-len(records),
              "valid": sum(row["failure_class"] is None for row in records),
              "failure_counts": dict(Counter(row["failure_class"] for row in records if row["failure_class"])),
              "records": records, "construction": constructions, "examples": examples, "summary": aggregate(records)}
    report["report_sha256"] = hashlib.sha256(encoded(report)).hexdigest()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--warm-repetitions", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1 or args.warm_repetitions < 0:
        parser.error("repetitions must be positive and warm-repetitions nonnegative")
    report = run(args.source.resolve(), args.repetitions, args.warm_repetitions)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False); stream.write("\n")
    else:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return int(report["valid"] != report["requested_calls"])


if __name__ == "__main__":
    raise SystemExit(main())
