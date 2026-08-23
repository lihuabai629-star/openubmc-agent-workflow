from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = SKILL_ROOT.parent / "openubmc-target-runtime"
sys.path.insert(0, str(SKILL_ROOT))
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_log_analyzer import LogBundleMcpBackend, LogBundleStages  # noqa: E402
from openubmc_target_runtime import (  # noqa: E402
    LocalArtifactStore,
    ReferenceViolation,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)


def write_bundle(path: Path) -> None:
    security = b"2026-08-23 login failed password=secret\n"
    dump_info = b"openUBMC one-click dump\n"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("dump/dump_info/dump_info.txt")
        info.size = len(dump_info)
        archive.addfile(info, io.BytesIO(dump_info))
        info = tarfile.TarInfo("dump/dump_info/LogDump/security.log")
        info.size = len(security)
        archive.addfile(info, io.BytesIO(security))


class LogBundleStageTests(unittest.TestCase):
    def test_sqlite_runtime_rebinds_the_same_persistent_artifact_store_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "dump.tar.gz"
            database = root / "runtime.sqlite3"
            write_bundle(bundle)
            first_backend = LogBundleMcpBackend()
            first = RuntimeMcpService(
                first_backend,
                context_repository=SQLiteRuntimeRepository(database),
            )
            try:
                bundle_ref = first_backend.stages.collect(
                    bundle,
                    target="192.0.2.48",
                    run_id="persistent-stage-task",
                    operation_id="effect-collect",
                    transport="redfish",
                    remote_bundle_path="/tmp/dump.tar.gz",
                    generation_ran=True,
                )["artifact_ref"]
            finally:
                first.close()

            second_backend = LogBundleMcpBackend()
            second = RuntimeMcpService(
                second_backend,
                context_repository=SQLiteRuntimeRepository(database),
            )
            try:
                indexed = second.call_tool(
                    "log_bundle_index",
                    {
                        "ip": "192.0.2.48",
                        "artifact_ref": bundle_ref,
                        "deadline": 10,
                    },
                    task_id="persistent-stage-task",
                    operation_id="effect-index",
                )
            finally:
                second.close()

            self.assertEqual(indexed["artifact_ref"]["kind"], "openubmc-log-index")

    def test_runtime_internal_stage_uses_the_shared_artifact_store_and_pack_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "dump.tar.gz"
            write_bundle(bundle)
            store = LocalArtifactStore(content_root=root / "artifacts")
            backend = LogBundleMcpBackend(artifact_store=store)
            bundle_ref = backend.stages.collect(
                bundle,
                target="192.0.2.49",
                run_id="runtime-stage-task",
                operation_id="effect-collect",
                transport="redfish",
                remote_bundle_path="/tmp/dump.tar.gz",
                generation_ran=True,
            )["artifact_ref"]
            service = RuntimeMcpService(backend)
            try:
                indexed = service.call_tool(
                    "log_bundle_index",
                    {
                        "ip": "192.0.2.49",
                        "artifact_ref": bundle_ref,
                        "deadline": 10,
                    },
                    task_id="runtime-stage-task",
                    operation_id="effect-index",
                )
            finally:
                service.close()

            self.assertEqual(indexed["artifact_ref"]["kind"], "openubmc-log-index")

    def test_collect_index_query_export_form_a_content_bound_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "dump.tar.gz"
            write_bundle(bundle)
            store = LocalArtifactStore(content_root=root / "artifacts")
            stages = LogBundleStages(store)

            collected = stages.collect(
                bundle,
                target="192.0.2.50",
                run_id="run-log-pipeline",
                operation_id="effect-collect",
                transport="redfish",
                remote_bundle_path="/tmp/dump.tar.gz",
                generation_ran=True,
            )
            bundle_ref = collected["artifact_ref"]
            self.assertEqual(bundle_ref["kind"], "openubmc-log-bundle")
            self.assertNotIn("bundle_root", collected)

            indexed = stages.index(
                bundle_ref,
                target="192.0.2.50",
                run_id="run-log-pipeline",
                operation_id="effect-index",
            )
            index_ref = indexed["artifact_ref"]
            index_body = json.loads(
                store.resolve(
                    store.reference(index_ref),
                    expected_kinds=("openubmc-log-index",),
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(index_body["source_artifact_ref"], bundle_ref)
            self.assertIn("dump_info/LogDump/security.log", index_body["entries"])

            queried = stages.query(
                index_ref,
                target="192.0.2.50",
                run_id="run-log-pipeline",
                operation_id="effect-query",
                problem="login failed",
                max_files=4,
                max_lines=8,
            )
            query_ref = queried["artifact_ref"]
            encoded_query = store.resolve(
                store.reference(query_ref),
                require_redacted=True,
            ).read_text(encoding="utf-8")
            self.assertNotIn("secret", encoded_query)
            self.assertIn("<redacted>", encoded_query)

            exported = stages.export(
                query_ref,
                target="192.0.2.50",
                run_id="run-log-pipeline",
                operation_id="effect-export",
            )
            report_ref = exported["artifact_ref"]
            report = store.resolve(
                store.reference(report_ref),
                expected_kinds=("openubmc-log-report",),
                require_redacted=True,
            ).read_text(encoding="utf-8")
            self.assertIn("login failed", report)
            self.assertNotIn("secret", report)

    def test_each_stage_rejects_cross_run_and_wrong_kind_references(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "dump.tar.gz"
            write_bundle(bundle)
            store = LocalArtifactStore(content_root=root / "artifacts")
            stages = LogBundleStages(store)
            bundle_ref = stages.collect(
                bundle,
                target="192.0.2.60",
                run_id="run-owner",
                operation_id="effect-collect",
                transport="ssh",
                remote_bundle_path="/tmp/dump.tar.gz",
                generation_ran=False,
            )["artifact_ref"]

            with self.assertRaisesRegex(ReferenceViolation, "run_id"):
                stages.index(
                    bundle_ref,
                    target="192.0.2.60",
                    run_id="run-intruder",
                    operation_id="effect-index",
                )
            with self.assertRaisesRegex(ReferenceViolation, "kind"):
                stages.query(
                    bundle_ref,
                    target="192.0.2.60",
                    run_id="run-owner",
                    operation_id="effect-query",
                    problem="login failed",
                )


if __name__ == "__main__":
    unittest.main()
