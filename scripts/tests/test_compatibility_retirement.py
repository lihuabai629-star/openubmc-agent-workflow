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
        "codex_adoption_qualification",
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
                ("codex_adoption_qualification", "b"),
                ("github_ci", "c"),
                ("agent_gateway_ab", "d"),
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

    def test_increment_reports_zero_use_without_a_calendar_window(self) -> None:
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
        )

        self.assertEqual(
            increment["schema"],
            "openubmc-agent-workflow.compatibility-retirement-increment.v3",
        )
        self.assertEqual(
            set(increment),
            {
                "schema",
                "baseline_digest",
                "baseline_source_commit",
                "baseline_captured_at",
                "baseline_telemetry",
                "source_commit",
                "captured_at",
                "operation_deltas",
                "feature_deltas",
                "telemetry",
                "evidence_digest",
            },
        )
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

    def test_writer_is_ready_with_zero_use_and_full_qualification(self) -> None:
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
        )

        decision = retirement.evaluate_retirement(
            increment,
            release_gate("b" * 40),
        )

        self.assertEqual(
            decision["schema"],
            "openubmc-agent-workflow.compatibility-retirement-decision.v3",
        )
        self.assertEqual(set(decision["writers"]), set(retirement.WRITER_METRICS))
        self.assertTrue(decision["writers"]["observe.assurance"]["ready"])
        self.assertTrue(decision["writers"]["execute.control_continue"]["ready"])
        self.assertTrue(decision["writers"]["execute.observation_receipt"]["ready"])
        self.assertTrue(decision["writers"]["phase_record"]["ready"])
        self.assertTrue(decision["writers"]["workflow.next"]["ready"])
        self.assertTrue(decision["compatibility_profile"]["ready"])
        self.assertEqual(
            set(decision),
            {
                "schema",
                "source_commit",
                "increment_digest",
                "release_gate_digest",
                "writers",
                "compatibility_profile",
                "preserved_readers",
                "evidence_digest",
            },
        )

    def test_calendar_gate_v1_increment_is_rejected(self) -> None:
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
        )
        increment["schema"] = (
            "openubmc-agent-workflow.compatibility-retirement-increment.v1"
        )
        increment["active_development_dates"] = ["2026-08-01"]
        increment["active_development_day_count"] = 1
        increment["evidence_digest"] = digest(
            {key: value for key, value in increment.items() if key != "evidence_digest"}
        )

        with self.assertRaisesRegex(ValueError, "schema is unsupported"):
            retirement.evaluate_retirement(
                increment,
                release_gate("b" * 40),
            )

    def test_unbound_v2_increment_is_rejected(self) -> None:
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
        )
        increment["schema"] = (
            "openubmc-agent-workflow.compatibility-retirement-increment.v2"
        )
        increment.pop("baseline_telemetry")
        increment["evidence_digest"] = digest(
            {key: value for key, value in increment.items() if key != "evidence_digest"}
        )

        with self.assertRaisesRegex(ValueError, "schema is unsupported"):
            retirement.evaluate_retirement(
                increment,
                release_gate("b" * 40),
            )

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

    def test_observe_assurance_growth_blocks_its_writer_and_the_profile(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        telemetry = current["compatibility_telemetry"]
        telemetry["feature_counts"]["observe.assurance"] = 1
        telemetry["total_features"] = 10
        telemetry["last_seen_at"]["features"]["observe.assurance"] = (
            1_700_100_000.0
        )
        increment = retirement.create_increment(
            baseline,
            current,
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
        )

        decision = retirement.evaluate_retirement(
            increment,
            release_gate("b" * 40),
        )

        assurance = decision["writers"]["observe.assurance"]
        self.assertFalse(assurance["ready"])
        self.assertIn("feature count increased by 1", assurance["blockers"])
        self.assertTrue(decision["writers"]["execute.control_continue"]["ready"])
        self.assertFalse(decision["compatibility_profile"]["ready"])

    def test_increment_capture_must_be_strictly_later_than_baseline(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )

        with self.assertRaisesRegex(ValueError, "must be later than the baseline"):
            retirement.create_increment(
                baseline,
                runtime_status(),
                source_commit="b" * 40,
                captured_at=1_700_001_000.0,
            )

    def test_non_finite_timestamps_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(location="tracking_started_at", value=value):
                status = runtime_status()
                status["compatibility_telemetry"]["tracking_started_at"] = value
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    retirement.create_baseline(
                        status,
                        source_commit="a" * 40,
                        captured_at=1_700_001_000.0,
                    )
            with self.subTest(location="last_seen_at", value=value):
                status = runtime_status()
                status["compatibility_telemetry"]["last_seen_at"]["features"][
                    "phase_record"
                ] = value
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    retirement.create_baseline(
                        status,
                        source_commit="a" * 40,
                        captured_at=1_700_001_000.0,
                    )
            with self.subTest(location="captured_at", value=value):
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    retirement.create_baseline(
                        runtime_status(),
                        source_commit="a" * 40,
                        captured_at=value,
                    )

    def test_digest_valid_increment_with_non_finite_time_is_rejected(self) -> None:
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
        )
        increment["captured_at"] = float("inf")
        increment["evidence_digest"] = digest(
            {key: value for key, value in increment.items() if key != "evidence_digest"}
        )

        with self.assertRaisesRegex(ValueError, "must be finite"):
            retirement.evaluate_retirement(
                increment,
                release_gate("b" * 40),
            )

    def test_digest_valid_increment_rejects_invalid_deltas(self) -> None:
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
        )
        cases = (
            ("feature_deltas", "phase_record", 0.5, "must be an integer"),
            ("feature_deltas", "workflow.next", True, "must be an integer"),
        )
        tampered = json.loads(json.dumps(increment))
        tampered["operation_deltas"]["phase_record"] = 1
        tampered["operation_deltas"]["workflow.next"] = -1
        tampered["evidence_digest"] = digest(
            {key: value for key, value in tampered.items() if key != "evidence_digest"}
        )
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            retirement.evaluate_retirement(tampered, release_gate("b" * 40))

        for mapping, metric, value, expected in cases:
            with self.subTest(mapping=mapping, metric=metric, value=value):
                tampered = json.loads(json.dumps(increment))
                tampered[mapping][metric] = value
                tampered["evidence_digest"] = digest(
                    {
                        key: item
                        for key, item in tampered.items()
                        if key != "evidence_digest"
                    }
                )
                with self.assertRaisesRegex(ValueError, expected):
                    retirement.evaluate_retirement(
                        tampered,
                        release_gate("b" * 40),
                    )

    def test_digest_valid_increment_rejects_missing_delta_metrics(self) -> None:
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
        )
        cases = (
            ("operation_deltas", {}),
            (
                "feature_deltas",
                {
                    key: value
                    for key, value in increment["feature_deltas"].items()
                    if key != "phase_record"
                },
            ),
        )
        for mapping, value in cases:
            with self.subTest(mapping=mapping):
                tampered = json.loads(json.dumps(increment))
                tampered[mapping] = value
                tampered["evidence_digest"] = digest(
                    {
                        key: item
                        for key, item in tampered.items()
                        if key != "evidence_digest"
                    }
                )
                with self.assertRaisesRegex(ValueError, "must match telemetry counts"):
                    retirement.evaluate_retirement(
                        tampered,
                        release_gate("b" * 40),
                    )

    def test_digest_valid_increment_rejects_rewritten_delta_values(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        current = runtime_status()
        telemetry = current["compatibility_telemetry"]
        telemetry["feature_counts"]["phase_record"] = 5
        telemetry["total_features"] = 10
        telemetry["last_seen_at"]["features"]["phase_record"] = (
            1_700_100_000.0
        )
        increment = retirement.create_increment(
            baseline,
            current,
            source_commit="b" * 40,
            captured_at=1_700_101_000.0,
        )
        self.assertEqual(increment["feature_deltas"]["phase_record"], 1)
        increment["feature_deltas"]["phase_record"] = 0
        increment["evidence_digest"] = digest(
            {key: value for key, value in increment.items() if key != "evidence_digest"}
        )

        with self.assertRaisesRegex(ValueError, "do not match baseline telemetry"):
            retirement.evaluate_retirement(
                increment,
                release_gate("b" * 40),
            )

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

    def test_increment_cli_needs_no_git_activity_history(self) -> None:
        baseline = retirement.create_baseline(
            runtime_status(),
            source_commit="a" * 40,
            captured_at=1_700_001_000.0,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline_path = root / "baseline.json"
            status_path = root / "runtime-status.json"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            status_path.write_text(json.dumps(runtime_status()), encoding="utf-8")

            completed = subprocess.run(
                [
                    __import__("sys").executable,
                    str(SCRIPT),
                    "increment",
                    "--baseline",
                    str(baseline_path),
                    "--runtime-status",
                    str(status_path),
                    "--source-commit",
                    "b" * 40,
                    "--captured-at",
                    "1700101000",
                ],
                cwd=root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        increment = json.loads(completed.stdout)
        self.assertEqual(increment["operation_deltas"], {"phase_record": 0, "workflow.next": 0})
        self.assertEqual(
            increment["feature_deltas"],
            {
                "execute.control_continue": 0,
                "execute.observation_receipt": 0,
                "phase_record": 0,
                "workflow.next": 0,
            },
        )

    def test_evaluate_cli_returns_nonzero_when_selected_writer_is_not_ready(self) -> None:
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
