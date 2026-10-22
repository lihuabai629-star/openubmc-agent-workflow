import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_evidence_index import _Backend, _create_public_run
from openubmc_target_runtime import RuntimeMcpService, SQLiteRuntimeRepository
from openubmc_target_runtime.context_runtime import CaseClosed

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("qemu_fixture", ROOT / "qemu-testing/tests/test_workflow_contract.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class QemuEvidenceTests(unittest.TestCase):
    def setUp(self):
        fixture.NOW = datetime.now(timezone.utc).replace(microsecond=0)
        fixture.START = fixture.NOW - timedelta(seconds=120)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repository = SQLiteRuntimeRepository(self.root / "runtime.sqlite3")
        self.target = "192.0.2.80"
        self.run_id = _create_public_run(self.repository, target=self.target)
        self.plan = fixture.plan()
        for key, name in (("launcher", "launcher.py"), ("index", "build.json"), ("image", "product.hpm")):
            path = self.root / name
            path.write_text(name)
            identity = fixture.file_identity(path)
            if key == "launcher":
                self.plan["launcher"].update(identity)
            elif key == "index":
                self.plan["artifact"]["index"] = identity
            else:
                self.plan["artifact"]["image_inputs"] = [{"role": "hpm", **identity}]
        self.result = fixture.result(self.plan)
        self.result["instance"]["artifact_index_sha256"] = self.plan["artifact"]["index"]["sha256"]
        self.packet = {"schema": "openubmc-qemu-evidence/v1", "run_id": self.run_id,
                       "target": self.target, "plan": self.plan, "result": self.result}

    def action(self):
        path = self.root / "qemu-evidence.json"
        path.write_text(json.dumps(self.packet))
        return {"kind": "attach_qemu_evidence", "run_id": self.run_id, "target": self.target,
                "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_agent_attaches_validated_evidence_without_gate_or_outcome_transition(self):
        before = self.repository.load(self.run_id)
        action = self.action()
        for attempt in range(2):
            service = RuntimeMcpService(_Backend(), context_repository=self.repository)
            try:
                self.assertEqual(set(service.interface_catalog.names()), {"observe", "execute"})
                turn = service.call_exposed_tool("execute", action, task_id="qemu", operation_id=f"qemu-attach-{attempt}")
                self.assertNotEqual(turn["state"], "failed", turn)
            finally:
                service.close()
            if attempt == 0:
                Path(action["path"]).unlink()
        after = self.repository.load(self.run_id)
        for field in ("current_gate", "run_outcome", "status", "workflow_state"):
            self.assertEqual(before.get(field), after.get(field), field)
        attached = [ref for ref in after["evidence_refs"] if ref.get("producer") == "qemu-evidence-attach"]
        self.assertEqual(len(attached), 1)
        self.assertTrue(attached[0]["validation"]["freshness_verified"])
        self.assertEqual(attached[0]["blob_id"], action["sha256"])

    def test_rejects_stale_cross_run_digest_and_artifact_drift_without_writes(self):
        for case in ("stale", "cross-run", "digest", "artifact", "target"):
            with self.subTest(case=case):
                original = json.loads(json.dumps(self.packet))
                before = self.repository.load(self.run_id)
                if case == "stale":
                    self.result["smoke"][0]["observed_at"] = "2000-01-01T00:00:00Z"
                if case == "cross-run":
                    self.packet["run_id"] = "another-run"
                action = self.action()
                if case == "digest":
                    action["sha256"] = "0" * 64
                if case == "target":
                    action["target"] = "192.0.2.81"
                if case == "artifact":
                    (self.root / "product.hpm").write_text("changed")
                service = RuntimeMcpService(_Backend(), context_repository=self.repository)
                try:
                    with self.assertRaises(ValueError):
                        service.call_exposed_tool("execute", action, task_id="qemu", operation_id="qemu-" + case)
                finally:
                    service.close()
                self.assertEqual(self.repository.load(self.run_id), before)
                self.packet = original
                self.plan, self.result = self.packet["plan"], self.packet["result"]
                (self.root / "product.hpm").write_text("product.hpm")

    def test_terminal_run_rejects_new_evidence(self):
        self.run_id = _create_public_run(self.repository, target="192.0.2.82", terminal=True)
        self.target = "192.0.2.82"
        self.packet.update(run_id=self.run_id, target=self.target)
        service = RuntimeMcpService(_Backend(), context_repository=self.repository)
        try:
            with self.assertRaises(CaseClosed):
                service.call_exposed_tool("execute", self.action(), task_id="qemu", operation_id="qemu-terminal")
        finally:
            service.close()

    def test_cli_prepares_an_action_the_agent_can_execute(self):
        plan_path, result_path = self.root / "plan.json", self.root / "result.json"
        plan_path.write_text(json.dumps(self.plan))
        result_path.write_text(json.dumps(self.result))
        prepared = subprocess.run([sys.executable,
            str(ROOT / "qemu-testing/scripts/qemu_workflow_contract.py"), "prepare-evidence",
            str(plan_path), str(result_path), "--run-id", self.run_id, "--target", self.target,
            "--output", str(self.root / "bundle.json")], capture_output=True, text=True, timeout=10)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        service = RuntimeMcpService(_Backend(), context_repository=self.repository)
        try:
            turn = service.call_exposed_tool("execute", json.loads(prepared.stdout),
                                             task_id="qemu", operation_id="qemu-cli")
            self.assertNotEqual(turn["state"], "failed", turn)
        finally:
            service.close()
