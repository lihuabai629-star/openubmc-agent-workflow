from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

try:
    from .support import create_verified_product
except ImportError:
    from support import create_verified_product


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BUILD_ROOT.parent
RUNTIME_ROOT = REPO_ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import GateConflict, RuntimeMcpService  # noqa: E402


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
        return {
            "ok": True,
            "task_id": task.task_id,
            "ip": captured.get("ip"),
            "observed_at": "2026-09-05T00:00:00Z",
            "freshness": {"status": "fresh"},
        }

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        captured = dict(arguments)
        self.calls.append(("upgrade_run", captured))
        self.upgrade_epoch += 1
        return {
            "ok": True,
            "task_id": task.task_id,
            "ip": captured.get("ip"),
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "upgrade",
            },
            "target_epoch": self.upgrade_epoch,
            "verification": {
                "installed_version": captured.get("product_version"),
                "target_epoch": self.upgrade_epoch,
            },
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
            "observed_at": "2026-09-05T00:00:00Z",
            "freshness": {"status": "fresh"},
            "business_acceptance": "passed",
        }


class BuildContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = BuildWorkflowBackend()
        self.service = RuntimeMcpService(self.backend)
        self.task_id = "build-contract-task"
        self.artifact_root = Path(tempfile.mkdtemp(prefix="build-contract-"))

    def tearDown(self) -> None:
        self.service.close()
        shutil.rmtree(self.artifact_root, ignore_errors=True)

    @staticmethod
    def gate_binding(turn: dict[str, object]) -> dict[str, object]:
        gate = turn["gate"]
        assert isinstance(gate, dict)
        return {
            "gate_id": gate["gate_id"],
            "gate_version": gate["gate_version"],
            "schema_digest": gate["schema_digest"],
        }

    def execute(self, arguments: dict[str, object], operation_id: str):
        return self.service.call_exposed_tool(
            "execute",
            arguments,
            task_id=self.task_id,
            operation_id=operation_id,
        )

    def read_case(self, case_id: str, operation_id: str) -> dict[str, object]:
        return self.service.call_tool(
            "case_read",
            {"case_id": case_id},
            task_id=self.task_id,
            operation_id=operation_id,
        )

    def advance_to_build(
        self, *, suffix: str = "main"
    ) -> tuple[str, dict[str, object]]:
        diagnosis = self.execute(
            {
                "kind": "start",
                "target": "192.0.2.120",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            f"{suffix}-start",
        )
        self.assertEqual(diagnosis["gate"]["name"], "diagnosis.acceptance")
        evidence_ids = [
            item["evidence_id"] for item in diagnosis["diagnostic_receipt"]["evidence"]
        ]
        developer = self.execute(
            {
                "kind": "respond",
                "run_id": diagnosis["run_id"],
                **self.gate_binding(diagnosis),
                "response": {
                    "status": "completed",
                    "summary": "diagnosis accepted",
                    "payload": {
                        "root_cause": "source defect isolated",
                        "evidence_ids": evidence_ids,
                        "causal_chain": ["observed defect", "source repair"],
                        "code_owner": "openubmc-developer",
                        "contradictions": [],
                        "remaining_gaps": [],
                        "verification_status": "verified",
                    },
                },
            },
            f"{suffix}-diagnosis",
        )
        self.assertEqual(developer["gate"]["name"], "developer.change")
        build = self.execute(
            {
                "kind": "respond",
                "run_id": diagnosis["run_id"],
                **self.gate_binding(developer),
                "response": {
                    "status": "completed",
                    "summary": "source change completed",
                    "payload": {
                        "source_revision": "source-revision-1",
                        "authored_files": ["src/example.lua"],
                        "verification_plan": ["build", "upgrade", "fresh-debug"],
                    },
                },
            },
            f"{suffix}-developer",
        )
        self.assertEqual(build["gate"]["name"], "build.artifact")
        return str(diagnosis["run_id"]), build

    def artifact_ref(
        self, run_id: str, *, key: str, version: str = "2.0.0"
    ) -> dict[str, object]:
        artifact = self.artifact_root / f"{key}.hpm"
        artifact.write_bytes(f"firmware-{version}-{key}".encode())
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        reference = {
            "handle": str(artifact),
            "digest": "sha256:" + digest,
            "kind": "openubmc-hpm",
            "size": artifact.stat().st_size,
            "provenance": "openubmc-build",
            "retention_hint": "run-lifetime",
            "version": version,
            "target": "192.0.2.120",
            "run_id": run_id,
        }
        Path(str(artifact) + ".metadata.json").write_text(
            json.dumps(
                {
                    "schema": "openubmc-agent-workflow/artifact-metadata-v1",
                    "artifact": {
                        "sha256": digest,
                        "size": reference["size"],
                        "kind": reference["kind"],
                    },
                    "product_version": version,
                    "provenance": reference["provenance"],
                    "package_binding": "package_binding_verified",
                    "upgrade_eligible": True,
                    "evidence_ids": [f"{key}-log", f"{key}-artifact-checksum"],
                }
            ),
            encoding="utf-8",
        )
        return reference

    def build_command(
        self,
        run_id: str,
        gate: dict[str, object],
        *,
        key: str,
        status: str = "completed",
        artifact_ref: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_revision": "source-revision-1",
            "dependency_readiness": {
                "readiness_id": f"{key}-readiness",
                "status": "ready",
                "resolution": "available",
                "summary": "build dependencies resolved",
                "check_commands": ["conan graph info ."],
                "evidence_ids": [f"{key}-dependency"],
                "attempt_count": 1,
                "reused_by": ["build"],
            },
            "validation_results": [
                {
                    "kind": "build",
                    "status": "compiled" if status == "completed" else "compile_failed",
                    "summary": f"build {status}",
                    "commands": ["bmcgo build"],
                    "evidence_ids": [f"{key}-log"],
                    "dependency_readiness_id": f"{key}-readiness",
                }
            ],
        }
        if artifact_ref is not None:
            payload["artifact_ref"] = artifact_ref
        if status == "completed":
            payload.update(
                {
                    "package_binding": "package_binding_verified",
                    "upgrade_eligible": True,
                    "evidence_ids": [f"{key}-log", f"{key}-artifact-checksum"],
                }
            )
        return {
            "kind": "respond",
            "run_id": run_id,
            **self.gate_binding(gate),
            "submission_id": key,
            "response": {
                "status": status,
                "summary": f"build {status}",
                "payload": payload,
            },
        }


class BuildSkillContractTests(unittest.TestCase):
    def test_product_reference_commands_use_only_current_plan_bound_flags(self) -> None:
        verification = (
            BUILD_ROOT / "references" / "artifact-verification.md"
        ).read_text(encoding="utf-8")
        for stale_flag in (
            "--baseline-lock",
            "--actual-lock",
            "--observed-product-version",
            "--identity",
        ):
            self.assertNotIn(stale_flag, verification)

        plan_reference = (
            BUILD_ROOT / "references" / "build-plan.md"
        ).read_text(encoding="utf-8")
        product_example = plan_reference.split(
            "For a product artifact:",
            maxsplit=1,
        )[1]
        for required_flag in (
            "--rootfs-image",
            "--rootfs-service",
            "--baseline-resolved-lock",
            "--artifact-path",
            "--product-version",
        ):
            self.assertIn(required_flag, product_example)

    def test_build_boundary_excludes_target_transport_and_upgrade(self) -> None:
        skill = (BUILD_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(
            "Build never reads target credentials or opens SSH, Telnet, or Redfish sessions",
            skill,
        )
        self.assertIn(
            "do not upload from build or acquire a target lease",
            skill.lower(),
        )
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
                "package_binding",
                "upgrade_eligible",
                "evidence_ids",
            },
        )
        self.assertNotIn("credential", json.dumps(result).lower())
        self.assertNotIn("password", json.dumps(result).lower())
        self.assertNotIn("target_bmc", result)

    def test_skill_requires_the_typed_runtime_handoff_contract(self) -> None:
        skill = (BUILD_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for field in (
            "execute(kind=respond)",
            "execute(kind=resume)",
            "run_id",
            "gate_id",
            "gate_version",
            "schema_digest",
            "artifact_ref",
            "typed Build result",
            "package_binding",
            "upgrade_eligible",
            "artifact_path",
            "artifact_sha256",
            "product_version",
            "evidence_ids",
        ):
            self.assertIn(field, skill)
        self.assertNotIn("workflow.next", skill)
        self.assertNotIn("workflow.advance", skill)
        self.assertNotIn("phase_record", skill)

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

    def test_finalizer_owned_metadata_binds_artifact_and_retires_writer_cli(self) -> None:
        writer = BUILD_ROOT / "scripts" / "write_artifact_metadata.py"
        with tempfile.TemporaryDirectory() as raw:
            fixture = create_verified_product(Path(raw), BUILD_ROOT)
            artifact = fixture["artifact"]
            verification = fixture["verification"]
            sidecar = fixture["metadata"]
            result = subprocess.run(
                [
                    sys.executable,
                    str(writer),
                    "--artifact-path",
                    str(artifact),
                    "--verification",
                    str(verification),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("finalize_product_attempt.py", result.stderr)
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            verification_document = json.loads(
                verification.read_text(encoding="utf-8")
            )
            content = artifact.read_bytes()
            self.assertEqual(
                metadata["artifact"]["sha256"],
                hashlib.sha256(content).hexdigest(),
            )
            self.assertEqual(metadata["artifact"]["size"], len(content))
            self.assertEqual(metadata["product_version"], "12.00.05.03")
            self.assertEqual(
                metadata["build"]["plan_id"],
                verification_document["plan_id"],
            )
            self.assertEqual(
                metadata["build"]["attempt_id"],
                verification_document["attempt_id"],
            )
            self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)


class BuildWorkflowContractTests(BuildContractTestCase):
    def test_completed_artifact_advances_to_upgrade_and_fresh_debug(self) -> None:
        run_id, gate = self.advance_to_build()
        reference = self.artifact_ref(run_id, key="build-one")
        command = self.build_command(
            run_id, gate, key="build-one", artifact_ref=reference
        )
        completed = self.execute(command, "build-one")

        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["outcome"]["status"], "completed")
        upgrade = next(args for name, args in self.backend.calls if name == "upgrade_run")
        verification = next(
            args for name, args in self.backend.calls if name == "debug_collect"
        )
        self.assertEqual(upgrade["ip"], "192.0.2.120")
        self.assertEqual(upgrade["artifact_path"], reference["handle"])
        self.assertEqual(
            upgrade["artifact_sha256"], reference["digest"].removeprefix("sha256:")
        )
        self.assertEqual(upgrade["product_version"], reference["version"])
        self.assertEqual(upgrade["package_binding"], "package_binding_verified")
        self.assertIs(upgrade["upgrade_eligible"], True)
        self.assertEqual(
            upgrade["evidence_ids"], ["build-one-log", "build-one-artifact-checksum"]
        )
        self.assertTrue(upgrade["allow_insecure_tls"])
        case = self.read_case(run_id, "read-build-evidence")
        build = case["workflow_phase_values"]["build.artifact"]
        self.assertEqual(build["artifact_ref"]["digest"], reference["digest"])
        self.assertEqual(
            build["validation_results"][0]["evidence_ids"], ["build-one-log"]
        )
        self.assertFalse(verification["no_freshness"])
        self.assertEqual(
            verification["_minimum_target_epoch"], self.backend.upgrade_epoch
        )
        self.assertEqual(
            [name for name, _args in self.backend.calls],
            ["debug_run", "upgrade_run", "debug_collect"],
        )

    def test_failed_build_stays_terminal_and_retry_requires_a_new_run(self) -> None:
        run_id, gate = self.advance_to_build()
        failed_command = self.build_command(
            run_id, gate, key="build-failed", status="failed"
        )
        self.assertNotIn("artifact_ref", failed_command["response"]["payload"])
        failed = self.execute(failed_command, "build-failed")
        self.assertEqual(failed["state"], "failed")
        self.assertIsNone(failed["gate"])
        resumed = self.execute(
            {"kind": "resume", "run_id": run_id}, "resume-build-failed"
        )
        self.assertEqual(resumed["state"], "failed")
        self.assertIsNone(resumed["gate"])
        self.assertEqual([name for name, _args in self.backend.calls], ["debug_run"])

        with self.assertRaisesRegex(
            GateConflict, "different gate_id|not waiting at a Gate"
        ):
            self.execute(
                self.build_command(
                    run_id,
                    gate,
                    key="build-invalid-retry",
                    artifact_ref=self.artifact_ref(run_id, key="build-invalid-retry"),
                ),
                "build-invalid-retry",
            )

        retry_id, retry_gate = self.advance_to_build(suffix="retry")
        self.assertNotEqual(retry_id, run_id)
        completed = self.execute(
            self.build_command(
                retry_id,
                retry_gate,
                key="build-retry",
                artifact_ref=self.artifact_ref(retry_id, key="build-retry"),
            ),
            "build-retry",
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(
            [name for name, _args in self.backend.calls].count("upgrade_run"), 1
        )

    def test_completed_build_rejects_missing_or_invalid_artifact_identity(self) -> None:
        run_id, gate = self.advance_to_build()
        reference = self.artifact_ref(run_id, key="identity-validation")
        invalid = (
            ("missing-ref", None, "artifact_ref"),
            ("missing-digest", {**reference, "digest": ""}, "digest|empty"),
            ("short-digest", {**reference, "digest": "abc"}, "digest|SHA-256"),
            ("non-hex-digest", {**reference, "digest": "z" * 64}, "digest|SHA-256"),
            ("wrong-digest", {**reference, "digest": "0" * 64}, "digest does not match"),
            (
                "missing-file",
                {**reference, "handle": str(self.artifact_root / "missing.hpm")},
                "content is unavailable",
            ),
            ("wrong-size", {**reference, "size": reference["size"] + 1}, "size does not match"),
            ("wrong-version", {**reference, "version": "9.9.9"}, "version does not match"),
            ("wrong-target", {**reference, "target": "192.0.2.121"}, "target does not match"),
            ("wrong-run", {**reference, "run_id": "run-other"}, "run_id does not match"),
        )
        for key, invalid_ref, message in invalid:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, message):
                    self.execute(
                        self.build_command(
                            run_id, gate, key=key, artifact_ref=invalid_ref
                        ),
                        key,
                    )
                resumed = self.execute(
                    {"kind": "resume", "run_id": run_id}, f"resume-{key}"
                )
                self.assertEqual(resumed["state"], "waiting_response")
                self.assertEqual(self.gate_binding(resumed), self.gate_binding(gate))
                self.assertEqual(
                    [name for name, _args in self.backend.calls], ["debug_run"]
                )

        completed = self.execute(
            self.build_command(run_id, gate, key="valid-build", artifact_ref=reference),
            "valid-build",
        )
        self.assertEqual(completed["state"], "completed")

    def test_build_gate_blocks_missing_or_unverified_upgrade_eligibility(self) -> None:
        run_id, gate = self.advance_to_build()
        reference = self.artifact_ref(run_id, key="eligibility-validation")
        invalid = (
            ("package_binding", None),
            ("package_binding", "package_binding_unverified"),
            ("upgrade_eligible", None),
            ("upgrade_eligible", False),
            ("upgrade_eligible", 1),
            ("upgrade_eligible", "true"),
            ("evidence_ids", None),
            ("evidence_ids", []),
            ("evidence_ids", [" "]),
        )
        for index, (field, value) in enumerate(invalid):
            with self.subTest(field=field, value=value):
                command = self.build_command(
                    run_id, gate, key=f"eligibility-{index}", artifact_ref=reference
                )
                payload = command["response"]["payload"]
                if value is None:
                    payload.pop(field)
                else:
                    payload[field] = value
                with self.assertRaisesRegex(GateConflict, field):
                    self.execute(command, f"eligibility-{index}")
                resumed = self.execute(
                    {"kind": "resume", "run_id": run_id}, f"eligibility-resume-{index}"
                )
                self.assertEqual(self.gate_binding(resumed), self.gate_binding(gate))
                self.assertEqual(
                    [name for name, _args in self.backend.calls], ["debug_run"]
                )

    def test_closed_build_gate_cannot_replace_artifact_or_repeat_upgrade(self) -> None:
        run_id, gate = self.advance_to_build()
        first_command = self.build_command(
            run_id,
            gate,
            key="build-one",
            artifact_ref=self.artifact_ref(run_id, key="build-one"),
        )
        first = self.execute(first_command, "build-one")
        self.assertEqual(first["state"], "completed")
        calls_after_delivery = list(self.backend.calls)

        with self.assertRaisesRegex(GateConflict, "not waiting at a Gate"):
            self.execute(
                self.build_command(
                    run_id,
                    gate,
                    key="build-two",
                    artifact_ref=self.artifact_ref(run_id, key="build-two", version="2.0.1"),
                ),
                "build-two",
            )
        replay = self.execute(first_command, "replay-build-one")
        resumed = self.execute(
            {"kind": "resume", "run_id": run_id}, "resume-completed-delivery"
        )
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(resumed["state"], "completed")
        self.assertIsNone(resumed["gate"])
        self.assertEqual(self.backend.calls, calls_after_delivery)


if __name__ == "__main__":
    unittest.main()
