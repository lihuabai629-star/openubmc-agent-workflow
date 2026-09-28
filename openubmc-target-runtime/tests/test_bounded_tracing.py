"""Offline acceptance for optional Run tracing; no target or collector socket."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid
import unittest
from unittest import mock


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RUNTIME_ROOT.parent
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.context_runtime import (  # noqa: E402
    ContextRuntime, InMemoryRuntimeRepository,
)
from openubmc_target_runtime.mcp import JsonRpcMcpEndpoint, RuntimeMcpService  # noqa: E402
from openubmc_target_runtime.tracing import RunTracer, TraceSettings  # noqa: E402
from test_agent_gateway import (  # noqa: E402
    SemanticBackend, accepted_diagnosis_payload, gate_binding,
)

try:
    from opentelemetry.sdk.trace.export import SpanExportResult
except ImportError:
    SpanExportResult = None


SECRET = "trace-secret-marker-7193"
ADDRESS = "192.0.2.20"
FIXED_TIME = 1_800_000_000.0


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class SecretBackend(SemanticBackend):
    def debug_run(self, task, arguments, context):
        result = super().debug_run(task, arguments, context)
        result["root_cause"] = SECRET
        result["raw_output"] = SECRET
        return result


class FailingExporter:
    def __init__(self) -> None:
        self.seen = []

    def export(self, spans):
        self.seen.extend(spans)
        raise RuntimeError(SECRET)

    def shutdown(self) -> None:
        pass


class BlockingExporter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen = []

    def export(self, spans):
        self.started.set()
        self.release.wait(timeout=3)
        self.seen.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def run_flow(tracer: RunTracer, *, flush: bool = True):
    """Return exact MCP bytes, exact persisted event bytes, and collector data."""
    ids = itertools.count(1)
    fixed_clock = lambda: FIXED_TIME
    defaults = {**ContextRuntime.__init__.__kwdefaults__, "clock": fixed_clock}
    with (
        mock.patch("time.time", return_value=FIXED_TIME),
        mock.patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(ids))),
        mock.patch.object(ContextRuntime.__init__, "__kwdefaults__", defaults),
    ):
        backend = SecretBackend()
        repository = InMemoryRuntimeRepository(clock=fixed_clock)
        service = RuntimeMcpService(
            backend, context_repository=repository, tracing=tracer,
        )
        endpoint = JsonRpcMcpEndpoint(service, session_task_id=SECRET + "-task")
        replies = []

        def call(name: str, arguments: dict, request_id: int, operation_id: str):
            reply = endpoint.handle({
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments,
                           "_meta": {"openubmc/operationId": operation_id}},
            })
            assert reply is not None and not reply["result"]["isError"], reply
            replies.append(reply)
            return reply["result"]["structuredContent"]

        try:
            call("observe", {
                "target": ADDRESS,
                "selectors": [{"id": "caps", "kind": "capability", "names": ["ssh"]}],
            }, 1, "observe-1")
            first = call("execute", {
                "kind": "start", "target": ADDRESS, "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only", "purpose": SECRET,
            }, 2, "execute-1")
            evidence_ids = [item["evidence_id"] for item in first["diagnostic_receipt"]["evidence"]]
            second = call("execute", {
                "kind": "respond", "run_id": first["run_id"], **gate_binding(first),
                "response": {
                    "status": "completed", "summary": "fixture diagnosis verified",
                    "payload": accepted_diagnosis_payload(evidence_ids),
                },
            }, 3, "diagnosis-accept")
            last = call("execute", {
                "kind": "respond", "run_id": second["run_id"], **gate_binding(second),
                "response": {
                    "status": "completed", "summary": "source repair completed",
                    "payload": {
                        "source_revision": "abc123",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run regression tests"],
                    },
                },
            }, 4, "development-accept")
            events = repository.events(first["run_id"])
            if flush:
                tracer.flush()
            return canonical(replies), canonical(events), last, tracer.local_spans(), backend.calls
        finally:
            service.close()


class BoundedTracingTests(unittest.TestCase):
    def test_default_and_explicit_disable_need_no_sdk_or_endpoint(self) -> None:
        with mock.patch.dict("os.environ", {
            "OPENUBMC_TRACE_ENABLED": "0",
            "OPENUBMC_TRACE_OTLP_ENDPOINT": "https://collector.example/v1/traces",
        }):
            tracer = RunTracer()
        self.assertFalse(tracer.enabled)
        self.assertEqual(tracer.stats()["dropped_spans"], 0)
        with tracer.span("runtime.execute", task_id=SECRET, run_id=SECRET):
            pass
        self.assertEqual(tracer.local_spans(), ())

    def test_installed_skill_carries_adapter_and_explicit_disable(self) -> None:
        packager = RUNTIME_ROOT / "tools" / "package_runtime_skill.py"
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "installed-skill"
            subprocess.run([
                sys.executable, str(packager), "--source",
                str(REPO_ROOT / "openubmc-live-patch"), "--output", str(output),
            ], check=True, capture_output=True, text=True)
            vendor = output / "scripts/_vendor/openubmc_target_runtime"
            self.assertTrue((vendor / "tracing.py").is_file())
            probe = (
                "import sys;"
                f"sys.path.insert(0,{str(vendor.parent)!r});"
                "from openubmc_target_runtime.tracing import RunTracer;"
                "assert not RunTracer().enabled"
            )
            with mock.patch.dict("os.environ", {"OPENUBMC_TRACE_ENABLED": "0"}):
                subprocess.run(
                    [sys.executable, "-I", "-c", probe],
                    check=True, capture_output=True, text=True,
                )

    @unittest.skipUnless(SpanExportResult is not None, "install tracing extra")
    def test_offline_correlation_is_bounded_and_contains_no_input(self) -> None:
        tracer = RunTracer(TraceSettings(enabled=True))
        self.assertTrue(tracer.enabled)
        _replies, _events, last, spans, calls = run_flow(tracer)
        self.assertEqual(last["state"], "completed")
        self.assertEqual([name for name, _arguments in calls].count("debug_run"), 1)
        self.assertEqual(tracer.stats()["dropped_spans"], 0)
        names = {span.name for span in spans}
        self.assertTrue({
            "mcp.observe", "agent.observe", "runtime.observe", "mcp.execute",
            "agent.execute", "runtime.execute", "runtime.gate", "runtime.effect",
        } <= names)
        self.assertEqual(len({span.context.trace_id for span in spans}), 1)
        execute_ids = {span.context.span_id for span in spans if span.name == "runtime.execute"}
        for span in spans:
            if span.name in {"runtime.gate", "runtime.effect"}:
                self.assertIn(span.parent.span_id, execute_ids)
            self.assertTrue(set(span.attributes) <= {
                "task.ref", "run.ref", "effect.ref", "trace.dropped_before",
            })
            for value in span.attributes.values():
                self.assertLessEqual(len(str(value).encode()), 64)
        run_refs = {span.attributes["run.ref"] for span in spans if span.name in {
            "runtime.gate", "runtime.effect",
        }}
        self.assertEqual(len(run_refs), 1)
        exported = canonical([span.to_json() for span in spans])
        self.assertNotIn(SECRET.encode(), exported)
        self.assertNotIn(ADDRESS.encode(), exported)
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import encode_spans

        otlp_payload = encode_spans(spans).SerializePartialToString()
        self.assertNotIn(SECRET.encode(), otlp_payload)
        self.assertNotIn(ADDRESS.encode(), otlp_payload)

    @unittest.skipUnless(SpanExportResult is not None, "install tracing extra")
    def test_authoritative_bytes_match_when_enabled_failed_or_queue_full(self) -> None:
        baseline = RunTracer(TraceSettings())
        (expected_replies, expected_events, expected_last,
         _spans, expected_calls) = run_flow(baseline)
        self.assertEqual(expected_last["state"], "completed")

        for scenario in ("normal", "failure", "queue_full"):
            with self.subTest(scenario=scenario):
                exporter = (FailingExporter() if scenario == "failure"
                            else BlockingExporter() if scenario == "queue_full"
                            else None)
                tracer = RunTracer(TraceSettings(
                    enabled=True,
                    queue_size=1 if scenario == "queue_full" else 128,
                ), exporter=exporter)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    replies, events, last, _spans, calls = run_flow(
                        tracer, flush=scenario != "queue_full",
                    )
                    if scenario == "queue_full":
                        self.assertTrue(exporter.started.wait(timeout=1))
                        exporter.release.set()
                        tracer.flush()
                self.assertEqual(replies, expected_replies)
                self.assertEqual(events, expected_events)
                self.assertEqual(last["outcome"], expected_last["outcome"])
                self.assertEqual(calls, expected_calls)
                self.assertNotIn(SECRET, stderr.getvalue())
                stats = tracer.stats()
                if scenario == "failure":
                    self.assertGreater(stats["dropped_export"], 0)
                    payload = canonical([s.to_json() for s in exporter.seen])
                    self.assertNotIn(SECRET.encode(), payload)
                elif scenario == "queue_full":
                    self.assertGreater(stats["dropped_queue"], 0)

    @unittest.skipUnless(SpanExportResult is not None, "install tracing extra")
    def test_per_run_span_budget_counts_drops(self) -> None:
        tracer = RunTracer(TraceSettings(enabled=True, max_spans_per_run=2))
        for _ in range(5):
            with tracer.span("runtime.gate", run_id="run-test"):
                pass
        tracer.flush()
        self.assertEqual(len(tracer.local_spans()), 2)
        self.assertEqual(tracer.stats()["dropped_budget"], 3)
        tracer.close()

        # StartRun learns its Run ID after the outer span begins. The export
        # boundary still applies the per-Run budget to that late binding.
        late = RunTracer(TraceSettings(enabled=True, max_spans_per_run=2))
        for _ in range(5):
            with late.span("mcp.execute", task_id="one-task") as span:
                span.bind_run("run-test")
        late.flush()
        self.assertEqual(len(late.local_spans()), 2)
        self.assertEqual(late.stats()["dropped_budget"], 3)
        late.close()


if __name__ == "__main__":
    unittest.main()
