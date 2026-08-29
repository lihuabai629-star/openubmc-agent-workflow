from __future__ import annotations

import hashlib
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
        return {
            "ok": True,
            "task_id": task.task_id,
            "ip": captured.get("ip"),
            "summary": "diagnosis completed",
            "root_cause": "the bounded source defect was identified",
            "observed_at": "2026-08-29T00:00:00Z",
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
            "artifact_ref",
            "digest: sha256:",
            "kind: openubmc-hpm",
            "retention_hint: run-lifetime",
            "provenance: openubmc-build",
            "execute(kind=resume)",
        ):
            self.assertIn(field, skill)
        for retired in ("phase_record", "workflow.next", "workflow.advance"):
            self.assertNotIn(retired, skill)

    def test_build_contract_classifies_dependency_and_compile_boundaries(self) -> None:
        skill = (BUILD_ROOT / "SKILL.md").read_text(encoding="utf-8")
        handoff = (BUILD_ROOT / "references" / "handoff-contract.md").read_text(
            encoding="utf-8"
        )
        for concept in (
            "check dependency readiness once",
            "dependency_graph_blocked",
            "compile_failed",
            "compiled",
            "blocked_external",
            "Do not fabricate or vendor",
        ):
            self.assertIn(concept, skill + handoff)
        self.assertIn("dependency_readiness", handoff)
        self.assertIn("validation_results", handoff)
        self.assertIn("counts_as_official_ut=false", handoff)

    def test_documented_build_gate_payload_matches_runtime_execute_schema(self) -> None:
        backend = BuildWorkflowBackend()
        service = RuntimeMcpService(backend)
        try:
            developer_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.120",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="build-skill-contract",
                operation_id="build-skill-start",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    "gate_id": developer_gate["gate"]["gate_id"],
                    "gate_version": developer_gate["gate"]["gate_version"],
                    "schema_digest": developer_gate["gate"]["schema_digest"],
                    "response": {
                        "status": "completed",
                        "summary": "source change completed",
                        "payload": {
                            "source_revision": "source-revision-1",
                            "authored_files": ["src/example.lua"],
                            "verification_plan": ["build and verify"],
                        },
                    },
                },
                task_id="build-skill-contract",
                operation_id="build-skill-source",
            )
            with tempfile.TemporaryDirectory() as raw:
                artifact = Path(raw) / "openubmc-contract.hpm"
                artifact.write_bytes(b"openubmc build artifact")
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                Path(str(artifact) + ".metadata.json").write_text(
                    json.dumps(
                        {
                            "schema": "openubmc-agent-workflow/artifact-metadata-v1",
                            "artifact": {
                                "sha256": digest,
                                "size": artifact.stat().st_size,
                                "kind": "openubmc-hpm",
                            },
                            "product_version": "2.0.0",
                            "provenance": "openubmc-build",
                        }
                    ),
                    encoding="utf-8",
                )
                final = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": build_gate["run_id"],
                        "gate_id": build_gate["gate"]["gate_id"],
                        "gate_version": build_gate["gate"]["gate_version"],
                        "schema_digest": build_gate["gate"]["schema_digest"],
                        "response": {
                            "status": "completed",
                            "summary": "build completed",
                            "payload": {
                                "source_revision": "source-revision-1",
                                "artifact_ref": {
                                    "handle": str(artifact),
                                    "digest": "sha256:" + digest,
                                    "kind": "openubmc-hpm",
                                    "size": artifact.stat().st_size,
                                    "provenance": "openubmc-build",
                                    "retention_hint": "run-lifetime",
                                    "version": "2.0.0",
                                    "target": "192.0.2.120",
                                    "run_id": build_gate["run_id"],
                                },
                                "component_versions": ["component/2.0.0"],
                                "build_commands": ["bmcgo build"],
                                "build_logs": ["build-log"],
                                "dependency_readiness": {
                                    "readiness_id": "build-contract-readiness",
                                    "status": "ready",
                                    "resolution": "available",
                                    "summary": "build dependencies resolved",
                                    "check_commands": ["conan graph info ."],
                                    "evidence_ids": ["dependency-log"],
                                    "attempt_count": 1,
                                    "reused_by": ["build"],
                                },
                                "validation_results": [
                                    {
                                        "kind": "build",
                                        "status": "compiled",
                                        "summary": "product compilation completed",
                                        "commands": ["bmcgo build"],
                                        "evidence_ids": ["build-log"],
                                        "dependency_readiness_id": (
                                            "build-contract-readiness"
                                        ),
                                    }
                                ],
                                "known_gaps": [],
                            },
                        },
                    },
                    task_id="build-skill-contract",
                    operation_id="build-skill-artifact",
                )

            self.assertIn(final["state"], {"completed", "failed"}, final)
            self.assertTrue(
                any(
                    fact.get("kind") == "phase"
                    and fact.get("name") == "build.artifact"
                    and fact.get("status") == "completed"
                    for fact in final["facts"]
                ),
                final,
            )
            upgrade = next(
                arguments
                for operation, arguments in backend.calls
                if operation == "upgrade_run"
            )
            self.assertEqual(upgrade["artifact_sha256"], digest)
            self.assertEqual(upgrade["product_version"], "2.0.0")
        finally:
            service.close()

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


if __name__ == "__main__":
    unittest.main()
