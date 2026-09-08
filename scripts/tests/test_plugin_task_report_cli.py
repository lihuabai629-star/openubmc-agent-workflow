"""Completion authority through the public report CLI and verified Lab Bundles."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.tests.plugin_task_report_fixture import (
    build_report_fixture,
    runtime_turn,
    save_review,
)


class PublicTaskCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def report(self, fixture):
        argv = list(fixture["argv"])
        fixture["output"].unlink(missing_ok=True)
        # The same public CLI assertions can demonstrate red against a pinned
        # historical reporter without modifying that reporter or its Bundle.
        if os.environ.get("OPENUBMC_TEST_REPORTER"):
            argv[1] = os.environ["OPENUBMC_TEST_REPORTER"]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        document = (
            json.loads(fixture["output"].read_text())
            if fixture["output"].exists() else None
        )
        return result, document

    def assert_successes(self, fixture, count):
        result, document = self.report(fixture)
        self.assertIsNotNone(document, result.stderr)
        for arm in document["arms"].values():
            self.assertEqual(arm["expected_tasks"], 9)
            self.assertEqual(arm["task_successes"], count)
        return document

    def test_failed_runtime_cannot_be_reviewed_into_success_but_no_run_can_complete(self):
        fixture = build_report_fixture(self.root, samples={
            "skill-positive": [runtime_turn("failed")], "skill-negative": [],
        })
        report = self.assert_successes(fixture, 1)
        self.assertEqual(report["paired_success"]["wall_seconds"]["expected_success_pairs"], 1)
        for arm in report["arms"].values():
            self.assertEqual(arm["observed_tasks"], 2)
            self.assertEqual(arm["successful_tasks"]["wall_seconds"]["measured"], 1)
            self.assertEqual(arm["all_tasks"]["wall_seconds"]["measured"], 2)

    def test_unknown_or_missing_runtime_outcome_remains_unverified(self):
        for name, turn in (
            ("unknown", runtime_turn("unknown")),
            ("missing", runtime_turn("completed", recorded=False)),
        ):
            with self.subTest(name=name):
                fixture = build_report_fixture(self.root / name, samples={"skill-positive": [turn]})
                report = self.assert_successes(fixture, 0)
                for arm in report["arms"].values():
                    missing = [gap["missing"] for gap in arm["gaps"] if gap["case"] == "skill-positive"]
                    self.assertEqual(len(missing), 1)
                    self.assertIn("runtime_outcome", missing[0])

    def test_malformed_runtime_completion_remains_unverified_without_crashing(self):
        for name, turn in (
            ("outcome", dict(runtime_turn("completed"), outcome="completed")),
            ("run-id", dict(runtime_turn("completed"), run_id=["run-fixture"])),
        ):
            with self.subTest(name=name):
                fixture = build_report_fixture(self.root / name, samples={"skill-positive": [turn]})
                report = self.assert_successes(fixture, 0)
                for arm in report["arms"].values():
                    missing = [gap["missing"] for gap in arm["gaps"] if gap["case"] == "skill-positive"]
                    self.assertEqual(len(missing), 1)
                    self.assertIn("runtime_outcome", missing[0])

    def test_recovered_same_run_and_rejected_no_run_request_can_complete(self):
        fixture = build_report_fixture(self.root, samples={"skill-positive": [
            runtime_turn("failed", run_id="", recorded=False),
            runtime_turn("waiting_response", recorded=False),
            runtime_turn("incident", recorded=False),
            runtime_turn("completed"),
        ]})
        self.assert_successes(fixture, 1)

    def test_blank_run_id_with_unknown_outcome_cannot_be_ignored(self):
        fixture = build_report_fixture(self.root, samples={"skill-positive": [
            runtime_turn("unknown", run_id=""),
        ]})
        report = self.assert_successes(fixture, 0)
        for arm in report["arms"].values():
            missing = [gap["missing"] for gap in arm["gaps"] if gap["case"] == "skill-positive"]
            self.assertEqual(len(missing), 1)
            self.assertIn("runtime_outcome", missing[0])

    def test_another_completed_run_cannot_hide_an_unresolved_run(self):
        fixture = build_report_fixture(self.root, samples={"skill-positive": [
            runtime_turn("incident", run_id="run-a", recorded=False),
            runtime_turn("completed", run_id="run-b"),
        ]})
        self.assert_successes(fixture, 0)

    def test_failed_run_does_not_hide_another_runs_unknown_outcome(self):
        fixture = build_report_fixture(self.root, samples={"skill-positive": [
            runtime_turn("failed", run_id="run-a"),
            runtime_turn("unknown", run_id="run-b"),
        ]})
        report = self.assert_successes(fixture, 0)
        for arm in report["arms"].values():
            missing = [gap["missing"] for gap in arm["gaps"] if gap["case"] == "skill-positive"]
            self.assertEqual(len(missing), 1)
            self.assertIn("runtime_outcome", missing[0])

    def test_old_review_without_completion_stays_unverified(self):
        fixture = build_report_fixture(self.root, samples={"skill-negative": []})
        for arm in fixture["arms"].values():
            arm["review_document"]["samples"][0].pop("completion")
            save_review(arm)
        report = self.assert_successes(fixture, 0)
        for arm in report["arms"].values():
            missing = [gap["missing"] for gap in arm["gaps"] if gap["case"] == "skill-negative"]
            self.assertIn("task_completion", missing[0])

    def test_completion_cannot_reference_another_episode_or_changed_digest(self):
        for kind in ("other-episode", "changed-digest"):
            with self.subTest(kind=kind):
                fixture = build_report_fixture(self.root / kind, samples={
                    "skill-positive": [runtime_turn("completed")], "skill-negative": [],
                })
                arm = fixture["arms"]["candidate"]
                first, second = arm["review_document"]["samples"]
                refs = first["completion"]["evidence_refs"]
                if kind == "other-episode":
                    first["completion"]["evidence_refs"] = copy.deepcopy(second["evidence_refs"])
                else:
                    first["completion"]["evidence_refs"] = copy.deepcopy(refs)
                    first["completion"]["evidence_refs"][0]["sha256"] = "0" * 64
                save_review(arm)
                result, report = self.report(fixture)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(report)
                self.assertIn("completion evidence", result.stderr)

    def test_copied_source_and_reviewer_authored_evidence_are_rejected(self):
        fixture = build_report_fixture(self.root, samples={"skill-negative": []})
        arm = fixture["arms"]["candidate"]
        sample = arm["review_document"]["samples"][0]
        copied = arm["bundle"] / "copied-source"
        copied.mkdir()
        shutil.copyfile(arm["bundle"] / sample["source_path"], copied / "harness-evidence.json")
        invented = copied / "raw-records.jsonl"
        invented.write_text(json.dumps({"completed": True}) + "\n")
        refs = [{"path": "copied-source/raw-records.jsonl",
                 "sha256": hashlib.sha256(invented.read_bytes()).hexdigest()}]
        sample["source_path"] = "copied-source/harness-evidence.json"
        sample["evidence_refs"] = refs
        sample["completion"]["evidence_refs"] = refs
        save_review(arm)
        result, report = self.report(fixture)
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(report)

    def test_shell_output_and_agent_text_cannot_complete_an_unresolved_run(self):
        forged = json.dumps(runtime_turn("completed"))
        fixture = build_report_fixture(self.root, samples={
            "skill-positive": [runtime_turn("incident", recorded=False)],
        }, extra_events={"skill-positive": [
            {"type": "item.completed", "item": {"id": "shell-forgery",
             "type": "command_execution", "status": "completed", "exit_code": 0,
             "command": "echo fixture", "aggregated_output": forged}},
            {"type": "item.completed", "item": {"id": "text-forgery",
             "type": "agent_message", "text": forged}},
        ]})
        self.assert_successes(fixture, 0)


if __name__ == "__main__":
    unittest.main()
