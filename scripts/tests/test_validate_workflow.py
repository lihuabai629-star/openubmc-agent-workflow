#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "scripts" / "validate_workflow.py"
SPEC = importlib.util.spec_from_file_location("openubmc_workflow_validator", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class WorkflowManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        skill = self.root / "example"
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: example-skill\n"
            "description: Example release contract fixture.\n"
            "---\n",
            encoding="utf-8",
        )
        (skill / "agents" / "openai.yaml").write_text(
            "interface:\n"
            "  display_name: 'Example'\n"
            "  short_description: 'Example fixture'\n"
            "  default_prompt: 'Use $example-skill.'\n",
            encoding="utf-8",
        )
        (self.root / "workflow.json").write_text(
            json.dumps(
                {
                    "skills": [
                        {"name": "example-skill", "path": "example"},
                    ],
                    "profiles": {
                        "full": ["example-skill"],
                        "target-runtime": ["example-skill"],
                    },
                }
            ),
            encoding="utf-8",
        )
        installer = self.root / "openubmc-environment-setup" / "scripts"
        installer.mkdir(parents=True)
        (installer / "install_environment.py").write_text(
            "SKILL_BUNDLE = ((\"example-skill\", \"example\"),)\n"
            "TARGET_RUNTIME_SKILL_NAMES = frozenset({\"example-skill\"})\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, *files: str) -> None:
        (self.root / "example" / "skill.json").write_text(
            json.dumps(
                {
                    "manifestVersion": 1,
                    "name": "example-skill",
                    "files": [
                        "SKILL.md",
                        "skill.json",
                        "agents/openai.yaml",
                        *files,
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_every_installable_skill_requires_a_manifest(self) -> None:
        with (
            mock.patch.object(validator, "ROOT", self.root),
            self.assertRaisesRegex(
                SystemExit,
                r"missing skill\.json: example",
            ),
        ):
            validator.validate_manifest()

    def test_manifest_rejects_an_unlisted_runtime_file(self) -> None:
        self.write_manifest()
        scripts = self.root / "example" / "scripts"
        scripts.mkdir()
        (scripts / "required_helper.py").write_text(
            "print('required')\n",
            encoding="utf-8",
        )

        with (
            mock.patch.object(validator, "ROOT", self.root),
            self.assertRaisesRegex(
                SystemExit,
                r"skill\.json omits package files \(scripts/required_helper\.py\): example/skill\.json",
            ),
        ):
            validator.validate_manifest()

    def test_manifest_rejects_duplicate_and_out_of_root_entries(self) -> None:
        for extra, message in (
            ("SKILL.md", "duplicate skill.json files entry"),
            ("../workflow.json", "invalid skill.json file path"),
        ):
            with self.subTest(extra=extra):
                self.write_manifest(extra)
                with (
                    mock.patch.object(validator, "ROOT", self.root),
                    self.assertRaisesRegex(SystemExit, message),
                ):
                    validator.validate_manifest()


class WorkflowStageReportingTests(unittest.TestCase):
    def test_full_validation_labels_each_python_root_and_node_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "alpha/tests/test_alpha.py",
                "beta/tests/test_beta.py",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            (root / "openubmc-kb-mcp").mkdir()

            with (
                mock.patch.object(validator, "ROOT", root),
                mock.patch.object(validator, "validate_manifest", return_value={}),
                mock.patch.object(validator, "validate_release_metadata"),
                mock.patch.object(validator, "run") as run,
            ):
                self.assertEqual(validator.main([]), 0)

        self.assertEqual(
            [call.kwargs["stage"] for call in run.call_args_list],
            [
                "Python compile",
                "Node dependencies: openubmc-kb-mcp",
                "Python tests: alpha/tests",
                "Python tests: beta/tests",
                "Node tests: openubmc-kb-mcp",
                "Node syntax: openubmc-kb-mcp",
            ],
        )


class RoadmapCloseoutValidationTests(unittest.TestCase):
    def write_completed_fixture(self, root: Path) -> None:
        process = [
            "isolated-worktree",
            "tdd",
            "standards-review",
            "spec-review",
            "github-ci",
            "merged",
        ]
        merge_commit = "a" * 40
        batches = []
        for index, batch_id in enumerate(sorted(validator.ROADMAP_BATCH_IDS), start=1):
            batches.append(
                {
                    "id": batch_id,
                    "issues": [index],
                    "pull_requests": [index],
                    "merge_commits": [
                        merge_commit
                        if batch_id == "compatibility-retirement"
                        else f"{index}" * 40
                    ],
                    "test_seams": ["tests/public_seam.py"],
                    "ci_runs": [{"id": index, "conclusion": "success"}],
                    "delivery_process": process,
                    **(
                        {"qualified_source_commit": "b" * 40}
                        if batch_id == "compatibility-retirement"
                        else {}
                    ),
                }
            )
        evidence = {
            "schema": "openubmc-agent-workflow.roadmap-completion.v1",
            "status": "completed",
            "closeout_issue": 70,
            "release": {
                "identity_model": "source-plus-lock-only-commit",
                "mutable_main_policy": "historical-lock-snapshot",
                "tag_created": False,
                "github_release_created": False,
            },
            "canonical_main": {
                "merge_commit": merge_commit,
                "ci_run": {"id": 1, "conclusion": "success"},
            },
            "qualification": {
                "release_version": "2.0.0",
                "source_commit": "b" * 40,
                "lock_only_commit": "c" * 40,
                "release_lock_digest": "sha256:" + "d" * 64,
                "source_tree_digest": "sha256:" + "e" * 64,
                "lock_topology": {
                    "parent_source_commit": "b" * 40,
                    "changed_files": ["release-lock.json"],
                },
                "execute_ab": {
                    "valid_pairs": 10,
                    "invalid_pairs": 0,
                    "decision": "passed",
                    "evidence_digest": "sha256:" + "f" * 64,
                },
                "release_gate": {
                    "promotable": True,
                    "passed_gates": 13,
                    "total_gates": 13,
                    "evidence_digest": "sha256:" + "0" * 64,
                },
                "compatibility_retirement": {
                    "writers_ready": {name: True for name in validator.ROADMAP_WRITERS},
                    "profile_ready": True,
                    "historical_telemetry": "preserved-read-only",
                    "old_event_upcasters": "preserved-read-only",
                },
            },
            "continuous_qualification": {
                "schema": "openubmc-agent-workflow.p2-lifecycle-qualification.v1",
                "issue": 79,
                "source_commit": "9" * 40,
                "promotable": True,
                "evidence_digest": "sha256:" + "1" * 64,
                "runtime_stability_digest": "sha256:" + "2" * 64,
                "qualification_groups": {
                    "persisted_run_compatibility": 9,
                    "semantic_projection_completion": 15,
                },
                "agent_projection_policy": {
                    "budget_mode": "soft-display-target",
                    "observation_receipt_target_bytes": 4096,
                    "gate_schema_target_bytes": 4096,
                    "turn_target_bytes": 8192,
                    "target_exceeded_behavior": "preserve-runtime-semantics",
                    "manual_narrowing_required_on_target_exceeded": False,
                    "projection_budget_blocker": False,
                },
                "artifact_lifecycle": {
                    "created_raw_records": 64,
                    "restart_record_count": 66,
                    "first_gc_deleted_records": 33,
                    "second_gc_deleted_records": 31,
                    "final_audit_record_count": 2,
                    "shared_content_preserved_after_partial_gc": True,
                },
            },
            "batches": batches,
        }
        adr = root / "docs" / "adr"
        adr.mkdir(parents=True)
        tests = root / "tests"
        tests.mkdir()
        (tests / "public_seam.py").write_text("", encoding="utf-8")
        (root / "README.md").write_text(
            "[Roadmap evidence](docs/roadmap-completion.json)\n",
            encoding="utf-8",
        )
        (adr / "README.md").write_text(
            "| [ADR-0005](0005-retire-compatibility-writers-and-profile.md) "
            "| Accepted | Retire compatibility writers. |\n",
            encoding="utf-8",
        )
        (adr / "0005-retire-compatibility-writers-and-profile.md").write_text(
            "# ADR-0005\n\n- Status: Accepted\n\n[Evidence](../roadmap-completion.json)\n",
            encoding="utf-8",
        )
        (root / "docs" / "compatibility-retirement.md").write_text(
            "# Compatibility retirement\n\n"
            "The compatibility writers and profile are retired from canonical `main`.\n"
            "[Evidence](roadmap-completion.json)\n",
            encoding="utf-8",
        )
        (root / "docs" / "workflow-evolution-roadmap.md").write_text(
            "# Evolution roadmap\n\n[Evidence](roadmap-completion.json)\n",
            encoding="utf-8",
        )
        (root / "docs" / "workflow-architecture-arbitration.md").write_text(
            "Compatibility retirement is merged.\n",
            encoding="utf-8",
        )
        (root / "docs" / "external-workflow-research-reconciliation.md").write_text(
            "Compatibility retirement is merged.\n",
            encoding="utf-8",
        )
        (root / "docs" / "roadmap-completion-audit.md").write_text(
            "# Roadmap completion audit\n\n[Evidence](roadmap-completion.json)\n",
            encoding="utf-8",
        )
        (root / "docs" / "roadmap-completion.json").write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )

    def test_release_contract_accepts_a_completed_roadmap_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_completed_fixture(root)

            with mock.patch.object(validator, "ROOT", root):
                validator.validate_roadmap_closeout(verify_git=False)

    def test_release_contract_rejects_unmerged_candidate_wording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_completed_fixture(root)
            path = root / "README.md"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "The candidate must not be promoted to canonical `main`.\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(validator, "ROOT", root),
                self.assertRaisesRegex(SystemExit, "obsolete roadmap closeout state"),
            ):
                validator.validate_roadmap_closeout(verify_git=False)

    def test_release_contract_requires_final_identity_in_the_evolution_roadmap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_completed_fixture(root)
            (root / "docs" / "workflow-evolution-roadmap.md").write_text(
                "# Evolution roadmap\n\nOld candidate identities only.\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(validator, "ROOT", root),
                self.assertRaisesRegex(SystemExit, "roadmap closeout marker missing"),
            ):
                validator.validate_roadmap_closeout(verify_git=False)

    def test_release_contract_rejects_inconsistent_structured_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_completed_fixture(root)
            (root / "docs" / "roadmap-completion.json").write_text(
                json.dumps(
                    {
                        "schema": "openubmc-agent-workflow.roadmap-completion.v1",
                        "status": "completed",
                        "release_gate": {
                            "promotable": True,
                            "passed_gates": 12,
                            "total_gates": 13,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(validator, "ROOT", root),
                self.assertRaisesRegex(SystemExit, "roadmap completion evidence"),
            ):
                validator.validate_roadmap_closeout(verify_git=False)

    def test_release_contract_rejects_a_hard_projection_budget_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_completed_fixture(root)
            path = root / "docs" / "roadmap-completion.json"
            evidence = json.loads(path.read_text(encoding="utf-8"))
            evidence["continuous_qualification"]["agent_projection_policy"][
                "projection_budget_blocker"
            ] = True
            path.write_text(json.dumps(evidence), encoding="utf-8")

            with (
                mock.patch.object(validator, "ROOT", root),
                self.assertRaisesRegex(SystemExit, "continuous qualification"),
            ):
                validator.validate_roadmap_closeout(verify_git=False)

    def test_release_contract_rejects_unresolvable_repository_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_completed_fixture(root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "fixture"],
                check=True,
            )

            with (
                mock.patch.object(validator, "ROOT", root),
                self.assertRaisesRegex(SystemExit, "unresolvable roadmap completion commit"),
            ):
                validator.validate_roadmap_closeout()


if __name__ == "__main__":
    unittest.main()
