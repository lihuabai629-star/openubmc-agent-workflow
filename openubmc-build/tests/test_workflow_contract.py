from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BUILD_ROOT.parent
RUNTIME_ROOT = REPO_ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import RuntimeMcpService  # noqa: E402


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.closed = False


class BuildWorkflowBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.upgrade_epoch = 1

    @staticmethod
    def open_task(task_id: str) -> FakeTask:
        return FakeTask(task_id)

    @staticmethod
    def close_task(task: FakeTask) -> None:
        task.closed = True

    @staticmethod
    def maintain_task(_task: FakeTask) -> int:
        return 0

    @staticmethod
    def task_status(task: FakeTask) -> dict[str, object]:
        return {"task_id": task.task_id, "closed": task.closed}

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        captured = dict(arguments)
        self.calls.append(("debug_run", captured))
        return {"ok": True, "task_id": task.task_id, "ip": captured.get("ip")}

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        captured = dict(arguments)
        self.calls.append(("upgrade_run", captured))
        self.upgrade_epoch += 1
        return {
            "ok": True,
            "task_id": task.task_id,
            "ip": captured.get("ip"),
            "journal": {"stage": "verified"},
            "target_epoch": self.upgrade_epoch,
        }

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        captured = dict(arguments)
        self.calls.append(("debug_collect", captured))
        return {
            "ok": True,
            "task_id": task.task_id,
            "ip": captured.get("ip"),
            "profile": captured.get("profile"),
            "target_epoch": captured.get("_minimum_target_epoch", 0),
        }


class BuildContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = BuildWorkflowBackend()
        self.service = RuntimeMcpService(self.backend)
        self.task_id = "build-contract-task"

    def tearDown(self) -> None:
        self.service.close()

    def read_case(self, case_id: str, operation_id: str) -> dict[str, object]:
        return self.service.call_tool(
            "case_read",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id=operation_id,
        )

    def advance_to_build(self) -> str:
        waiting_developer = self.service.call_tool(
            "workflow.advance",
            {
                "ip": "192.0.2.120",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id=self.task_id,
            operation_id="advance-diagnosis",
        )
        self.assertEqual(
            waiting_developer["required_phase_type"],
            "developer.change",
        )
        case_id = str(waiting_developer.envelope["case_id"])
        case = self.read_case(case_id, "read-before-developer")
        self.service.call_tool(
            "phase_record",
            {
                "case_id": case_id,
                "expected_revision": case["revision"],
                "idempotency_key": "developer-change",
                "phase_type": "developer.change",
                "producer_identity": "openubmc-developer",
                "status": "completed",
                "source_revision": "source-revision-1",
                "summary": "source change completed",
                "authored_files": ["src/example.lua"],
                "verification_plan": ["build", "upgrade", "fresh-debug"],
            },
            task_id=self.task_id,
            operation_id="record-developer",
        )
        waiting_build = self.service.call_tool(
            "workflow.next",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id="next-to-build",
        )
        self.assertEqual(waiting_build["required_phase_type"], "build.artifact")
        return case_id

    def record_build(
        self,
        case_id: str,
        *,
        key: str,
        status: str = "completed",
        digest: str = "a" * 64,
        version: str = "2.0.0",
    ):
        case = self.read_case(case_id, f"read-{key}")
        arguments: dict[str, object] = {
            "case_id": case_id,
            "expected_revision": case["revision"],
            "idempotency_key": key,
            "phase_type": "build.artifact",
            "producer_identity": "openubmc-build",
            "status": status,
            "source_revision": "source-revision-1",
            "summary": f"build {status}",
        }
        if status == "completed":
            arguments.update(
                {
                    "artifact_path": "/tmp/openubmc-contract.hpm",
                    "artifact_sha256": digest,
                    "product_version": version,
                    "evidence_ids": ["build-log", "artifact-checksum"],
                }
            )
        return self.service.call_tool(
            "phase_record",
            arguments,
            task_id=self.task_id,
            operation_id=key,
        )


class BuildSkillContractTests(unittest.TestCase):
    def test_build_boundary_excludes_target_transport_and_upgrade(self) -> None:
        skill = (BUILD_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(
            "Build never reads target credentials or opens SSH, Telnet, or Redfish sessions",
            skill,
        )
        self.assertIn("do not upload from Build or acquire a target lease", skill)
        self.assertIn("Do not perform an upgrade from Build", skill)

        forbidden_runtime_owners = (
            "import paramiko",
            "from paramiko",
            "import requests",
            "from requests",
            "import telnetlib",
            "sshpass",
            "redfish/v1",
        )
        for path in sorted((BUILD_ROOT / "scripts").iterdir()):
            if path.suffix not in {".py", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8").lower()
            for marker in forbidden_runtime_owners:
                self.assertNotIn(marker, text, f"{path} owns remote target access")

    def test_typed_build_result_contains_only_artifact_handoff_fields(self) -> None:
        handoff = (BUILD_ROOT / "references" / "handoff-contract.md").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"## Build Result.*?```json\n(?P<body>\{.*?\})\n```",
            handoff,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        result = json.loads(match.group("body"))
        self.assertEqual(
            set(result),
            {
                "artifact_path",
                "artifact_sha256",
                "product_version",
                "evidence_ids",
            },
        )
        self.assertNotIn("credential", json.dumps(result).lower())
        self.assertNotIn("password", json.dumps(result).lower())
        self.assertNotIn("target_bmc", result)

    def test_skill_requires_the_complete_run_gate_contract(self) -> None:
        skill = (BUILD_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for field in (
            "execute(kind=respond)",
            "run_id",
            "gate_id",
            "gate_version",
            "schema_digest",
            "artifact_path",
            "artifact_sha256",
            "product_version",
            "evidence_ids",
            "execute(kind=resume)",
        ):
            self.assertIn(field, skill)
        for retired in ("phase_record", "workflow.next", "workflow.advance"):
            self.assertNotIn(retired, skill)

    def test_checked_runner_rejects_failure_text_even_with_zero_exit(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_bmcgo_checked.py"
        with tempfile.TemporaryDirectory() as raw:
            failure = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--log",
                    str(Path(raw) / "failure.log"),
                    "--",
                    sys.executable,
                    "-c",
                    "print('ERROR build task failed')",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            ignored = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--log",
                    str(Path(raw) / "ignored.log"),
                    "--",
                    sys.executable,
                    "-c",
                    "print('Failed validating optional metadata'); print('0 failed')",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(failure.returncode, 1)
        self.assertIn("failure-looking log lines", failure.stderr)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)


class BuildWorkflowContractTests(BuildContractTestCase):
    def test_completed_artifact_advances_to_upgrade_and_fresh_debug(self) -> None:
        case_id = self.advance_to_build()
        self.record_build(case_id, key="build-one")

        completed = self.service.call_tool(
            "workflow.next",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id="next-delivery",
        )

        self.assertTrue(completed["completed"])
        upgrade = next(args for name, args in self.backend.calls if name == "upgrade_run")
        verification = next(
            args for name, args in self.backend.calls if name == "debug_collect"
        )
        self.assertEqual(upgrade["ip"], "192.0.2.120")
        self.assertEqual(upgrade["artifact_path"], "/tmp/openubmc-contract.hpm")
        self.assertEqual(upgrade["artifact_sha256"], "a" * 64)
        self.assertEqual(upgrade["product_version"], "2.0.0")
        self.assertTrue(upgrade["allow_insecure_tls"])
        case = self.read_case(case_id, "read-build-evidence")
        self.assertEqual(
            case["workflow_phase_values"]["build.artifact"]["evidence_ids"],
            ["build-log", "artifact-checksum"],
        )
        self.assertEqual(verification["profile"], "standard")
        self.assertEqual(
            verification["_minimum_target_epoch"],
            self.backend.upgrade_epoch,
        )
        self.assertNotIn("build_run", [name for name, _args in self.backend.calls])

    def test_failed_build_can_retry_in_the_same_case_without_an_artifact(self) -> None:
        case_id = self.advance_to_build()
        failed = self.record_build(case_id, key="build-failed", status="failed")
        waiting_retry = self.service.call_tool(
            "workflow.advance",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id="advance-build-retry",
        )
        completed = self.record_build(case_id, key="build-retry")

        self.assertEqual(failed["phase_attempt"], 1)
        self.assertEqual(failed["artifact_path"], "")
        self.assertEqual(waiting_retry["required_phase_type"], "build.artifact")
        self.assertEqual(completed["phase_attempt"], 2)
        self.assertEqual(completed["artifact_sha256"], "a" * 64)

    def test_completed_build_rejects_missing_or_invalid_artifact_identity(self) -> None:
        case_id = self.advance_to_build()
        case = self.read_case(case_id, "read-invalid-artifact")
        base = {
            "case_id": case_id,
            "expected_revision": case["revision"],
            "phase_type": "build.artifact",
            "producer_identity": "openubmc-build",
            "status": "completed",
            "source_revision": "source-revision-1",
            "summary": "invalid artifact",
            "artifact_path": "/tmp/openubmc-contract.hpm",
            "product_version": "2.0.0",
        }
        for key, digest in (
            ("missing-digest", ""),
            ("short-digest", "abc"),
            ("non-hex-digest", "z" * 64),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError, "artifact_sha256|SHA-256 artifact hash"
            ):
                self.service.call_tool(
                    "phase_record",
                    {
                        **base,
                        "idempotency_key": key,
                        "artifact_sha256": digest,
                    },
                    task_id=self.task_id,
                    operation_id=key,
                )

    def test_replacing_build_artifact_reruns_only_upgrade_and_verification(self) -> None:
        case_id = self.advance_to_build()
        self.record_build(case_id, key="build-one", digest="a" * 64, version="2.0.0")
        first = self.service.call_tool(
            "workflow.next",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id="next-first-delivery",
        )
        self.assertTrue(first["completed"])

        second_build = self.record_build(
            case_id,
            key="build-two",
            digest="b" * 64,
            version="2.0.1",
        )
        second = self.service.call_tool(
            "workflow.next",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id="next-second-delivery",
        )

        self.assertEqual(second_build["phase_attempt"], 2)
        self.assertTrue(second["completed"])
        debug_runs = [args for name, args in self.backend.calls if name == "debug_run"]
        upgrades = [args for name, args in self.backend.calls if name == "upgrade_run"]
        verifications = [
            args for name, args in self.backend.calls if name == "debug_collect"
        ]
        self.assertEqual(len(debug_runs), 1)
        self.assertEqual(len(upgrades), 2)
        self.assertEqual(len(verifications), 2)
        self.assertEqual(upgrades[-1]["artifact_sha256"], "b" * 64)
        self.assertEqual(upgrades[-1]["product_version"], "2.0.1")


if __name__ == "__main__":
    unittest.main()
