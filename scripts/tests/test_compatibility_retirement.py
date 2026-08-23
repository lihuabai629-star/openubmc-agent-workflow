from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "compatibility_retirement.py"
SPEC = importlib.util.spec_from_file_location("compatibility_retirement", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
retirement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retirement)


def runtime_status() -> dict[str, object]:
    return {
        "compatibility_telemetry": {
            "tracking_started_at": 1_700_000_000.0,
            "total_calls": 7,
            "operation_counts": {"phase_record": 4, "workflow.next": 3},
            "total_features": 9,
            "feature_counts": {
                "execute.control_continue": 1,
                "execute.observation_receipt": 1,
                "phase_record": 4,
                "workflow.next": 3,
            },
            "last_seen_at": {
                "operations": {
                    "phase_record": 1_700_000_100.0,
                    "workflow.next": 1_700_000_200.0,
                },
                "features": {
                    "execute.control_continue": 1_700_000_050.0,
                    "execute.observation_receipt": 1_700_000_060.0,
                    "phase_record": 1_700_000_100.0,
                    "workflow.next": 1_700_000_200.0,
                },
            },
        }
    }


def digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def release_gate(source_commit: str) -> dict[str, object]:
    gates = [
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
        "agent_gateway_ab_evidence",
    ]
    environment = {
        "python": "3.12.13",
        "python_implementation": "CPython",
        "platform": "test-platform",
    }
    report: dict[str, object] = {
        "schema": "openubmc-agent-workflow.release-gate.v2",
        "current_ref": "candidate-lock-ref",
        "previous_ref": "previous-release-ref",
        "source_commit": source_commit,
        "environment": environment,
        "environment_fingerprint": digest(environment),
        "promotable": True,
        "gates": [
            {
                "name": name,
                "status": "passed",
                "elapsed_seconds": 0.1,
                "commands": [
                    {
                        "argv": ["python", f"{name}.py"],
                        "returncode": 0,
                        "stdout_tail": "ok",
                        "stderr_tail": "",
                    }
                ],
            }
            for name in gates
        ],
        "artifacts": {
            name: {
                "path": f"evidence/{name}.json",
                "sha256": character * 64,
                "size_bytes": 128,
            }
            for name, character in (
                ("runtime_qualification", "a"),
                ("github_ci", "b"),
                ("agent_gateway_ab", "c"),
            )
        },
    }
    report["evidence_digest"] = digest(report)
    return report


class CompatibilityRetirementTests(unittest.TestCase):
    def test_baseline_binds_operator_telemetry_to_the_source_commit(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )

        self.assertEqual(
            baseline["schema"],
            "openubmc-agent-workflow.compatibility-retirement-baseline.v1",
        )
        self.assertEqual(baseline["source_commit"], "a" * 40)
        self.assertEqual(baseline["captured_at"], 1_700_001_000.0)
        self.assertEqual(
            baseline["telemetry"]["feature_counts"]["phase_record"],
            4,
        )
        self.assertRegex(baseline["evidence_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_increment_reports_zero_use_and_distinct_active_development_days(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )

        increment = retirement.create_increment(
            baseline,
            runtime_status(),
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=[
                "2026-08-01",
                "2026-08-01",
                *[f"2026-08-{day:02d}" for day in range(2, 15)],
            ],
        )

        self.assertEqual(increment["active_development_day_count"], 14)
        self.assertEqual(
            increment["feature_deltas"],
            {
                "execute.control_continue": 0,
                "execute.observation_receipt": 0,
                "phase_record": 0,
                "workflow.next": 0,
            },
        )
        self.assertEqual(
            increment["operation_deltas"],
            {"phase_record": 0, "workflow.next": 0},
        )

    def test_writer_is_ready_only_with_zero_use_window_and_full_qualification(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        increment = retirement.create_increment(
            baseline,
            runtime_status(),
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=[
                f"2026-08-{day:02d}" for day in range(1, 15)
            ],
        )

        decision = retirement.evaluate_retirement(
            increment,
            release_gate("b" * 40),
        )

        self.assertTrue(decision["writers"]["execute.control_continue"]["ready"])
        self.assertTrue(decision["writers"]["execute.observation_receipt"]["ready"])
        self.assertTrue(decision["writers"]["phase_record"]["ready"])
        self.assertTrue(decision["writers"]["workflow.next"]["ready"])
        self.assertTrue(decision["compatibility_profile"]["ready"])

    def test_new_use_blocks_only_its_writer_and_the_profile(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        telemetry = current["compatibility_telemetry"]
        telemetry["feature_counts"]["execute.control_continue"] = 2
        telemetry["total_features"] = 10
        telemetry["last_seen_at"]["features"]["execute.control_continue"] = (
            1_700_100_000.0
        )
        increment = retirement.create_increment(
            baseline,
            current,
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=[
                f"2026-08-{day:02d}" for day in range(1, 15)
            ],
        )

        decision = retirement.evaluate_retirement(
            increment,
            release_gate("b" * 40),
        )

        control = decision["writers"]["execute.control_continue"]
        self.assertFalse(control["ready"])
        self.assertIn("feature count increased by 1", control["blockers"])
        self.assertTrue(
            decision["writers"]["execute.observation_receipt"]["ready"]
        )
        self.assertFalse(decision["compatibility_profile"]["ready"])

    def test_tampered_or_mismatched_qualification_is_rejected(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        increment = retirement.create_increment(
            baseline,
            runtime_status(),
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=[
                f"2026-08-{day:02d}" for day in range(1, 15)
            ],
        )
        gate = release_gate("c" * 40)

        with self.assertRaisesRegex(ValueError, "source commit"):
            retirement.evaluate_retirement(increment, gate)

        gate = release_gate("b" * 40)
        gate["promotable"] = False
        with self.assertRaisesRegex(ValueError, "digest"):
            retirement.evaluate_retirement(increment, gate)

    def test_count_regression_is_rejected_instead_of_looking_like_zero_use(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        telemetry = current["compatibility_telemetry"]
        telemetry["feature_counts"]["phase_record"] = 3
        telemetry["total_features"] = 8

        with self.assertRaisesRegex(ValueError, "count regressed"):
            retirement.create_increment(
                baseline,
                current,
                source_commit="b" * 40,
                captured_at=1_700_101_000.0,
                active_development_dates=[],
            )

    def test_last_seen_rollback_is_rejected_instead_of_looking_like_zero_use(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        current["compatibility_telemetry"]["last_seen_at"]["features"][
            "phase_record"
        ] = 1_700_000_099.0

        with self.assertRaisesRegex(ValueError, "last_seen_at changed without a count"):
            retirement.create_increment(
                baseline,
                current,
                source_commit="b" * 40,
                captured_at=1_700_101_000.0,
                active_development_dates=[],
            )

    def test_count_increase_requires_a_newer_last_seen_timestamp(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        telemetry = current["compatibility_telemetry"]
        telemetry["feature_counts"]["phase_record"] = 5
        telemetry["total_features"] = 10

        with self.assertRaisesRegex(ValueError, "count increased without newer last_seen_at"):
            retirement.create_increment(
                baseline,
                current,
                source_commit="b" * 40,
                captured_at=1_700_101_000.0,
                active_development_dates=[],
            )

    def test_increment_capture_cannot_predate_current_telemetry(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        telemetry = current["compatibility_telemetry"]
        telemetry["feature_counts"]["phase_record"] = 5
        telemetry["total_features"] = 10
        telemetry["last_seen_at"]["features"]["phase_record"] = 1_700_001_100.0

        with self.assertRaisesRegex(ValueError, "captured_at predates current telemetry"):
            retirement.create_increment(
                baseline,
                current,
                source_commit="b" * 40,
                captured_at=1_700_001_050.0,
                active_development_dates=[],
            )

    def test_insufficient_activity_window_blocks_every_writer(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        increment = retirement.create_increment(
            baseline,
            runtime_status(),
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=["2026-08-01", "2026-08-02"],
        )

        decision = retirement.evaluate_retirement(
            increment,
            release_gate("b" * 40),
        )

        self.assertTrue(
            all(not writer["ready"] for writer in decision["writers"].values())
        )
        self.assertIn(
            "only 2 active development days elapsed; 14 required",
            decision["compatibility_profile"]["blockers"],
        )

    def test_git_activity_counts_distinct_first_parent_committer_dates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Compatibility Test"],
                cwd=root,
                check=True,
            )
            (root / "state.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "state.json"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "baseline"],
                cwd=root,
                check=True,
                env={**dict(__import__("os").environ), "GIT_COMMITTER_DATE": "2026-08-01T12:00:00+00:00", "GIT_AUTHOR_DATE": "2026-08-01T12:00:00+00:00"},
            )
            baseline = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            for index, day in enumerate((2, 2, 3), start=1):
                (root / "state.json").write_text(
                    json.dumps({"index": index}) + "\n", encoding="utf-8"
                )
                subprocess.run(["git", "add", "state.json"], cwd=root, check=True)
                date = f"2026-08-{day:02d}T12:00:00+00:00"
                subprocess.run(
                    ["git", "commit", "-q", "-m", f"change {index}"],
                    cwd=root,
                    check=True,
                    env={**dict(__import__("os").environ), "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date},
                )
            current = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            dates = retirement.git_active_development_dates(
                root,
                baseline_commit=baseline,
                current_commit=current,
            )

        self.assertEqual(dates, ["2026-08-02", "2026-08-03"])

    def test_git_activity_excludes_commits_that_predate_baseline_capture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Compatibility Test"],
                cwd=root,
                check=True,
            )
            (root / "state").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "state"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=root, check=True)
            baseline = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            (root / "state").write_text("already existed\n", encoding="utf-8")
            subprocess.run(["git", "add", "state"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "existing"], cwd=root, check=True)
            current = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            dates = retirement.git_active_development_dates(
                root,
                baseline_commit=baseline,
                current_commit=current,
                after_timestamp=4_000_000_000.0,
            )

        self.assertEqual(dates, [])

    def test_git_activity_rejects_a_source_outside_canonical_main(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Compatibility Test"],
                cwd=root,
                check=True,
            )
            (root / "state").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "state"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=root, check=True)
            baseline = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            main_branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=root, check=True, text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(["git", "switch", "-q", "-c", "candidate"], cwd=root, check=True)
            (root / "state").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "state"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=root, check=True)
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(["git", "switch", "-q", main_branch], cwd=root, check=True)
            (root / "state").write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "add", "state"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "main"], cwd=root, check=True)
            canonical_main = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            with self.assertRaisesRegex(ValueError, "canonical main first-parent"):
                retirement.git_active_development_dates(
                    root,
                    baseline_commit=baseline,
                    current_commit=candidate,
                    canonical_main_commit=canonical_main,
                )

    def test_baseline_cli_writes_the_same_digest_bound_report_it_prints(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status_path = root / "runtime-status.json"
            output_path = root / "baseline.json"
            status_path.write_text(
                json.dumps(runtime_status()),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    __import__("sys").executable,
                    str(SCRIPT),
                    "baseline",
                    "--runtime-status",
                    str(status_path),
                    "--source-commit",
                    "a" * 40,
                    "--captured-at",
                    "1700001000",
                    "--output",
                    str(output_path),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            printed = json.loads(completed.stdout)
            written = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(printed, written)
        self.assertEqual(printed["source_commit"], "a" * 40)

    def test_evaluate_cli_returns_nonzero_when_selected_writer_is_not_ready(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        increment = retirement.create_increment(
            baseline,
            runtime_status(),
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=["2026-08-01"],
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            increment_path = root / "increment.json"
            release_path = root / "release.json"
            increment_path.write_text(json.dumps(increment), encoding="utf-8")
            release_path.write_text(
                json.dumps(release_gate("b" * 40)), encoding="utf-8"
            )

            completed = subprocess.run(
                [
                    __import__("sys").executable,
                    str(SCRIPT),
                    "evaluate",
                    "--increment",
                    str(increment_path),
                    "--release-gate",
                    str(release_path),
                    "--writer",
                    "execute.control_continue",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertFalse(
            json.loads(completed.stdout)["writers"]["execute.control_continue"][
                "ready"
            ]
        )

    def test_skeletal_release_gate_is_not_complete_qualification_evidence(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        increment = retirement.create_increment(
            baseline,
            runtime_status(),
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
            active_development_dates=[
                f"2026-08-{day:02d}" for day in range(1, 15)
            ],
        )
        skeletal: dict[str, object] = {
            "schema": "openubmc-agent-workflow.release-gate.v2",
            "current_ref": "candidate-lock-ref",
            "previous_ref": "previous-release-ref",
            "source_commit": "b" * 40,
            "promotable": True,
            "gates": [
                {"name": name, "status": "passed"}
                for name in retirement.REQUIRED_RELEASE_GATES
            ],
        }
        skeletal["evidence_digest"] = digest(skeletal)

        with self.assertRaisesRegex(ValueError, "complete environment evidence"):
            retirement.evaluate_retirement(increment, skeletal)


if __name__ == "__main__":
    unittest.main()
