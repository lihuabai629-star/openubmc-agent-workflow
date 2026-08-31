from __future__ import annotations

import copy
from unittest import mock
import unittest

from scripts import codex_adoption_qualification as adoption
from scripts.evidence_report import evidence_fingerprint


def closeout_report() -> dict[str, object]:
    return {
        "source_commit": "a" * 40,
        "source_clean": True,
        "qualified": True,
        "qualification_digest": "sha256:" + "b" * 64,
        "product_contract": {"status": "passed"},
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
                    "launcher_sha256": "2" * 64,
                    "runtime_invocation": "client-configured-mcp-command",
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
                "summary": {"live_processes": 0, "active_requests": 0},
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
    def test_one_report_combines_identity_codex_runtime_tasks_and_provenance(
        self,
    ) -> None:
        with (
            mock.patch.object(
                adoption, "qualify_closeout", return_value=closeout_report()
            ),
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
                model_identity={
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                },
                codex_identity={
                    "version": "codex-cli 0.150.0",
                    "sha256": "1" * 64,
                },
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
            report = adoption.qualify()

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
            first = adoption.qualify()
            second = adoption.qualify()

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
            report = adoption.qualify()

        self.assertTrue(report["qualified"])
        self.assertEqual(report["failed_dimensions"], [])
        self.assertFalse(report["maintenance_checkpoint_ready"])
        self.assertEqual(
            report["maintenance_checkpoint_blockers"],
            ["evaluation_isolation"],
        )
        self.assertFalse(report["external_evaluation"]["blocking"])
        self.assertFalse(report["release_gate"]["eligible"])

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
            report = adoption.qualify()

        self.assertFalse(report["qualified"])
        self.assertEqual(report["failed_dimensions"], ["codex_mcp"])
        self.assertFalse(report["dimensions"]["codex_mcp"]["identity_bound"])

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
            report = adoption.qualify()

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
                adoption.qualify(source_commit="9" * 40)


if __name__ == "__main__":
    unittest.main()
