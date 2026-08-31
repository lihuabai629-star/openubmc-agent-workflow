from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import continuous_closeout_qualification as qualification


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "continuous_closeout_qualification.py"
MODEL_IDENTITY = {"model": "codex-product-client-qualification"}
CODEX_IDENTITY = {"version": "codex-cli 0.151.0"}


def passed_lifecycle_record() -> dict[str, object]:
    return {
        "schema": "openubmc.mcp-process-lifecycle.v1",
        "component": "target-runtime",
        "version": "openubmc.target-runtime.v1",
        "client": "codex",
        "task_id": "codex-adoption-probe",
        "session_id": "codex-adoption-session",
        "source_commit": "a" * 40,
        "formal_run": True,
        "model_identity": {"model": "codex-product-client-qualification"},
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
        "exit_reason": "client-terminated",
    }


def passed_mcp_closeout() -> dict[str, object]:
    return {
        "status": "passed",
        "task_closeout_ready": True,
        "identity_records_valid": True,
        "isolation_verified": True,
        "restart_verified": True,
        "records": [
            {
                **passed_lifecycle_record(),
                "task_id": "continuous-closeout-qualification",
                "session_id": "continuous-closeout-session",
                "exit_reason": "task-closeout",
            }
        ],
        "summary": {
            "record_count": 2,
            "live_processes": 0,
            "active_requests": 0,
            "confirmed_live_orphans": 0,
            "unattributed_live_processes": 0,
            "owned_live_processes": 0,
            "stopped_processes": 2,
        },
        "closeout_checks": {
            "active_requests_zero": True,
            "confirmed_live_orphans_zero": True,
            "unattributed_live_processes_zero": True,
            "owned_live_processes_zero": True,
        },
        "operator_status": {
            "schema": "openubmc-agent-workflow.mcp-process-status.v1",
            "operation": "status",
            "task_id": "codex-adoption-probe",
            "session_id": "codex-adoption-session",
            "task_closeout_ready": True,
            "summary": {
                "record_count": 2,
                "live_processes": 0,
                "active_requests": 0,
                "confirmed_live_orphans": 0,
                "unattributed_live_processes": 0,
                "owned_live_processes": 0,
                "stopped_processes": 2,
            },
            "closeout_checks": {
                "active_requests_zero": True,
                "confirmed_live_orphans_zero": True,
                "unattributed_live_processes_zero": True,
                "owned_live_processes_zero": True,
            },
        },
        "isolation": {
            "qualification_root": "/isolated",
            "task_home": "/isolated/home",
            "codex_config_root": "/isolated/codex",
            "runtime_state_root": "/isolated/runtime-state",
            "lifecycle_root": "/isolated/mcp-processes",
            "global_codex_state_used": False,
            "installed_launcher_invocation": True,
        },
    }


def passed_product_client_run(name: str) -> dict[str, object]:
    launcher_identity = {
        "schema": "openubmc-agent-workflow.codex-launcher-identity.v1",
        "runtime_api": "openubmc.target-runtime.v1",
        "runtime_content_digest": "sha256:" + "f" * 64,
        "source_commit": "a" * 40,
        "entrypoint": "openubmc-debug/scripts/target_runtime_mcp.py",
    }
    return {
        "status": "passed",
        "client": name,
        "tests": [f"qualification.{name}"],
        "returncode": 0,
        "failure_tail": "",
        "adapter_available": True,
        "support_mode": "skills-and-runtime-mcp",
        "declared_mcp": True,
        "mcp_registration_verified": True,
        "runtime_launcher_verified": True,
        "launcher_state_verified": True,
        "launcher_identity": launcher_identity,
        "launcher_identity_digest": qualification.evidence_fingerprint(
            launcher_identity
        ),
        "runtime_invocation": "installed-runtime-launcher-protocol",
        "protocol_exchange": [
            "initialize",
            "tools/list",
            "tools/call:execute",
        ],
        "tools": ["execute", "observe"],
        "source_commit": "a" * 40,
        "installation": {
            "ok": True,
            "clients": ["codex"],
            "operational_ready": True,
            "release_identity_verified": True,
            "evaluation_ready": True,
            "source": {
                "mode": "managed",
                "ref_kind": "commit",
                "requested_ref": "3" * 40,
                "resolved_commit": "3" * 40,
                "current_commit": "3" * 40,
                "dirty": False,
            },
            "release": {
                "schema": "openubmc-agent-workflow.release-lock.v1",
                "immutable": True,
                "verified": True,
                "trust_mode": "verified-immutable-source",
                "release_version": "2.0.2",
                "source_commit": "a" * 40,
                "lock_digest": "sha256:" + "c" * 64,
                "source_tree_digest": "sha256:" + "d" * 64,
                "workflow_digest": "sha256:" + "e" * 64,
                "skill_digests": {
                    "openubmc-debug": "sha256:" + "1" * 64,
                },
                "runtime": {
                    "api_version": "openubmc.target-runtime.v1",
                    "content_digest": "sha256:" + "f" * 64,
                },
            },
        },
        "runtime_api": "openubmc.target-runtime.v1",
        "runtime_content_digest": "sha256:" + "f" * 64,
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
        "codex_process_invocation": True,
        "codex_process_runs": [
            {
                "process_id": 123,
                "process_identity": "parent-identity",
                "parent_pid": 123,
                "parent_identity": "parent-identity",
                "executable": "/isolated/codex",
                "executable_sha256": "sha256:" + "9" * 64,
                "version": "codex-cli 0.151.0",
                "requested_model": "codex-product-client-qualification",
                "captured_request_models": [
                    "codex-product-client-qualification"
                ],
                "returncode": 0,
            },
            {
                "process_id": 124,
                "process_identity": "parent-identity-2",
                "parent_pid": 124,
                "parent_identity": "parent-identity-2",
                "executable": "/isolated/codex",
                "executable_sha256": "sha256:" + "9" * 64,
                "version": "codex-cli 0.151.0",
                "requested_model": "codex-product-client-qualification",
                "captured_request_models": [
                    "codex-product-client-qualification"
                ],
                "returncode": 0,
            },
        ],
        "captured_model_tools": [
            "mcp__openubmc_target_runtime",
        ],
        "captured_runtime_tool_contracts": [
            {
                "name": "mcp__openubmc_target_runtime",
                "type": "namespace",
                "tools": [
                    {"name": "execute"},
                    {"name": "observe"},
                ],
            }
        ],
        "restart_verified": True,
        "mcp_closeout": passed_mcp_closeout(),
        "mcp_lifecycle_records": [
            passed_lifecycle_record(),
            {
                **passed_lifecycle_record(),
                "parent_pid": 124,
                "parent_identity": "parent-identity-2",
                "process_id": 457,
            },
        ],
    }


class ContinuousCloseoutQualificationTests(unittest.TestCase):
    def test_mcp_closeout_binds_requested_formal_identities(self) -> None:
        report = qualification._mcp_closeout_snapshot(
            passed_product_client_run("codex"),
            source_commit="a" * 40,
            model_identity={"model": "codex-product-client-qualification"},
            codex_identity=CODEX_IDENTITY,
        )

        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(
            report["records"][0]["model_identity"],
            {"model": "codex-product-client-qualification"},
        )
        self.assertEqual(
            report["records"][0]["codex_identity"],
            CODEX_IDENTITY,
        )

    def test_candidate_release_commit_is_deterministic(self) -> None:
        source_commit = qualification.resolve_source_commit(ROOT)
        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
        ):
            first = qualification._prepare_candidate_release(
                Path(first_raw), source_commit
            )
            second = qualification._prepare_candidate_release(
                Path(second_raw), source_commit
            )

        self.assertEqual(first.release_commit, second.release_commit)
        self.assertEqual(first.release, second.release)

    def test_candidate_release_does_not_depend_on_github_remote_name(self) -> None:
        with (
            tempfile.TemporaryDirectory() as clone_raw,
            tempfile.TemporaryDirectory() as qualification_raw,
        ):
            isolated = Path(clone_raw) / "repository"
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(ROOT), str(isolated)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            github_refs = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/github"],
                cwd=isolated,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            self.assertEqual(github_refs, "")
            tree = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=isolated,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Qualification Test",
                "GIT_AUTHOR_EMAIL": "qualification@example.invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_NAME": "Qualification Test",
                "GIT_COMMITTER_EMAIL": "qualification@example.invalid",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            }
            historical_commit = subprocess.run(
                ["git", "commit-tree", tree],
                cwd=isolated,
                check=True,
                input="remote-only historical evidence\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=commit_environment,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/historical-evidence",
                    historical_commit,
                ],
                cwd=isolated,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            source_commit = qualification.resolve_source_commit(isolated)

            with mock.patch.object(qualification, "ROOT", isolated):
                candidate = qualification._prepare_candidate_release(
                    Path(qualification_raw),
                    source_commit,
                )
            bundle_heads = subprocess.run(
                ["git", "bundle", "list-heads", str(candidate.bundle)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout

        self.assertEqual(candidate.release["source_commit"], source_commit)
        self.assertIn(historical_commit, bundle_heads)

    def test_qualification_integrates_product_client_isolation_lifecycle_and_projection(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--model-identity",
                json.dumps(MODEL_IDENTITY),
                "--codex-identity",
                json.dumps(CODEX_IDENTITY),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(
            report["schema"],
            "openubmc-agent-workflow.continuous-closeout-qualification.v1",
        )
        self.assertTrue(report["qualified"])
        self.assertEqual(
            report["client_matrix"]["product_clients"],
            ["codex"],
        )
        self.assertEqual(
            report["client_matrix"]["evaluation_harnesses"], ["dsh"]
        )
        self.assertEqual(report["client_matrix"]["overlap"], [])
        self.assertEqual(
            sorted(report["client_matrix"]["runs"]),
            ["codex"],
        )
        self.assertTrue(
            all(
                run["status"] == "passed"
                for run in report["client_matrix"]["runs"].values()
            )
        )
        run = report["client_matrix"]["runs"]["codex"]
        self.assertTrue(run["adapter_available"])
        self.assertEqual(run["support_mode"], "skills-and-runtime-mcp")
        self.assertTrue(run["declared_mcp"])
        self.assertTrue(run["mcp_registration_verified"])
        self.assertEqual(
            run["runtime_invocation"], "installed-runtime-launcher-protocol"
        )
        self.assertTrue(run["codex_process_invocation"])
        self.assertEqual(len(run["codex_process_runs"]), 2)
        self.assertTrue(
            all(item["version"] == "codex-cli 0.151.0" for item in run["codex_process_runs"])
        )
        self.assertEqual(
            {item["process_id"] for item in run["codex_process_runs"]},
            {item["parent_pid"] for item in run["mcp_lifecycle_records"]},
        )
        runtime_contract = run["captured_runtime_tool_contracts"][0]
        self.assertEqual(runtime_contract["name"], "mcp__openubmc_target_runtime")
        self.assertEqual(
            {tool["name"] for tool in runtime_contract["tools"]},
            {"execute", "observe"},
        )
        self.assertEqual(
            run["protocol_exchange"],
            ["initialize", "tools/list", "tools/call:execute"],
        )
        self.assertEqual(
            run["workflow_exchange"]["state"], "preflight_failed"
        )
        self.assertEqual(run["tools"], ["execute", "observe"])
        self.assertTrue(run["installation"]["evaluation_ready"])
        self.assertTrue(run["installation"]["release_identity_verified"])
        self.assertEqual(
            run["installation"]["release"]["trust_mode"],
            "verified-immutable-source",
        )
        self.assertTrue(
            any("codex_product_client" in item for item in run["tests"])
        )
        self.assertTrue(report["evaluation_isolation"]["global_state_blocked"])
        self.assertTrue(report["evaluation_isolation"]["task_owned"])
        self.assertTrue(report["mcp_lifecycle"]["parent_loss_covered"])
        self.assertTrue(report["mcp_lifecycle"]["active_request_drain_covered"])
        self.assertTrue(report["mcp_lifecycle"]["cleanup_covered"])
        self.assertTrue(report["mcp_lifecycle"]["zero_live_orphans_covered"])
        self.assertTrue(report["mcp_lifecycle"]["restart_closeout_covered"])
        closeout = report["mcp_lifecycle"]["closeout"]
        self.assertTrue(closeout["task_closeout_ready"])
        self.assertTrue(closeout["identity_records_valid"])
        self.assertTrue(closeout["isolation_verified"])
        self.assertEqual(closeout["summary"]["live_processes"], 0)
        self.assertEqual(closeout["summary"]["active_requests"], 0)
        self.assertEqual(closeout["summary"]["confirmed_live_orphans"], 0)
        self.assertEqual(closeout["summary"]["unattributed_live_processes"], 0)
        self.assertEqual(closeout["summary"]["owned_live_processes"], 0)
        self.assertEqual(closeout["summary"]["stopped_processes"], 2)
        self.assertTrue(all(closeout["closeout_checks"].values()))
        self.assertEqual(closeout["task_ids"], ["codex-adoption-probe"])
        self.assertEqual(closeout["session_ids"], ["codex-adoption-session"])
        self.assertEqual(closeout["records"][0]["client"], "codex")
        self.assertEqual(closeout["records"][0]["source_commit"], report["source_commit"])
        self.assertEqual(closeout["records"][0]["exit_reason"], "client-terminated")
        self.assertTrue(closeout["records"][0]["parent_identity_verified"])
        self.assertTrue(closeout["records"][0]["model_identity"])
        self.assertTrue(closeout["records"][0]["codex_identity"])
        self.assertFalse(closeout["isolation"]["global_codex_state_used"])
        self.assertNotEqual(
            closeout["isolation"]["task_home"],
            str(Path.home()),
        )
        projection = report["execute_projection"]
        self.assertTrue(projection["correctness_primary"])
        self.assertTrue(projection["operator_projection_covered"])
        self.assertTrue(projection["repeated_reference"])
        self.assertGreater(projection["saved_bytes"], 0)
        self.assertFalse(projection["blocks_promotability"])
        self.assertIn(
            "fresh_runtime_product_evidence_required",
            report["external_blockers"],
        )
        self.assertTrue(report["qualification_digest"].startswith("sha256:"))

    def test_qualification_ignores_target_and_credential_environment(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                "OPENUBMC_TARGET": "198.51.100.99",
                "OPENUBMC_SSH_PASSWORD": "must-not-be-used",
                "OPENUBMC_REDFISH_PASSWORD": "must-not-be-used",
                },
            ),
            mock.patch.object(
                qualification,
                "_workflow_metadata",
                return_value={
                    "clients": {
                        "codex": {
                            "role": "supported-product-client",
                            "mcp": True,
                        }
                    },
                    "evaluation_harnesses": {},
                },
            ),
            mock.patch.object(
                qualification,
                "_run_tests",
                return_value={"status": "passed", "tests": [], "returncode": 0},
            ),
            mock.patch.object(
                qualification,
                "_product_client_run",
                side_effect=lambda name, tests, contract, source_commit, **_kwargs: passed_product_client_run(name),
            ),
            mock.patch.object(
                qualification,
                "_mcp_closeout_snapshot",
                return_value=passed_mcp_closeout(),
            ),
            mock.patch.object(
                qualification,
                "qualify_dual_projection",
                return_value={
                    "status": "passed",
                    "correctness": {"passed": True},
                    "representative_receipt": {
                        "repeated_projection": {
                            "repeated_reference": True,
                            "full_bytes": 2,
                            "reference_bytes": 1,
                            "saved_bytes": 1,
                        }
                    },
                },
            ),
            mock.patch.object(qualification, "_source_clean", return_value=True),
            mock.patch.object(
                qualification,
                "resolve_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualification.qualify(
                model_identity=MODEL_IDENTITY,
                codex_identity=CODEX_IDENTITY,
            )

        self.assertTrue(report["qualified"])
        self.assertEqual(report["client_matrix"]["status"], "passed")
        self.assertEqual(report["client_matrix"]["evaluation_harnesses"], [])
        run = report["client_matrix"]["runs"]["codex"]
        self.assertEqual(run["client"], "codex")
        self.assertTrue(run["adapter_available"])
        self.assertTrue(run["declared_mcp"])
        self.assertTrue(run["runtime_launcher_verified"])
        self.assertEqual(
            run["runtime_invocation"], "installed-runtime-launcher-protocol"
        )
        self.assertEqual(run["tools"], ["execute", "observe"])
        self.assertTrue(report["task_matrix"]["correctness_primary"])
        self.assertEqual(
            sorted(report["task_matrix"]["groups"]),
            [
                "build_upgrade",
                "dependency_blocked",
                "hardware_blocked",
                "live_patch",
                "restart_crash",
                "source_only",
                "wide_observe",
            ],
        )
        self.assertTrue(
            all(
                group["status"] == "passed"
                for group in report["task_matrix"]["groups"].values()
            )
        )
        self.assertTrue(report["task_matrix"]["completion_primary"])
        for group in report["task_matrix"]["groups"].values():
            self.assertEqual(group["completion"]["status"], "passed")
            self.assertEqual(group["correctness"]["status"], "passed")
            self.assertEqual(group["terminal_contract"]["status"], "passed")
            dimension_tests = {
                tuple(group[dimension]["tests"])
                for dimension in ("completion", "correctness", "terminal_contract")
            }
            self.assertEqual(len(dimension_tests), 3)
        self.assertIn(
            "test_source_only_keeps_dependency_and_nvme_coverage_gaps_visible",
            " ".join(report["task_matrix"]["groups"]["hardware_blocked"]["tests"]),
        )
        self.assertNotIn(
            "test_completed_failed_build_does_not_advance_to_upgrade",
            " ".join(report["task_matrix"]["groups"]["hardware_blocked"]["tests"]),
        )
        self.assertNotIn("198.51.100.99", json.dumps(report))
        self.assertNotIn("must-not-be-used", json.dumps(report))

    def test_qualification_accepts_trusted_product_ingestion_input(self) -> None:
        descriptor = Path("fresh-product-ingestion.json")
        runtime_repository = Path("runtime.sqlite3")
        with (
            mock.patch.object(
                qualification,
                "_workflow_metadata",
                return_value={
                    "clients": {
                        "codex": {
                            "role": "supported-product-client",
                            "mcp": True,
                        }
                    },
                    "evaluation_harnesses": {
                        "dsh": {"role": "evaluation-harness"}
                    },
                },
            ),
            mock.patch.object(
                qualification,
                "_run_tests",
                return_value={"status": "passed", "tests": [], "returncode": 0},
            ),
            mock.patch.object(
                qualification,
                "_product_client_run",
                side_effect=lambda name, tests, contract, source_commit, **_kwargs: passed_product_client_run(name),
            ),
            mock.patch.object(
                qualification,
                "_mcp_closeout_snapshot",
                return_value=passed_mcp_closeout(),
            ),
            mock.patch.object(
                qualification,
                "qualify_dual_projection",
                return_value={
                    "status": "passed",
                    "correctness": {"passed": True},
                    "representative_receipt": {
                        "repeated_projection": {
                            "repeated_reference": True,
                            "full_bytes": 2,
                            "reference_bytes": 1,
                            "saved_bytes": 1,
                        }
                    },
                },
            ),
            mock.patch.object(qualification, "_source_clean", return_value=True),
            mock.patch.object(
                qualification,
                "resolve_source_commit",
                return_value="a" * 40,
            ),
            mock.patch.object(
                qualification,
                "load_product_ingestion",
                return_value={"schema": "ingestion"},
            ) as load_ingestion,
            mock.patch.object(
                qualification,
                "assemble_product_manifest",
                return_value={"schema": "assembled-manifest"},
            ) as assemble,
            mock.patch.object(
                qualification,
                "qualify_product_closeout",
                return_value={
                    "qualified": True,
                    "promotable": True,
                    "claim_level": "fresh-runtime-product-closed",
                    "manifest_digest": "sha256:" + "b" * 64,
                    "evidence_digest": "sha256:" + "c" * 64,
                    "gaps": [],
                    "violations": [],
                },
            ),
        ):
            report = qualification.qualify(
                product_ingestion=descriptor,
                runtime_repository=runtime_repository,
                model_identity=MODEL_IDENTITY,
                codex_identity=CODEX_IDENTITY,
            )

        load_ingestion.assert_called_once_with(descriptor)
        assemble.assert_called_once_with(
            {"schema": "ingestion"},
            runtime_repository=runtime_repository,
        )
        self.assertEqual(
            report["product_evidence"]["status"], "verified-ingestion"
        )
        self.assertTrue(report["fresh_product_promotable"])
        self.assertEqual(report["external_blockers"], [])

    def test_qualification_requires_explicit_formal_identity(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "formal model and Codex identity are required",
        ):
            qualification.qualify()


if __name__ == "__main__":
    unittest.main()
