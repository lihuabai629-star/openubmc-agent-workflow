from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openubmc_target_runtime import FilesystemBlobRepository, RuntimeMcpService, SQLiteRuntimeRepository
from test_agent_gateway import SemanticBackend


class ScopedBackend(SemanticBackend):
    def __init__(self):
        super().__init__()
        self.epoch = 1
        self.identity = "boot-1"
        self.fingerprint = "ssh:fixture-target"
        self.tick = 0
        self.change_on_query = ""
        self.delay_on_query = ""

    def observe_query(self, task, arguments, context):
        queries = arguments.get("mdb_queries", [])
        if self.change_on_query and self.change_on_query in queries:
            self.epoch += 1
            self.change_on_query = ""
        if self.delay_on_query and self.delay_on_query in queries:
            self.tick += 20
            self.delay_on_query = ""
        self.tick += 1
        result = super().observe_query(task, arguments, context)
        anchor = (datetime(2026, 9, 5, tzinfo=timezone.utc) + timedelta(seconds=self.tick)).isoformat()
        result["observed_at"] = anchor
        timing = result["observation_timing"]
        timing["started_at"] = timing["completed_at"] = anchor
        for selector in timing["selectors"]:
            selector["started_at"] = selector["completed_at"] = anchor
        result["result"]["runtime"] = {"status": {"targets": [{
            "target": {"host": arguments["ip"], "fingerprint": self.fingerprint},
            "identity": {"schema": "target-identity", "target_clock": anchor, "boot_id": self.identity},
            "epochs": {"target_epoch": self.epoch},
        }]}}
        return result


def observe(service, *, task="continuity", operation="observe-1", target="192.0.2.90", queries=None, selector="facts"):
    return service.call_exposed_tool(
        "observe", {"target": target, "selectors": [{"id": selector, "kind": "mdb", "queries": queries or ["lsprop Object0"]}]},
        task_id=task, operation_id=operation,
    )


def start(service, *, task="continuity", operation="start-1", target="192.0.2.90", **extra):
    return service.call_exposed_tool(
        "execute", {"kind": "start", "target": target, "intent": "diagnosis-only", **extra},
        task_id=task, operation_id=operation,
    )


class ObservationContinuityTests(unittest.TestCase):
    def test_observe_binds_nondefault_ssh_port_to_scope_and_runtime(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.90",
                    "ssh_port": 2222,
                    "selectors": [
                        {"id": "ssh", "kind": "capability", "names": ["ssh"]},
                        {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]},
                    ],
                },
                task_id="nondefault-ssh-port",
                operation_id="observe-port-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["scope"]["ssh_port"], 2222)
        self.assertEqual(
            [arguments["ssh_port"] for name, arguments in backend.calls if name == "debug_collect"],
            [2222],
        )

    def test_start_discovers_ref_and_replay_keeps_original_caller_identity(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            observation = observe(service)
            waiting = start(service)
            self.assertEqual(waiting["observation_ref"], observation["observation_ref"])
            self.assertEqual(waiting["gate"]["name"], "diagnosis.acceptance")
            self.assertEqual([name for name, _ in backend.calls], ["debug_collect", "debug_collect"])
            self.assertEqual(backend.calls[-1][1]["mdb_queries"], [])
            observe(service, operation="new-observation", selector="other")
            calls_before = len(backend.calls)
            replay = start(service)
            self.assertEqual(replay["observation_ref"], waiting["observation_ref"])
            self.assertEqual(replay["gate"]["gate_id"], waiting["gate"]["gate_id"])
            self.assertEqual(len(backend.calls), calls_before)
        finally:
            service.close()

    def test_automatic_selection_rejects_changed_or_missing_scope(self):
        for dimension, value in (("epoch", 2), ("identity", "boot-2"), ("identity", ""), ("fingerprint", "ssh:another-target")):
            with self.subTest(dimension=dimension, value=value):
                backend = ScopedBackend()
                service = RuntimeMcpService(backend)
                try:
                    observe(service)
                    setattr(backend, dimension, value)
                    turn = start(service)
                    self.assertNotIn("observation_ref", turn)
                    self.assertEqual(backend.calls[-1][0], "debug_run")
                finally:
                    service.close()

    def test_automatic_selection_rejects_other_task_target_expiry_and_ambiguity(self):
        for mode in ("task", "target", "expired", "ambiguous"):
            with self.subTest(mode=mode):
                backend = ScopedBackend()
                service = RuntimeMcpService(backend)
                try:
                    now = [1000.0]
                    service._test.context_runtime.clock = lambda: now[0]
                    observe(service)
                    if mode == "expired":
                        now[0] += 61
                    if mode == "ambiguous":
                        observe(service, operation="second", selector="other")
                    turn = start(service, task="other" if mode == "task" else "continuity", target="192.0.2.91" if mode == "target" else "192.0.2.90")
                    self.assertNotIn("observation_ref", turn)
                    self.assertEqual(backend.calls[-1][0], "debug_run")
                finally:
                    service.close()

    def test_automatic_index_rebuilds_across_service_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            def service(backend):
                return RuntimeMcpService(backend, context_repository=SQLiteRuntimeRepository(root / "runtime.sqlite3"), blob_repository=FilesystemBlobRepository(root / "blobs"))
            first = service(ScopedBackend())
            try:
                previous = observe(first)
            finally:
                first.close()
            backend = ScopedBackend()
            second = service(backend)
            try:
                turn = start(second)
                self.assertEqual(turn["observation_ref"], previous["observation_ref"])
                self.assertEqual([name for name, _ in backend.calls], ["debug_collect"])
                self.assertEqual(backend.calls[0][1]["mdb_queries"], [])
            finally:
                second.close()

    def test_automatic_selection_declines_an_oversized_scan(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            observe(service)
            for index in range(257):
                service._test.context_runtime.blob_repository.put(
                    (f"unrelated-observation-{index}".encode() * 16)
                )
            turn = start(service)
            self.assertNotIn("observation_ref", turn)
            self.assertEqual(backend.calls[-1][0], "debug_run")
        finally:
            service.close()

    def test_automatic_selection_declines_an_oversized_blob(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            observe(service)
            service._test.context_runtime.blob_repository.put(
                b"x" * (8 * 1024 * 1024 + 1)
            )
            turn = start(service)
            self.assertNotIn("observation_ref", turn)
            self.assertEqual(backend.calls[-1][0], "debug_run")
        finally:
            service.close()

    def test_corrupt_blob_consumes_budget_and_cannot_seed_reuse(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            observe(service)
            repository = service._test.context_runtime.blob_repository
            corrupt_id = repository.put(b"valid-but-corrupted")
            repository._blobs[corrupt_id] = b"tampered"
            requests = []
            original_bounded = repository.read_bounded

            def counted_bounded(blob_id, *, max_bytes):
                requests.append(max_bytes + 1)
                return original_bounded(blob_id, max_bytes=max_bytes)

            repository.read_bounded = counted_bounded
            turn = start(service)
            self.assertIn("observation_ref", turn)
            self.assertEqual(backend.calls[-1][0], "debug_collect")
            self.assertLessEqual(sum(requests), 32 * 1024 * 1024)
        finally:
            service.close()

    def test_filesystem_bounded_blob_read_stops_after_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            repository = FilesystemBlobRepository(Path(raw))
            blob_id = repository.put(b"z" * (16 * 1024 * 1024))
            returned_bytes = []
            original_read = gzip.GzipFile.read

            def counted_read(stream, size=-1):
                value = original_read(stream, size)
                returned_bytes.append(len(value))
                return value

            with patch.object(gzip.GzipFile, "read", counted_read):
                self.assertIsNone(repository.read_bounded(blob_id, max_bytes=8 * 1024 * 1024))
            self.assertLessEqual(sum(returned_bytes), 8 * 1024 * 1024 + 1)

    def test_automatic_selection_declines_cumulative_scan_bytes(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            observe(service)
            for index in range(5):
                service._test.context_runtime.blob_repository.put(
                    bytes([65 + index]) * (7 * 1024 * 1024)
                )
            turn = start(service)
            self.assertNotIn("observation_ref", turn)
            self.assertEqual(backend.calls[-1][0], "debug_run")
        finally:
            service.close()

    def test_source_expiring_during_scope_probe_is_not_automatically_selected(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            now = [1000.0]
            service._test.context_runtime.clock = lambda: now[0]
            observe(service)
            original = backend.observe_query
            def delayed_probe(task, arguments, context):
                value = original(task, arguments, context)
                now[0] += 31
                return value
            backend.observe_query = delayed_probe
            turn = start(service)
            self.assertNotIn("observation_ref", turn)
            self.assertEqual(backend.calls[-1][0], "debug_run")
        finally:
            service.close()

    def test_maintenance_preserves_live_standalone_observation_sources(self):
        service = RuntimeMcpService(ScopedBackend())
        try:
            now = [1000.0]
            service._test.context_runtime.clock = lambda: now[0]
            reference = observe(service)["observation_ref"]
            blob_id = reference["digest"].removeprefix("sha256:")
            service._test.context_runtime.maintain()
            self.assertIn(blob_id, service._test.context_runtime.blob_repository.blob_ids())
            now[0] += 901
            service._test.context_runtime.maintain()
            self.assertNotIn(blob_id, service._test.context_runtime.blob_repository.blob_ids())
        finally:
            service.close()

    def test_incremental_evidence_plan_reads_only_missing_values_in_requested_order(self):
        backend = ScopedBackend()
        service = RuntimeMcpService(backend)
        try:
            observe(service, queries=["lsprop Object1"])
            result = observe(service, operation="extend", queries=["lsprop Object0", "lsprop Object1", "lsprop Object2"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual([args["mdb_queries"] for name, args in backend.calls], [["lsprop Object1"], [], ["lsprop Object0", "lsprop Object2"]])
            values = result["results"]["facts"]["values"]
            for index, item in enumerate(values):
                self.assertIn(f"lsprop Object{index}", str(item["value"]))
        finally:
            service.close()

    def test_incremental_collection_never_merges_changed_epoch_or_time_window(self):
        for mode in ("epoch", "time"):
            with self.subTest(mode=mode):
                backend = ScopedBackend()
                service = RuntimeMcpService(backend)
                try:
                    observe(service)
                    if mode == "epoch":
                        backend.change_on_query = "lsprop Object1"
                    else:
                        backend.delay_on_query = "lsprop Object1"
                    result = observe(service, operation="extend", queries=["lsprop Object0", "lsprop Object1"])
                    self.assertEqual(result["status"], "complete")
                    self.assertEqual(backend.calls[-1][1]["mdb_queries"], ["lsprop Object0", "lsprop Object1"])
                    self.assertEqual(len(backend.calls), 4)
                finally:
                    service.close()


if __name__ == "__main__":
    unittest.main()
