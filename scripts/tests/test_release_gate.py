from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "release_gate.py"
SPEC = importlib.util.spec_from_file_location("openubmc_release_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_gate)


SOURCE_COMMIT = "a" * 40
RELEASE_COMMIT = "b" * 40
MODEL_IDENTITY = {"model": "codex-product-client-qualification"}
CODEX_IDENTITY = {
    "client_info_name": "codex-adoption-qualification",
    "client_info_version": "1",
}


def execute_release_gate(**kwargs: object) -> dict[str, object]:
    return release_gate.execute_release_gate(
        model_identity=MODEL_IDENTITY,
        codex_identity=CODEX_IDENTITY,
        **kwargs,
    )


def codex_adoption_report(
    source_commit: str = SOURCE_COMMIT,
) -> dict[str, object]:
    launcher_identity = {
        "schema": "openubmc-agent-workflow.codex-launcher-identity.v1",
        "runtime_api": "openubmc.target-runtime.v1",
        "runtime_content_digest": "sha256:" + "5" * 64,
        "source_commit": source_commit,
        "entrypoint": "openubmc-debug/scripts/target_runtime_mcp.py",
    }
    report: dict[str, object] = {
        "schema": "openubmc-agent-workflow.codex-adoption-qualification.v1",
        "source_commit": source_commit,
        "qualified": True,
        "maintenance_checkpoint_ready": True,
        "maintenance_checkpoint_blockers": [],
        "failed_dimensions": [],
        "dimensions": {
            "installation_identity": {
                "status": "passed",
                "failure_codes": [],
                "source_clean": True,
                "release_version": "2.0.2",
                "source_commit": source_commit,
                "release_commit": RELEASE_COMMIT,
                "lock_digest": "sha256:" + "1" * 64,
                "source_tree_digest": "sha256:" + "2" * 64,
                "workflow_digest": "sha256:" + "3" * 64,
                "clients": ["codex"],
                "source_mode": "managed",
                "trust_mode": "verified-immutable-source",
                "operational_ready": True,
                "release_identity_verified": True,
                "evaluation_ready": True,
                "skill_digests": {
                    "openubmc-debug": "sha256:" + "4" * 64,
                },
                "runtime_api": "openubmc.target-runtime.v1",
                "runtime_content_digest": "sha256:" + "5" * 64,
            },
            "codex_mcp": {
                "status": "passed",
                "failure_codes": [],
                "configured": True,
                "registration_verified": True,
                "runtime_launcher_verified": True,
                "runtime_invocation": "client-configured-mcp-command",
                "protocol_exchange": [
                    "initialize",
                    "tools/list",
                    "tools/call:execute",
                ],
                "tools": ["execute", "observe"],
                "identity_bound": True,
                "installed_source_commit": source_commit,
                "runtime_api": "openubmc.target-runtime.v1",
                "runtime_content_digest": "sha256:" + "5" * 64,
                "launcher_state_verified": True,
                "launcher_identity": launcher_identity,
                "launcher_identity_digest": release_gate.evidence_fingerprint(
                    launcher_identity
                ),
                "workflow_exchange": {
                    "tool": "execute",
                    "state": "preflight_failed",
                    "classification": "preflight_failure",
                    "error_field": "run_id",
                    "canonical_retry": {
                        "kind": "resume",
                        "run_id": "<current Run ID>",
                    },
                    "is_error": True,
                },
                "restart_verified": True,
                "mcp_closeout": {
                    "status": "passed",
                    "task_closeout_ready": True,
                    "identity_records_valid": True,
                    "isolation_verified": True,
                    "summary": {
                        "record_count": 2,
                        "live_processes": 0,
                        "active_requests": 0,
                        "confirmed_live_orphans": 0,
                        "unattributed_live_processes": 0,
                        "owned_live_processes": 0,
                    },
                    "closeout_checks": {
                        "active_requests_zero": True,
                        "confirmed_live_orphans_zero": True,
                        "unattributed_live_processes_zero": True,
                        "owned_live_processes_zero": True,
                    },
                    "isolation": {
                        "qualification_root": "/isolated",
                        "task_home": "/isolated/home",
                        "codex_config_root": "/isolated/codex",
                        "runtime_state_root": "/isolated/runtime-state",
                        "lifecycle_root": "/isolated/mcp-processes",
                        "global_codex_state_used": False,
                        "configured_client_invocation": True,
                    },
                },
                "mcp_lifecycle_records": [
                    {
                        "schema": "openubmc.mcp-process-lifecycle.v1",
                        "component": "target-runtime",
                        "version": "openubmc.target-runtime.v1",
                        "client": "codex",
                        "task_id": "codex-adoption-probe",
                        "session_id": "codex-adoption-session",
                        "source_commit": source_commit,
                        "formal_run": True,
                        "model_identity": MODEL_IDENTITY,
                        "codex_identity": CODEX_IDENTITY,
                        "parent_pid": 123,
                        "parent_identity": "parent-identity",
                        "parent_identity_verified": True,
                        "parent_identity_currently_verified": True,
                        "process_id": 456,
                        "process_identity": "process-identity",
                        "start_time": "2026-08-31T00:00:00Z",
                        "runtime_state_root": "/isolated/runtime-state",
                        "lifecycle_state": "stopped",
                        "active_requests": 0,
                        "exit_reason": "task-closeout",
                    },
                    {
                        "schema": "openubmc.mcp-process-lifecycle.v1",
                        "component": "target-runtime",
                        "version": "openubmc.target-runtime.v1",
                        "client": "codex",
                        "task_id": "codex-adoption-probe",
                        "session_id": "codex-adoption-session",
                        "source_commit": source_commit,
                        "formal_run": True,
                        "model_identity": MODEL_IDENTITY,
                        "codex_identity": CODEX_IDENTITY,
                        "parent_pid": 123,
                        "parent_identity": "parent-identity",
                        "parent_identity_verified": True,
                        "parent_identity_currently_verified": True,
                        "process_id": 457,
                        "process_identity": "process-identity-2",
                        "start_time": "2026-08-31T00:00:01Z",
                        "runtime_state_root": "/isolated/runtime-state",
                        "lifecycle_state": "stopped",
                        "active_requests": 0,
                        "exit_reason": "task-closeout",
                    },
                ],
            },
            "product_contract": {
                "status": "passed",
                "returncode": 0,
                "tests": ["qualification.product"],
            },
            "task_matrix": {
                "status": "passed",
                "correctness_primary": True,
                "completion_primary": True,
                "terminal_contract_primary": True,
                "groups": {"source_only": {"status": "passed"}},
            },
            "projection": {
                "status": "passed",
                "correctness_primary": True,
                "repeated_reference": True,
                "full_bytes": 2,
                "reference_bytes": 1,
                "saved_bytes": 1,
                "operator_projection_covered": True,
            },
            "lifecycle": {
                "status": "passed",
                "closeout": {
                    "status": "passed",
                    "task_closeout_ready": True,
                    "identity_records_valid": True,
                    "isolation_verified": True,
                    "summary": {
                        "active_requests": 0,
                        "live_processes": 0,
                        "confirmed_live_orphans": 0,
                        "unattributed_live_processes": 0,
                        "owned_live_processes": 0,
                    },
                    "closeout_checks": {
                        "active_requests_zero": True,
                        "confirmed_live_orphans_zero": True,
                        "unattributed_live_processes_zero": True,
                        "owned_live_processes_zero": True,
                    },
                },
            },
        },
        "provenance": {
            "source": {
                "commit": source_commit,
                "qualification_commit": source_commit,
                "continuous_closeout_digest": "sha256:" + "7" * 64,
            },
            "model": MODEL_IDENTITY,
            "codex": CODEX_IDENTITY,
        },
        "external_evaluation": {
            "blocking": False,
            "harnesses": ["dsh"],
            "isolation": {"status": "failed"},
            "required_for_maintenance_checkpoint": False,
        },
        "release_gate": {
            "evidence_type": "codex-adoption-qualification",
            "eligible": True,
        },
    }
    report["evidence_digest"] = release_gate.evidence_fingerprint(report)
    return report


@contextmanager
def resolved_candidate(
    requested_ref: str,
    *,
    release_commit: str = RELEASE_COMMIT,
    source_commit: str = SOURCE_COMMIT,
    release_version: str = "1.1.2",
):
    with patch.object(
        release_gate,
        "_resolve_release_candidate",
        return_value=release_gate.ReleaseCandidate(
            requested_ref=requested_ref,
            release_commit=release_commit,
            source_commit=source_commit,
            release_version=release_version,
        ),
    ), patch.object(release_gate, "require_published_candidate"):
        yield


class ReleaseGateTests(unittest.TestCase):
    def assert_release_candidate_rejected(
        self,
        candidate: SimpleNamespace,
        *,
        previous_ref: str,
        pattern: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            release_gate,
            "_resolve_release_candidate",
            return_value=candidate,
        ), patch.object(release_gate, "require_published_candidate"):
            with self.assertRaisesRegex(ValueError, pattern):
                execute_release_gate(
                    current_ref=candidate.requested_ref,
                    previous_ref=previous_ref,
                    workspace=Path.cwd(),
                    work_root=Path(directory),
                    executor=lambda command, *, cwd: subprocess.CompletedProcess(
                        command,
                        0,
                        "ok",
                        "",
                    ),
                )

    def test_release_tag_must_match_lock_version(self) -> None:
        candidate = SimpleNamespace(
            requested_ref="v2.0.2",
            release_commit=RELEASE_COMMIT,
            source_commit=SOURCE_COMMIT,
            release_version="2.0.1",
        )
        self.assert_release_candidate_rejected(
            candidate,
            previous_ref="v2.0.1",
            pattern="does not match",
        )

    def test_release_version_must_advance_from_previous_tag(self) -> None:
        candidate = SimpleNamespace(
            requested_ref="HEAD",
            release_commit=RELEASE_COMMIT,
            source_commit=SOURCE_COMMIT,
            release_version="2.0.1",
        )
        self.assert_release_candidate_rejected(
            candidate,
            previous_ref="v2.0.1",
            pattern="must be newer",
        )

    def test_maintenance_release_requires_the_immediate_patch_predecessor(self) -> None:
        candidate = SimpleNamespace(
            requested_ref="v2.0.2",
            release_commit=RELEASE_COMMIT,
            source_commit=SOURCE_COMMIT,
            release_version="2.0.2",
        )
        self.assert_release_candidate_rejected(
            candidate,
            previous_ref="v2.0.0",
            pattern="immediate maintenance predecessor",
        )

    def test_symbolic_release_ref_must_be_a_strict_version_tag(self) -> None:
        candidate = SimpleNamespace(
            requested_ref="candidate-final",
            release_commit=RELEASE_COMMIT,
            source_commit=SOURCE_COMMIT,
            release_version="2.0.2",
        )
        self.assert_release_candidate_rejected(
            candidate,
            previous_ref="v2.0.1",
            pattern="strict vMAJOR.MINOR.PATCH tag",
        )

    def test_release_tag_is_validated_without_replacing_requested_ref(self) -> None:
        def succeed(command, *, cwd):
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "HEAD",
            release_version="2.0.2",
        ):
            report = execute_release_gate(
                current_ref="HEAD",
                release_tag="v2.0.2",
                previous_ref="v2.0.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
            )

        self.assertEqual(report["requested_ref"], "HEAD")
        self.assertEqual(report["release_tag"], "v2.0.2")

    def test_all_release_gates_are_required_for_promotion(self) -> None:
        calls: list[tuple[str, ...]] = []

        def succeed(command, *, cwd):
            self.assertTrue(cwd.is_dir())
            calls.append(tuple(command))
            if "codex_adoption_qualification.py" in " ".join(command):
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(codex_adoption_report()),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "v1.1.2"
        ):
            root = Path(directory)
            report = execute_release_gate(
                current_ref="v1.1.2",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=root,
                executor=succeed,
            )

        self.assertTrue(report["promotable"])
        self.assertEqual(
            [item["name"] for item in report["gates"]],
            [
                "github_ci",
                "clean_install",
                "upgrade",
                "rollback",
                "agent_interface",
                "source_only",
                "live_patch",
                "build_upgrade",
                "replay_smoke",
                "old_schema_compatibility",
                "domain_pack_conformance",
                "runtime_safety_qualification",
                "codex_adoption_qualification",
                "agent_gateway_ab_evidence",
            ],
        )
        self.assertTrue(all(item["status"] == "passed" for item in report["gates"]))
        self.assertEqual(len(calls), 15)
        github_ci = calls[0]
        self.assertIn("github_ci_evidence.py", github_ci[1])
        self.assertEqual(
            github_ci[github_ci.index("--commit") + 1],
            SOURCE_COMMIT,
        )
        self.assertEqual(report["source_commit"], SOURCE_COMMIT)
        self.assertEqual(
            report["formal_identity"],
            {"model": MODEL_IDENTITY, "codex": CODEX_IDENTITY},
        )
        self.assertRegex(report["environment_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_failure_blocks_later_gates_and_promotion(self) -> None:
        call_count = 0

        def fail_upgrade(command, *, cwd):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(
                command,
                19 if call_count == 4 else 0,
                "",
                "upgrade failed" if call_count == 4 else "",
            )

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "v1.1.2"
        ):
            report = execute_release_gate(
                current_ref="v1.1.2",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=fail_upgrade,
            )

        self.assertFalse(report["promotable"])
        self.assertEqual(
            [item["status"] for item in report["gates"]],
            ["passed", "passed", "failed"] + ["skipped"] * 11,
        )
        self.assertEqual(call_count, 4)

    def test_upgrade_installs_previous_then_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gates = dict(
                release_gate.gate_commands(
                    current_ref="v1.2.0",
                    previous_ref="v1.1.1",
                    clean_home=root / "clean",
                    lifecycle_home=root / "lifecycle",
                    source_commit="a" * 40,
                    model_identity=MODEL_IDENTITY,
                    codex_identity=CODEX_IDENTITY,
                )
            )

        upgrade = gates["upgrade"]
        self.assertEqual(upgrade[0][upgrade[0].index("--ref") + 1], "v1.1.1")
        self.assertEqual(upgrade[1][upgrade[1].index("--ref") + 1], "v1.2.0")
        rollback = gates["rollback"][0]
        self.assertIn("rollback", rollback)
        self.assertIn("--skip-tool-install", rollback)
        qualification = gates["runtime_safety_qualification"][0]
        self.assertIn("runtime_qualification.py", qualification[1])
        self.assertIn("--output", qualification)
        self.assertEqual(
            qualification[qualification.index("--source-commit") + 1],
            "a" * 40,
        )
        adoption = gates["codex_adoption_qualification"][0]
        self.assertIn("codex_adoption_qualification.py", adoption[1])
        self.assertIn("--output", adoption)
        self.assertEqual(
            adoption[adoption.index("--source-commit") + 1],
            "a" * 40,
        )
        self.assertEqual(
            json.loads(adoption[adoption.index("--model-identity") + 1]),
            MODEL_IDENTITY,
        )
        self.assertEqual(
            json.loads(adoption[adoption.index("--codex-identity") + 1]),
            CODEX_IDENTITY,
        )
        ab_evidence = gates["agent_gateway_ab_evidence"][0]
        self.assertIn("agent_gateway_ab.py", ab_evidence[1])
        self.assertIn("verify", ab_evidence)
        self.assertEqual(
            ab_evidence[ab_evidence.index("--source-ref") + 1],
            "a" * 40,
        )
        self.assertEqual(
            Path(ab_evidence[ab_evidence.index("--attestation-public-key") + 1]),
            root / "agent-gateway-ab-attestation.pub",
        )

    def test_symbolic_commit_and_tag_resolve_one_release_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "release-gate@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Gate Test"],
                cwd=root,
                check=True,
            )
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "source"], cwd=root, check=True
            )
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            (root / "release-lock.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "release-lock.json"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "lock"], cwd=root, check=True
            )
            lock_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(
                ["git", "tag", "v1.1.2", lock_commit],
                cwd=root,
                check=True,
            )

            with patch.object(
                release_gate,
                "verify_release_lock",
                return_value={
                    "source_commit": source_commit,
                    "release_version": "1.1.2",
                },
            ), patch.object(
                release_gate,
                "require_published_candidate",
            ):
                reports = []
                for index, requested_ref in enumerate(
                    ("HEAD", lock_commit, "v1.1.2")
                ):
                    reports.append(
                        execute_release_gate(
                            current_ref=requested_ref,
                            previous_ref="v1.1.1",
                            workspace=root,
                            work_root=root / f"gate-{index}",
                            executor=lambda command, *, cwd: subprocess.CompletedProcess(
                                command,
                                0,
                                "ok",
                                "",
                            ),
                        )
                    )

        self.assertEqual(
            {report["release_commit"] for report in reports},
            {lock_commit},
        )
        self.assertEqual(
            {report["source_commit"] for report in reports},
            {source_commit},
        )
        self.assertEqual(
            [report["requested_ref"] for report in reports],
            ["HEAD", lock_commit, "v1.1.2"],
        )

    def test_invalid_release_lock_fails_instead_of_falling_back(self) -> None:
        with patch.object(
            release_gate,
            "_resolve_commit",
            return_value="b" * 40,
        ), patch.object(
            release_gate,
            "verify_release_lock",
            side_effect=release_gate.ReleaseLockError("bad lock"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid immutable release ref"):
                release_gate._resolve_release_candidate(
                    Path.cwd(),
                    "release-ref",
                )

    def test_managed_checks_install_the_resolved_release_commit(self) -> None:
        release_commit = "b" * 40
        source_commit = "a" * 40
        calls: list[tuple[str, ...]] = []

        def succeed(command, *, cwd):
            calls.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "HEAD",
            release_commit=release_commit,
            source_commit=source_commit,
            release_version="2.0.1",
        ):
            report = execute_release_gate(
                current_ref="HEAD",
                previous_ref="v2.0.0",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
            )

        install_refs = [
            command[command.index("--ref") + 1]
            for command in calls
            if "bootstrap.py" in " ".join(command)
        ]
        self.assertEqual(install_refs, [release_commit, "v2.0.0", release_commit])
        self.assertEqual(report["requested_ref"], "HEAD")
        self.assertEqual(report["release_commit"], release_commit)
        self.assertEqual(report["source_commit"], source_commit)

    def test_unpublished_candidate_fails_before_expensive_gates(self) -> None:
        calls = 0

        def should_not_run(command, *, cwd):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            release_gate,
            "_resolve_release_candidate",
            return_value=release_gate.ReleaseCandidate(
                requested_ref="HEAD",
                release_commit="b" * 40,
                source_commit="a" * 40,
                release_version="2.0.1",
            ),
        ), patch.object(
            release_gate,
            "require_published_candidate",
            side_effect=ValueError(
                "release candidate is not published or reachable from GitHub; "
                "push the lock-only commit or tag before running Release Gate"
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "not published or reachable.*push the lock-only commit or tag",
            ):
                execute_release_gate(
                    current_ref="HEAD",
                    previous_ref="v2.0.0",
                    workspace=Path.cwd(),
                    work_root=Path(directory),
                    executor=should_not_run,
                )

        self.assertEqual(calls, 0)

    def test_cli_reports_unpublished_candidate_without_a_traceback(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            release_gate,
            "execute_release_gate",
            side_effect=ValueError(
                "release candidate is not published or reachable from GitHub; "
                "push the lock-only commit or tag before running Release Gate"
            ),
        ), redirect_stderr(stderr):
            returncode = release_gate.main(
                [
                    "--current-ref",
                    "HEAD",
                    "--previous-ref",
                    "v2.0.0",
                    "--model-identity",
                    json.dumps(MODEL_IDENTITY),
                    "--codex-identity",
                    json.dumps(CODEX_IDENTITY),
                    "--work-root",
                    directory,
                    "--ab-evidence",
                    str(Path(directory) / "summary.json"),
                    "--ab-attestation-public-key",
                    str(Path(directory) / "attestation.pub"),
                ]
            )

        self.assertEqual(returncode, 2)
        self.assertIn("not published or reachable", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("HTTP Error 404", stderr.getvalue())

    def test_github_missing_commit_errors_become_publication_guidance(self) -> None:
        for detail in (
            "gh: Not Found (HTTP 404)",
            "gh: No commit found for SHA (HTTP 422)",
        ):
            with self.subTest(detail=detail), patch.object(
                release_gate.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["gh", "api"],
                    1,
                    "",
                    detail,
                ),
            ) as run:
                with self.assertRaisesRegex(
                    ValueError,
                    "not published or reachable.*push the lock-only commit or tag",
                ) as raised:
                    release_gate.require_published_candidate(
                        "b" * 40,
                        "owner/repository",
                    )

            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], ["gh", "api"])
            self.assertIn("repos/owner/repository/commits/" + "b" * 40, argv)
            self.assertNotIn("HTTP Error 404", str(raised.exception))
            self.assertNotIn("HTTP 422", str(raised.exception))

    def test_self_referencing_release_lock_is_rejected(self) -> None:
        release_commit = "c" * 40
        with patch.object(
            release_gate,
            "_resolve_commit",
            return_value=release_commit,
        ), patch.object(
            release_gate,
            "verify_release_lock",
            return_value={"source_commit": release_commit},
        ):
            with self.assertRaisesRegex(ValueError, "lock-only child"):
                release_gate._resolve_release_candidate(
                    Path.cwd(),
                    "release-ref",
                )

    def test_release_report_digests_qualification_evidence(self) -> None:
        def succeed(command, *, cwd):
            if "runtime_qualification.py" in " ".join(command):
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps({"promotable": True, "violations": {}}),
                    encoding="utf-8",
                )
            if "codex_adoption_qualification.py" in " ".join(command):
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(codex_adoption_report()),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "HEAD"
        ):
            report = execute_release_gate(
                current_ref="HEAD",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
            )

        for name in ("runtime_qualification", "codex_adoption_qualification"):
            with self.subTest(name=name):
                artifact = report["artifacts"][name]
                self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
                self.assertGreater(artifact["size_bytes"], 0)

        adoption_artifact = report["artifacts"]["codex_adoption_qualification"]
        self.assertEqual(
            adoption_artifact["schema"],
            "openubmc-agent-workflow.codex-adoption-qualification.v1",
        )
        self.assertEqual(adoption_artifact["source_commit"], SOURCE_COMMIT)
        self.assertTrue(adoption_artifact["qualified"])
        self.assertTrue(adoption_artifact["maintenance_checkpoint_ready"])
        self.assertEqual(
            adoption_artifact["evidence_digest"],
            codex_adoption_report()["evidence_digest"],
        )

    def test_release_gate_rejects_malformed_codex_adoption_evidence(self) -> None:
        def succeed(command, *, cwd):
            if "codex_adoption_qualification.py" in " ".join(command):
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(
                        {
                            "qualified": True,
                            "maintenance_checkpoint_ready": True,
                            "failed_dimensions": [],
                        }
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "HEAD"
        ):
            report = execute_release_gate(
                current_ref="HEAD",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
            )

        gates = {item["name"]: item for item in report["gates"]}
        self.assertFalse(report["promotable"])
        self.assertEqual(
            gates["codex_adoption_qualification"]["status"], "failed"
        )
        self.assertIn(
            "schema",
            gates["codex_adoption_qualification"]["commands"][0][
                "stderr_tail"
            ],
        )

    def test_release_gate_rejects_adoption_evidence_for_another_identity(
        self,
    ) -> None:
        def succeed(command, *, cwd):
            if "codex_adoption_qualification.py" in " ".join(command):
                report = codex_adoption_report()
                report["provenance"]["model"] = {"model": "another-model"}
                report["evidence_digest"] = release_gate.evidence_fingerprint(
                    {
                        key: value
                        for key, value in report.items()
                        if key != "evidence_digest"
                    }
                )
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "HEAD"
        ):
            report = execute_release_gate(
                current_ref="HEAD",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
            )

        gates = {item["name"]: item for item in report["gates"]}
        self.assertFalse(report["promotable"])
        self.assertIn(
            "model identity mismatch",
            gates["codex_adoption_qualification"]["commands"][0][
                "stderr_tail"
            ],
        )

    def test_release_gate_rejects_digest_valid_skeletal_adoption_report(
        self,
    ) -> None:
        skeletal = {
            "schema": "openubmc-agent-workflow.codex-adoption-qualification.v1",
            "source_commit": SOURCE_COMMIT,
            "qualified": True,
            "maintenance_checkpoint_ready": True,
            "maintenance_checkpoint_blockers": [],
            "failed_dimensions": [],
            "dimensions": {
                name: {"status": "passed"}
                for name in (
                    "installation_identity",
                    "codex_mcp",
                    "product_contract",
                    "task_matrix",
                    "projection",
                    "lifecycle",
                )
            },
            "release_gate": {
                "evidence_type": "codex-adoption-qualification",
                "eligible": True,
            },
        }
        skeletal["evidence_digest"] = release_gate.evidence_fingerprint(
            skeletal
        )

        with self.assertRaisesRegex(ValueError, "installation identity"):
            release_gate.verify_codex_adoption_report(
                skeletal,
                expected_source_commit=SOURCE_COMMIT,
                require_ready=True,
            )

    def test_adoption_verifier_rejects_failed_dimension_missing_from_checkpoint_blockers(
        self,
    ) -> None:
        report = codex_adoption_report()
        report["dimensions"]["codex_mcp"]["status"] = "failed"
        report["dimensions"]["codex_mcp"]["failure_codes"] = [
            "agent_tools_invalid"
        ]
        report["failed_dimensions"] = ["codex_mcp"]
        report["qualified"] = False
        report["evidence_digest"] = release_gate.evidence_fingerprint(
            {key: value for key, value in report.items() if key != "evidence_digest"}
        )

        with self.assertRaisesRegex(ValueError, "checkpoint blockers"):
            release_gate.verify_codex_adoption_report(report)

    def test_adoption_verifier_reuses_launcher_identity_contract(self) -> None:
        report = codex_adoption_report()
        launcher = report["dimensions"]["codex_mcp"]["launcher_identity"]
        launcher["schema"] = "invalid-launcher-schema"
        report["dimensions"]["codex_mcp"]["launcher_identity_digest"] = (
            release_gate.evidence_fingerprint(launcher)
        )
        report["evidence_digest"] = release_gate.evidence_fingerprint(
            {key: value for key, value in report.items() if key != "evidence_digest"}
        )

        with self.assertRaisesRegex(ValueError, "MCP evidence"):
            release_gate.verify_codex_adoption_report(report)

    def test_new_reports_require_identity_while_v2_evidence_remains_readable(
        self,
    ) -> None:
        def succeed(command, *, cwd):
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, resolved_candidate(
            "HEAD"
        ):
            report = execute_release_gate(
                current_ref="HEAD",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
            )

        self.assertEqual(
            report["schema"],
            "openubmc-agent-workflow.release-gate.v3",
        )
        for field in ("requested_ref", "release_commit", "source_commit"):
            with self.subTest(field=field):
                invalid = dict(report)
                invalid.pop(field)
                invalid["evidence_digest"] = release_gate.evidence_fingerprint(
                    {key: value for key, value in invalid.items() if key != "evidence_digest"}
                )
                with self.assertRaisesRegex(ValueError, "candidate identity"):
                    release_gate.verify_release_gate_report(invalid)

        for field in ("release_commit", "source_commit"):
            with self.subTest(padded_field=field):
                invalid = dict(report)
                invalid[field] = f" {invalid[field]} "
                invalid["evidence_digest"] = release_gate.evidence_fingerprint(
                    {key: value for key, value in invalid.items() if key != "evidence_digest"}
                )
                with self.assertRaisesRegex(ValueError, "candidate identity"):
                    release_gate.verify_release_gate_report(invalid)

        invalid = dict(report)
        invalid.pop("formal_identity")
        invalid["evidence_digest"] = release_gate.evidence_fingerprint(
            {key: value for key, value in invalid.items() if key != "evidence_digest"}
        )
        with self.assertRaisesRegex(ValueError, "formal identity"):
            release_gate.verify_release_gate_report(invalid)

        legacy = dict(report)
        legacy["schema"] = "openubmc-agent-workflow.release-gate.v2"
        legacy.pop("requested_ref")
        legacy.pop("release_commit")
        legacy["evidence_digest"] = release_gate.evidence_fingerprint(
            {key: value for key, value in legacy.items() if key != "evidence_digest"}
        )
        release_gate.verify_release_gate_report(legacy)

    def test_release_workflow_restores_and_requires_execute_ab_evidence(self) -> None:
        workflow = yaml.load(
            (Path.cwd() / ".github" / "workflows" / "release.yml").read_text(
                encoding="utf-8"
            ),
            Loader=yaml.BaseLoader,
        )
        self.assertIsInstance(workflow, dict)
        dispatch = workflow["on"]["workflow_dispatch"]
        self.assertEqual(
            set(dispatch["inputs"]),
            {
                "current_ref",
                "previous_ref",
                "model_identity",
                "codex_identity",
                "ab_bundle_asset",
                "ab_bundle_sha256",
                "promote",
            },
        )
        self.assertEqual(workflow["permissions"]["actions"], "read")
        self.assertEqual(workflow["permissions"]["checks"], "read")
        self.assertEqual(
            workflow["jobs"]["release-gate"]["permissions"],
            {
                "actions": "read",
                "checks": "read",
                "contents": "write",
            },
        )

        steps = {
            step.get("name", step.get("uses", "")): step
            for step in workflow["jobs"]["release-gate"]["steps"]
        }
        self.assertIn("actions/checkout@v7", steps)
        self.assertEqual(
            steps["actions/checkout@v7"]["with"]["ref"],
            "${{ inputs.current_ref == 'HEAD' && github.sha || inputs.current_ref }}",
        )
        self.assertEqual(
            steps["actions/setup-python@v7"]["with"]["python-version"],
            "3.12.13",
        )
        self.assertEqual(
            steps["Upload release evidence"]["uses"],
            "actions/upload-artifact@v7",
        )
        resolve = steps["Resolve release candidate identity"]
        self.assertEqual(resolve["id"], "candidate")
        self.assertIn('git rev-parse "${REQUESTED_REF}^{commit}"', resolve["run"])
        self.assertIn('git tag --points-at "$RELEASE_COMMIT"', resolve["run"])
        self.assertIn(
            'gh release view "$tag" --json isDraft --jq .isDraft',
            resolve["run"],
        )
        self.assertIn("grep -qx true", resolve["run"])
        self.assertIn("release_tag=", resolve["run"])
        self.assertEqual(
            workflow["jobs"]["release-gate"]["outputs"]["release_tag"],
            "${{ steps.candidate.outputs.release_tag }}",
        )
        restore = steps["Restore execute AB qualification evidence"]["run"]
        self.assertIn("gh release download", restore)
        self.assertIn('"$RELEASE_TAG"', restore)
        self.assertNotIn('"$CURRENT_REF"', restore)
        self.assertIn('"$AB_BUNDLE_ASSET"', restore)
        self.assertIn("sha256sum --check --strict", restore)
        self.assertIn("scripts/restore_ab_bundle.py", restore)
        self.assertNotIn("base64 --decode", restore)
        trust_root = steps["Restore AB attestation trust root"]
        self.assertEqual(
            trust_root["env"]["AB_ATTESTATION_PUBLIC_KEY_BASE64"],
            "${{ vars.AB_ATTESTATION_PUBLIC_KEY_BASE64 }}",
        )
        self.assertIn("base64 --decode", trust_root["run"])
        self.assertIn("$RUNNER_TEMP/agent-gateway-ab-attestation.pub", trust_root["run"])
        gate = steps["Run immutable release gates"]["run"]
        self.assertEqual(
            steps["Run immutable release gates"]["env"][
                "FORMAL_MODEL_IDENTITY"
            ],
            "${{ inputs.model_identity }}",
        )
        self.assertEqual(
            steps["Run immutable release gates"]["env"][
                "FORMAL_CODEX_IDENTITY"
            ],
            "${{ inputs.codex_identity }}",
        )
        self.assertIn(
            '--current-ref "${{ inputs.current_ref }}"',
            gate,
        )
        self.assertIn(
            '--release-tag "${{ steps.candidate.outputs.release_tag }}"',
            gate,
        )
        self.assertIn('--model-identity "$FORMAL_MODEL_IDENTITY"', gate)
        self.assertIn('--codex-identity "$FORMAL_CODEX_IDENTITY"', gate)
        self.assertIn(
            '--ab-evidence "$RUNNER_TEMP/agent-gateway-ab-evidence/summary.json"',
            gate,
        )
        self.assertIn(
            '--ab-attestation-public-key "$RUNNER_TEMP/agent-gateway-ab-attestation.pub"',
            gate,
        )
        self.assertIn("--github-repository \"${{ github.repository }}\"", gate)
        self.assertIn('--work-root "$RUNNER_TEMP/release-gate-work"', gate)
        uploaded = set(
            steps["Upload release evidence"]["with"]["path"].splitlines()
        )
        self.assertEqual(
            uploaded,
            {
                "${{ runner.temp }}/agent-gateway-ab-evidence/summary.json",
                "${{ runner.temp }}/agent-gateway-ab-evidence/all_metrics.json",
                "${{ runner.temp }}/agent-gateway-ab-evidence/run_evidence.json",
                "${{ runner.temp }}/agent-gateway-ab-evidence/schedule.json",
                "${{ runner.temp }}/agent-gateway-ab-evidence.tar.xz",
                "${{ runner.temp }}/release-gate.json",
                "${{ runner.temp }}/release-gate-work/github-ci-evidence.json",
                "${{ runner.temp }}/release-gate-work/runtime-qualification.json",
                "${{ runner.temp }}/release-gate-work/codex-adoption-qualification.json",
            },
        )
        self.assertEqual(
            release_gate.execute_release_gate.__kwdefaults__["github_repository"],
            "lihuabai629-star/openubmc-agent-workflow",
        )

        promote_job = workflow["jobs"]["promote"]
        promote_checkout = promote_job["steps"][0]
        self.assertEqual(
            promote_checkout["with"]["ref"],
            "${{ needs.release-gate.outputs.release_tag }}",
        )
        promote_step = promote_job["steps"][-1]
        self.assertEqual(
            promote_step["env"]["RELEASE_TAG"],
            "${{ needs.release-gate.outputs.release_tag }}",
        )
        promote = promote_step["run"]
        self.assertIn('gh release view "$RELEASE_TAG" --json isDraft', promote)
        self.assertIn("grep -qx true", promote)
        self.assertIn('gh release edit "$RELEASE_TAG" --draft=false', promote)
        self.assertNotIn('gh release create "$RELEASE_TAG"', promote)


if __name__ == "__main__":
    unittest.main()
