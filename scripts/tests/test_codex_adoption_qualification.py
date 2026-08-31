from __future__ import annotations

import copy
from unittest import mock
import unittest

from scripts import codex_adoption_qualification as adoption
from scripts.evidence_report import evidence_fingerprint


FORMAL_MODEL_IDENTITY = {"model": "codex-product-client-qualification"}
FORMAL_CODEX_IDENTITY = {
    "client_info_name": "codex-adoption-qualification",
    "client_info_version": "1",
}


def qualify(**kwargs: object) -> dict[str, object]:
    return adoption.qualify(
        model_identity=FORMAL_MODEL_IDENTITY,
        codex_identity=FORMAL_CODEX_IDENTITY,
        **kwargs,
    )


def closeout_report() -> dict[str, object]:
    launcher_identity = {
        "schema": "openubmc-agent-workflow.codex-launcher-identity.v1",
        "runtime_api": "openubmc.target-runtime.v1",
        "runtime_content_digest": "sha256:" + "f" * 64,
        "source_commit": "a" * 40,
        "entrypoint": "openubmc-debug/scripts/target_runtime_mcp.py",
    }
    return {
        "source_commit": "a" * 40,
        "source_clean": True,
        "qualified": True,
        "qualification_digest": "sha256:" + "b" * 64,
        "product_contract": {
            "status": "passed",
            "returncode": 0,
            "tests": ["qualification.product"],
        },
        "client_matrix": {
            "status": "passed",
            "product_clients": ["codex"],
            "evaluation_harnesses": ["dsh"],
            "overlap": [],
            "runs": {
                "codex": {
                    "status": "passed",
                    "client": "codex",
                    "adapter_available": True,
                    "support_mode": "skills-and-runtime-mcp",
                    "declared_mcp": True,
                    "mcp_registration_verified": True,
                    "runtime_launcher_verified": True,
                    "launcher_state_verified": True,
                    "launcher_identity": launcher_identity,
                    "launcher_identity_digest": evidence_fingerprint(
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
                            "installed_launcher_invocation": True,
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
                            "source_commit": "a" * 40,
                            "formal_run": True,
                            "model_identity": {
                                "model": "codex-product-client-qualification"
                            },
                            "codex_identity": {
                                "client_info_name": "codex-adoption-qualification",
                                "client_info_version": "1",
                            },
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
                            "source_commit": "a" * 40,
                            "formal_run": True,
                            "model_identity": FORMAL_MODEL_IDENTITY,
                            "codex_identity": FORMAL_CODEX_IDENTITY,
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
                }
            },
        },
        "evaluation_isolation": {
            "status": "passed",
            "global_state_blocked": True,
            "task_owned": True,
        },
        "mcp_lifecycle": {
            "status": "passed",
            "closeout": {
                "status": "passed",
                "task_closeout_ready": True,
                "identity_records_valid": True,
                "isolation_verified": True,
                "summary": {
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
            },
        },
        "execute_projection": {
            "status": "passed",
            "correctness_primary": True,
            "repeated_reference": True,
            "full_bytes": 15516,
            "reference_bytes": 1035,
            "saved_bytes": 14481,
            "blocks_promotability": False,
            "operator_projection_covered": True,
        },
        "task_matrix": {
            "status": "passed",
            "correctness_primary": True,
            "completion_primary": True,
            "terminal_contract_primary": True,
            "groups": {"source_only": {"status": "passed"}},
        },
    }


def release_identity() -> dict[str, object]:
    return {
        "release_version": "2.0.2",
        "source_commit": "a" * 40,
        "lock_digest": "sha256:" + "c" * 64,
        "source_tree_digest": "sha256:" + "d" * 64,
        "workflow_digest": "sha256:" + "e" * 64,
        "compatibility": {"clients": {"codex": {"mcp": True}}},
        "runtime": {
            "api_version": "openubmc.target-runtime.v1",
            "content_digest": "sha256:" + "f" * 64,
        },
        "skills": [
            {
                "name": "openubmc-debug",
                "digest": "sha256:" + "1" * 64,
            }
        ],
    }


class CodexAdoptionQualificationTests(unittest.TestCase):
    def test_qualification_requires_explicit_formal_identities(self) -> None:
        with self.assertRaisesRegex(ValueError, "formal model and Codex identity"):
            adoption.qualify()

    def test_one_report_combines_identity_codex_runtime_tasks_and_provenance(
        self,
    ) -> None:
        model_identity = {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        }
        codex_identity = {
            "version": "codex-cli 0.150.0",
            "sha256": "1" * 64,
        }
        closeout = closeout_report()
        for lifecycle in closeout["client_matrix"]["runs"]["codex"][
            "mcp_lifecycle_records"
        ]:
            lifecycle["model_identity"] = model_identity
            lifecycle["codex_identity"] = codex_identity
        with (
            mock.patch.object(
                adoption, "qualify_closeout", return_value=closeout
            ) as qualify_closeout,
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = adoption.qualify(
                model_identity=model_identity,
                codex_identity=codex_identity,
            )

        qualify_closeout.assert_called_once_with(
            source_commit="a" * 40,
            model_identity=model_identity,
            codex_identity=codex_identity,
        )

        self.assertEqual(
            report["schema"],
            "openubmc-agent-workflow.codex-adoption-qualification.v1",
        )
        self.assertTrue(report["qualified"])
        self.assertTrue(report["maintenance_checkpoint_ready"])
        self.assertTrue(report["release_gate"]["eligible"])
        self.assertEqual(report["failed_dimensions"], [])
        self.assertEqual(
            report["dimensions"]["installation_identity"]["clients"],
            ["codex"],
        )
        self.assertEqual(
            report["dimensions"]["codex_mcp"]["tools"],
            ["execute", "observe"],
        )
        self.assertEqual(
            report["dimensions"]["codex_mcp"]["protocol_exchange"],
            ["initialize", "tools/list", "tools/call:execute"],
        )
        self.assertTrue(report["dimensions"]["codex_mcp"]["identity_bound"])
        self.assertEqual(
            report["dimensions"]["codex_mcp"]["launcher_identity_digest"],
            evidence_fingerprint(
                report["dimensions"]["codex_mcp"]["launcher_identity"]
            ),
        )
        self.assertNotIn(
            "launcher_sha256", report["dimensions"]["codex_mcp"]
        )
        self.assertEqual(
            report["dimensions"]["codex_mcp"]["workflow_exchange"]["state"],
            "preflight_failed",
        )
        self.assertEqual(
            report["dimensions"]["installation_identity"]["runtime_api"],
            "openubmc.target-runtime.v1",
        )
        self.assertEqual(
            report["provenance"]["model"]["model"], "gpt-5.6-sol"
        )
        self.assertEqual(
            report["provenance"]["codex"]["version"],
            "codex-cli 0.150.0",
        )
        self.assertFalse(report["external_evaluation"]["blocking"])
        self.assertEqual(report["external_evaluation"]["harnesses"], ["dsh"])
        unsigned = dict(report)
        digest = unsigned.pop("evidence_digest")
        self.assertEqual(digest, evidence_fingerprint(unsigned))

    def test_lifecycle_identity_must_match_report_provenance(self) -> None:
        closeout = closeout_report()
        closeout["client_matrix"]["runs"]["codex"]["mcp_lifecycle_records"][0][
            "model_identity"
        ] = {"model": "different-model"}
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(adoption, "bind_source_commit", return_value="a" * 40),
        ):
            report = adoption.qualify(
                model_identity={"model": "gpt-5.6-sol"},
                codex_identity={"version": "codex-cli 0.150.0"},
            )

        self.assertFalse(report["qualified"])
        self.assertIn(
            "model_identity_mismatch",
            report["dimensions"]["codex_mcp"]["failure_codes"],
        )

    def test_failed_dimension_is_explicit_and_checkpoint_cannot_pass(self) -> None:
        closeout = closeout_report()
        closeout["task_matrix"] = {
            **closeout["task_matrix"],
            "status": "failed",
            "completion_primary": False,
        }
        closeout["client_matrix"]["runs"]["codex"]["tools"] = [
            "execute",
            "observe",
            "raw_runtime_tool",
        ]
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertFalse(report["maintenance_checkpoint_ready"])
        self.assertEqual(
            report["failed_dimensions"], ["codex_mcp", "task_matrix"]
        )
        self.assertEqual(report["dimensions"]["codex_mcp"]["status"], "failed")
        self.assertEqual(report["dimensions"]["task_matrix"]["status"], "failed")

    def test_report_is_deterministic_for_the_same_inputs(self) -> None:
        with (
            mock.patch.object(
                adoption,
                "qualify_closeout",
                side_effect=[copy.deepcopy(closeout_report()), copy.deepcopy(closeout_report())],
            ),
            mock.patch.object(
                adoption,
                "build_release_lock",
                side_effect=[copy.deepcopy(release_identity()), copy.deepcopy(release_identity())],
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                side_effect=["a" * 40, "a" * 40],
            ),
        ):
            first = qualify()
            second = qualify()

        self.assertEqual(first, second)

    def test_external_harness_metadata_does_not_block_product_qualification(self) -> None:
        closeout = closeout_report()
        closeout["client_matrix"]["evaluation_harnesses"] = []
        closeout["evaluation_isolation"] = {
            "status": "failed",
            "global_state_blocked": False,
            "task_owned": False,
        }
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertTrue(report["qualified"])
        self.assertEqual(report["failed_dimensions"], [])
        self.assertTrue(report["maintenance_checkpoint_ready"])
        self.assertEqual(report["maintenance_checkpoint_blockers"], [])
        self.assertFalse(report["external_evaluation"]["blocking"])
        self.assertFalse(
            report["external_evaluation"]["required_for_maintenance_checkpoint"]
        )
        self.assertTrue(report["release_gate"]["eligible"])

    def test_installed_launcher_identity_mismatch_fails_codex_dimension(self) -> None:
        closeout = closeout_report()
        closeout["client_matrix"]["runs"]["codex"]["runtime_content_digest"] = (
            "sha256:" + "9" * 64
        )
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertEqual(report["failed_dimensions"], ["codex_mcp"])
        self.assertFalse(report["dimensions"]["codex_mcp"]["identity_bound"])

    def test_missing_codex_lifecycle_identity_fails_mcp_dimension(self) -> None:
        closeout = closeout_report()
        closeout["client_matrix"]["runs"]["codex"].pop(
            "mcp_lifecycle_records"
        )
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertIn(
            "mcp_lifecycle_records_missing",
            report["dimensions"]["codex_mcp"]["failure_codes"],
        )

    def test_non_formal_lifecycle_identity_fails_mcp_dimension(self) -> None:
        closeout = closeout_report()
        closeout["client_matrix"]["runs"]["codex"]["mcp_lifecycle_records"][0][
            "formal_run"
        ] = False
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertIn(
            "mcp_lifecycle_identity_invalid",
            report["dimensions"]["codex_mcp"]["failure_codes"],
        )

    def test_missing_restart_closeout_evidence_fails_mcp_dimension(self) -> None:
        closeout = closeout_report()
        codex = closeout["client_matrix"]["runs"]["codex"]
        codex["restart_verified"] = False
        codex["mcp_closeout"]["isolation"]["global_codex_state_used"] = True
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        failures = report["dimensions"]["codex_mcp"]["failure_codes"]
        self.assertIn("restart_unverified", failures)
        self.assertIn("mcp_closeout_invalid", failures)

    def test_missing_owned_process_zero_proof_fails_lifecycle_dimension(self) -> None:
        closeout = closeout_report()
        closeout["mcp_lifecycle"]["closeout"]["summary"].pop(
            "owned_live_processes"
        )
        closeout["mcp_lifecycle"]["closeout"]["closeout_checks"].pop(
            "owned_live_processes_zero"
        )
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertEqual(report["dimensions"]["lifecycle"]["status"], "failed")

    def test_operator_projection_coverage_is_required(self) -> None:
        closeout = closeout_report()
        closeout["execute_projection"]["operator_projection_covered"] = False
        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertEqual(report["dimensions"]["projection"]["status"], "failed")

    def test_linked_development_install_cannot_pass_release_qualification(
        self,
    ) -> None:
        closeout = closeout_report()
        installation = closeout["client_matrix"]["runs"]["codex"][
            "installation"
        ]
        installation["release_identity_verified"] = False
        installation["evaluation_ready"] = False
        installation["source"]["mode"] = "linked"
        installation["source"]["ref_kind"] = "linked"
        installation["release"]["immutable"] = False
        installation["release"]["verified"] = False
        installation["release"]["trust_mode"] = "linked-development"

        with (
            mock.patch.object(adoption, "qualify_closeout", return_value=closeout),
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualify()

        self.assertFalse(report["qualified"])
        self.assertFalse(report["maintenance_checkpoint_ready"])
        self.assertEqual(report["failed_dimensions"], ["installation_identity"])
        self.assertEqual(
            report["dimensions"]["installation_identity"]["status"],
            "failed",
        )

    def test_requested_source_commit_is_verified_before_qualification(self) -> None:
        with mock.patch.object(
            adoption,
            "bind_source_commit",
            side_effect=ValueError(
                "source commit must match workspace HEAD or the release-lock parent"
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must match workspace HEAD"):
                qualify(source_commit="9" * 40)

    def test_selected_source_commit_is_propagated_to_the_collector(self) -> None:
        with (
            mock.patch.object(
                adoption, "qualify_closeout", return_value=closeout_report()
            ) as closeout,
            mock.patch.object(
                adoption, "build_release_lock", return_value=release_identity()
            ),
            mock.patch.object(
                adoption,
                "bind_source_commit",
                return_value="a" * 40,
            ),
        ):
            qualify(source_commit="a" * 40)

        closeout.assert_called_once_with(
            source_commit="a" * 40,
            model_identity={"model": "codex-product-client-qualification"},
            codex_identity={
                "client_info_name": "codex-adoption-qualification",
                "client_info_version": "1",
            },
        )


if __name__ == "__main__":
    unittest.main()
