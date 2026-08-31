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
                    "runtime_invocation": "client-configured-mcp-command",
                    "protocol_exchange": ["initialize", "tools/list"],
                    "tools": ["execute", "observe"],
                }
            },
        },
        "evaluation_isolation": {
            "status": "failed",
            "global_state_blocked": False,
            "task_owned": False,
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
            ["initialize", "tools/list"],
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
        ):
            first = adoption.qualify()
            second = adoption.qualify()

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
