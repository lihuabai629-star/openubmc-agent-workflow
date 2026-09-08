"""Public candidate/export and paired task-report boundaries."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.tests.plugin_fixture import package_fixture

ROOT = Path(__file__).resolve().parents[2]


class CandidateExportTests(unittest.TestCase):
    def test_candidate_export_pins_archive_and_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package_fixture(root)
            archive = root / "bundle.tar.gz"
            sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            output = root / "candidate"
            cli = [
                sys.executable,
                str(ROOT / "scripts/plugin_task_evaluation.py"),
                "prepare",
                "--archive",
                str(archive),
                "--sha256",
                sha,
                "--output",
                str(output),
            ]
            result = subprocess.run(cli, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            subject = json.loads((output / "subject.json").read_text())
            self.assertEqual(subject["kind"], "codex-plugin-candidate")
            self.assertEqual(subject["distribution"]["archive"]["sha256"], sha)
            self.assertFalse(subject["published"])
            status = json.loads((output / "qualification.json").read_text())
            self.assertEqual(status["status"], "unverified")
            cli[cli.index(sha)] = "0" * 64
            cli[-1] = str(root / "wrong")
            failed = subprocess.run(cli, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((root / "wrong").exists())


class PairedTaskReportTests(unittest.TestCase):
    def test_a_subject_cannot_qualify_against_itself(self):
        from scripts.plugin_task_evaluation import summarize_tasks

        case = {
            "case_id": "task",
            "oracle": {"required_predicates": [], "forbidden_predicates": []},
        }
        arm = {
            "identity": {
                "subject_digest": "sha256:" + "a" * 64,
                "archive_sha256": "b" * 64,
            },
            "episodes": [],
            "scores": [],
        }
        with self.assertRaisesRegex(ValueError, "distinct.*subject"):
            summarize_tasks(
                [case], {"baseline": arm, "candidate": arm}, repetitions=1
            )

    def test_different_subjects_cannot_qualify_the_same_archive(self):
        from scripts.plugin_task_evaluation import summarize_tasks

        case = {
            "case_id": "task",
            "oracle": {"required_predicates": [], "forbidden_predicates": []},
        }
        arms = {
            name: {
                "identity": {
                    "subject_digest": "sha256:" + marker * 64,
                    "archive_sha256": "c" * 64,
                },
                "episodes": [],
                "scores": [],
            }
            for name, marker in (("baseline", "a"), ("candidate", "b"))
        }
        with self.assertRaisesRegex(ValueError, "distinct.*archive"):
            summarize_tasks([case], arms, repetitions=1)

    def test_success_requires_task_predicates_and_complete_pair_metrics(self):
        from scripts.plugin_task_evaluation import summarize_tasks

        case = {
            "case_id": "credential-reuse",
            "oracle": {
                "required_predicates": ["reused"],
                "forbidden_predicates": ["secret_exposed"],
            },
        }
        episode = {
            "episode_id": "sample",
            "case_id": "credential-reuse",
            "repetition": 1,
            "status": "completed",
            "strict_success": True,
            "hard_failure": False,
            "task_completion": {"status": "completed"},
            "runtime_completion": {"status": "not-applicable", "runs": []},
            "pairing_identity": {"target_input_digest": "sha256:" + "a" * 64},
            "metrics": {
                "wall_seconds": 10,
                "cost_usd": 0.02,
                "extra_tool_calls": 0,
                "human_interventions": 0,
                "recovery_attempts": 0,
            },
        }
        scores = [
            {"episode_id": "sample", "scorer_id": "plugin-task." + name, "passed": True}
            for name in ("reused", "secret_exposed")
        ]
        arms = {
            "baseline": {"episodes": [episode], "scores": scores},
            "candidate": {
                "episodes": [
                    dict(episode, metrics=dict(episode["metrics"], wall_seconds=5))
                ],
                "scores": scores,
            },
        }
        report = summarize_tasks([case], arms, repetitions=1)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["arms"]["candidate"]["task_success_rate"], 1.0)
        self.assertEqual(
            report["paired_success"]["wall_seconds"]["candidate_minus_baseline"], -5.0
        )
        arms["candidate"]["episodes"][0]["metrics"].pop("cost_usd")
        incomplete = summarize_tasks([case], arms, repetitions=1)
        self.assertEqual(incomplete["status"], "unverified")
        self.assertIsNone(
            incomplete["arms"]["candidate"]["successful_tasks"]["cost_usd"]["mean"]
        )
        arms["candidate"]["scores"] = []
        lifecycle_only = summarize_tasks([case], arms, repetitions=1)
        self.assertEqual(lifecycle_only["arms"]["candidate"]["task_success_rate"], 0.0)
        self.assertEqual(lifecycle_only["status"], "unverified")


class TaskDenominatorTests(unittest.TestCase):
    def test_failed_tasks_remain_in_total_and_do_not_lower_successful_task_time(self):
        from scripts.plugin_task_evaluation import summarize_tasks

        case = {
            "case_id": "task",
            "oracle": {"required_predicates": ["complete"], "forbidden_predicates": []},
        }

        def episode(identifier, repeat, success, seconds):
            return {
                "episode_id": identifier,
                "case_id": "task",
                "repetition": repeat,
                "strict_success": success,
                "status": "completed" if success else "failed",
                "hard_failure": False,
                "task_completion": {"status": "completed" if success else "failed"},
                "runtime_completion": {"status": "not-applicable", "runs": []},
                "failure_layer": "runtime" if not success else None,
                "pairing_identity": {"target_input_digest": "sha256:" + "a" * 64},
                "metrics": {
                    "wall_seconds": seconds,
                    "cost_usd": 0,
                    "extra_tool_calls": 0,
                    "human_interventions": 0,
                    "recovery_attempts": 0,
                },
            }

        rows = [episode("one", 1, True, 10), episode("two", 2, False, 2)]
        scores = [
            {
                "episode_id": row["episode_id"],
                "scorer_id": "plugin-task.complete",
                "passed": row["strict_success"],
            }
            for row in rows
        ]
        data = {"episodes": rows, "scores": scores}
        report = summarize_tasks(
            [case], {"baseline": data, "candidate": data}, repetitions=2
        )
        candidate = report["arms"]["candidate"]
        self.assertEqual(candidate["task_success_rate"], 0.5)
        self.assertEqual(candidate["successful_tasks"]["wall_seconds"]["mean"], 10)
        self.assertEqual(candidate["all_tasks"]["wall_seconds"]["mean"], 6)
        self.assertEqual(candidate["failure_layers"], {"runtime": 1})
        self.assertEqual(report["status"], "failed")


class TaskReviewEvidenceTests(unittest.TestCase):
    def test_review_binds_episode_source_and_detects_changed_evidence(self):
        from scripts.plugin_task_evaluation import reviewed_task_scores, digest

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proof = root / "episode" / "trace.json"
            proof.parent.mkdir()
            proof.write_text('{"observed":"fixture"}')
            episode = {
                "episode_id": "ep",
                "case_id": "reuse",
                "source": {
                    "digest": "sha256:" + hashlib.sha256(proof.read_bytes()).hexdigest()
                },
            }
            case = {
                "case_id": "reuse",
                "oracle": {
                    "required_predicates": ["reused"],
                    "forbidden_predicates": [],
                },
            }
            review = {
                "schema": "openubmc.plugin-task-review.v1",
                "bundle_digest": "sha256:" + "b" * 64,
                "reviewer": "fixture-independent-review",
                "method": "independent-task-review",
                "samples": [
                    {
                        "episode_id": "ep",
                        "source_digest": episode["source"]["digest"],
                        "source_path": "episode/trace.json",
                        "predicates": {"reused": True},
                        "metrics": {"human_interventions": 0},
                        "evidence_refs": [
                            {
                                "path": "episode/trace.json",
                                "sha256": hashlib.sha256(
                                    proof.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ],
            }
            review["digest"] = digest(review)
            scores, metrics = reviewed_task_scores(
                root, review, [episode], [case], bundle_digest=review["bundle_digest"]
            )
            self.assertTrue(scores[0]["passed"])
            self.assertEqual(metrics["ep"]["human_interventions"], 0)
            other = root / "other.json"
            other.write_bytes(proof.read_bytes())
            review["samples"][0]["evidence_refs"][0]["path"] = "other.json"
            review["digest"] = digest(review)
            with self.assertRaisesRegex(ValueError, "evidence"):
                reviewed_task_scores(
                    root,
                    review,
                    [episode],
                    [case],
                    bundle_digest=review["bundle_digest"],
                )
            review["samples"][0]["evidence_refs"][0]["path"] = "episode/trace.json"
            review["digest"] = digest(review)
            proof.write_text("changed")
            with self.assertRaisesRegex(ValueError, "evidence"):
                reviewed_task_scores(
                    root,
                    review,
                    [episode],
                    [case],
                    bundle_digest=review["bundle_digest"],
                )


class ActualPairingTests(unittest.TestCase):
    def test_missing_target_evidence_stays_unverified_for_each_affected_sample(self):
        from scripts.plugin_task_evaluation import summarize_tasks

        case = {
            "case_id": "task",
            "oracle": {"required_predicates": ["complete"], "forbidden_predicates": []},
        }
        episode = {
            "episode_id": "ep",
            "case_id": "task",
            "repetition": 1,
            "status": "completed",
            "strict_success": True,
            "hard_failure": False,
            "task_completion": {"status": "completed"},
            "runtime_completion": {"status": "not-applicable", "runs": []},
            "metrics": {
                "wall_seconds": 1,
                "cost_usd": 0,
                "extra_tool_calls": 0,
                "human_interventions": 0,
                "recovery_attempts": 0,
            },
        }
        scores = [
            {"episode_id": "ep", "scorer_id": "plugin-task.complete", "passed": True}
        ]
        for affected in (("candidate",), ("baseline", "candidate")):
            for missing in (
                {},
                {"target_input_digest": None},
                {"target_input_digest": "sha256:not-a-digest"},
            ):
                with self.subTest(affected=affected, missing=missing):
                    arms = {
                        name: {
                            "episodes": [
                                dict(
                                    episode,
                                    pairing_identity=missing
                                    if name in affected
                                    else {"target_input_digest": "sha256:" + "a" * 64},
                                )
                            ],
                            "scores": scores,
                        }
                        for name in ("baseline", "candidate")
                    }
                    report = summarize_tasks([case], arms, repetitions=1)
                    self.assertEqual(report["status"], "unverified")
                    for name in affected:
                        self.assertEqual(report["arms"][name]["task_successes"], 0)
                        self.assertIn(
                            {
                                "case": "task",
                                "repetition": 1,
                                "missing": ["target_input_digest"],
                            },
                            report["arms"][name]["gaps"],
                        )
        # An explicitly recorded empty target is still known input.
        for arm in arms.values():
            arm["episodes"][0]["pairing_identity"] = {
                "target_input_digest": (
                    "sha256:44136fa355b3678a1146ad16f7e8649e"
                    "94fb4fc21fe77e8310c060f61caaff8a"
                )
            }
        self.assertEqual(
            summarize_tasks([case], arms, repetitions=1)["status"], "passed"
        )

    def test_same_declared_task_cannot_pair_different_actual_targets(self):
        from scripts.plugin_task_evaluation import summarize_tasks

        case = {
            "case_id": "task",
            "oracle": {"required_predicates": ["complete"], "forbidden_predicates": []},
        }
        row = {
            "episode_id": "ep",
            "case_id": "task",
            "repetition": 1,
            "status": "completed",
            "strict_success": True,
            "hard_failure": False,
            "metrics": {},
            "pairing_identity": {
                "target_input_digest": "sha256:" + "a" * 64,
                "case_prompt_digest": "prompt",
            },
        }
        baseline = {"episodes": [row], "scores": []}
        candidate = {
            "episodes": [
                dict(
                    row,
                    pairing_identity={
                        "target_input_digest": "sha256:" + "b" * 64,
                        "case_prompt_digest": "prompt",
                    },
                )
            ],
            "scores": [],
        }
        with self.assertRaisesRegex(ValueError, "actual task inputs"):
            summarize_tasks(
                [case], {"baseline": baseline, "candidate": candidate}, repetitions=1
            )


class ProviderPairingTests(unittest.TestCase):
    def test_same_model_name_with_different_actual_providers_is_not_a_pair(self):
        from scripts.plugin_task_evaluation import (
            validate_task_identity,
            summarize_tasks,
        )

        def configuration(provider):
            records = [
                {
                    "event_type": "harness.prepared",
                    "payload": {
                        "adapter_kind": "codex",
                        "identity": {
                            "plugin_runtime": {
                                "subject_digest": "subject",
                                "runtime_digest": "runtime",
                            },
                            "executable": {"digest": "sha256:client"},
                            "model_configuration": {
                                "model": "fixed",
                                "provider": {"configuration_digest": provider},
                            },
                        },
                    },
                }
            ]
            return validate_task_identity(
                {"adapter_kind": "codex"},
                records,
                subject_digest="subject",
                runtime_digest="runtime",
                client="client",
                model="fixed",
            )

        case = {
            "case_id": "task",
            "oracle": {"required_predicates": ["complete"], "forbidden_predicates": []},
        }
        row = {
            "episode_id": "ep",
            "case_id": "task",
            "repetition": 1,
            "status": "completed",
            "strict_success": True,
            "hard_failure": False,
            "metrics": {},
        }
        arms = {
            name: {
                "episodes": [
                    dict(
                        row,
                        pairing_identity={"model_configuration": configuration(name)},
                    )
                ],
                "scores": [],
            }
            for name in ("baseline", "candidate")
        }
        with self.assertRaisesRegex(ValueError, "actual task inputs"):
            summarize_tasks([case], arms, repetitions=1)


class LoadedTaskIdentityTests(unittest.TestCase):
    def test_actual_harness_identity_must_match_loaded_plugin_and_client(self):
        from scripts.plugin_task_evaluation import validate_task_identity

        identity = {
            "plugin_runtime": {
                "subject_digest": "subject",
                "runtime_digest": "runtime",
            },
            "executable": {"digest": "sha256:client"},
            "model_configuration": {"model": "fixed"},
        }
        source = {"adapter_kind": "codex"}
        records = [
            {
                "event_type": "harness.prepared",
                "payload": {"adapter_kind": "codex", "identity": identity},
            }
        ]
        self.assertTrue(
            validate_task_identity(
                source,
                records,
                subject_digest="subject",
                runtime_digest="runtime",
                client="client",
                model="fixed",
            )
        )
        identity["plugin_runtime"]["runtime_digest"] = "other-runtime"
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_task_identity(
                source,
                records,
                subject_digest="subject",
                runtime_digest="runtime",
                client="client",
                model="fixed",
            )
        self.assertFalse(
            validate_task_identity(
                source,
                [],
                subject_digest="subject",
                runtime_digest="runtime",
                client="client",
                model="fixed",
            )
        )


if __name__ == "__main__":
    unittest.main()
