"""Fail-closed platform acceptance evidence tests for issue #284."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import platform_acceptance as acceptance  # noqa: E402


SOURCE = "a" * 40
HASH = hashlib.sha256(b"qualification-evidence").hexdigest()


def host(role: str, os_name: str, architecture: str, environment: str) -> dict[str, str]:
    return {
        "role": role, "os": os_name, "architecture": architecture,
        "environment": environment, "identity": f"{role}-one",
    }


def passing_matrix() -> dict[str, object]:
    matrix = acceptance.initial_report(SOURCE)
    for row in matrix["rows"]:
        row_id = row["id"]
        if row_id in {"macos-arm64-local", "windows-wsl-runtime"}:
            continue
        row.update({
            "status": "passed", "reason": "", "source_commit": SOURCE,
            "source_clean": True, "observed_at": "2026-09-27T01:00:00Z",
            "client_version": "codex-cli 0.153.4", "runtime_version": "2.1.2",
            "package_sha256": HASH, "artifacts": {"report": HASH},
            "toolchain": {"python": "3.12.13", "node": "22.23.2", "codex": "0.153.4",
                          "installer": "desktop-fixture"},
            "commands": [{"argv": ["python3", "qualification.py"], "exit_code": 0,
                          "stdout_sha256": HASH, "stderr_sha256": HASH}],
            "checks": {name: {"passed": True, "evidence_sha256": HASH}
                       for name in acceptance.ROWS[row_id]},
        })
        if row_id == "linux-x86_64":
            row["commands"] = [
                {"argv": ["python3", "scripts/validate_workflow.py"], "exit_code": 0,
                 "stdout_sha256": HASH, "stderr_sha256": HASH},
                {"argv": ["python3", "scripts/qualify_plugin.py"], "exit_code": 0,
                 "stdout_sha256": HASH, "stderr_sha256": HASH},
            ]
            row["artifacts"]["plugin_archive"] = HASH
            row["hosts"] = [host("client", "Linux", "x86_64", "native-linux"),
                            host("runtime", "Linux", "x86_64", "native-linux")]
            row["emulated"] = False
            row["test_counts"] = {"python": 127, "node": 19}
        elif row_id == "windows-bootstrap":
            row["commands"] = [{"argv": ["pwsh", "scripts/qualify_windows_plugin.ps1"],
                                "exit_code": 0, "stdout_sha256": HASH, "stderr_sha256": HASH}]
            row["artifacts"]["plugin_archive"] = HASH
            row["hosts"] = [host("client", "Windows", "x86_64", "native-windows")]
        elif row_id == "windows-native-device":
            row["artifacts"]["plugin_archive"] = HASH
            row["hosts"] = [host("client", "Windows", "x86_64", "native-windows"),
                            host("runtime", "Windows", "x86_64", "native-windows"),
                            host("target", "Synthetic", "none", "fixture")]
            row["routing"] = {
                "execution_host": "windows-native",
                "selected_wsl": None,
                "backend_pid": 1234,
                "credential_revision_before": "fixture-revision-1",
                "credential_revision_after": "fixture-revision-1",
                "run_id_before": "run-one", "run_id_after": "run-one",
                "effect_count_before": 1, "effect_count_after": 1,
            }
        elif row_id == "desktop-synthetic":
            row["artifacts"]["desktop_installer"] = HASH
            row["hosts"] = [host("client", "Darwin", "arm64", "native-desktop"),
                            host("target", "Synthetic", "none", "fixture")]
            row["desktop_source_commit"] = "b" * 40
            row["same_run_outcome"] = {
                "plugin_run_id": "run-one", "desktop_run_id": "run-one",
                "plugin_outcome_sha256": HASH, "desktop_outcome_sha256": HASH,
            }
        else:
            row["hosts"] = [host("linux-runner", "Linux", "x86_64", "github-hosted"),
                            host("windows-runner", "Windows", "x86_64", "github-hosted")]
            row["ci_run"] = {
                "id": 123, "repository": acceptance.REPOSITORY,
                "path": acceptance.WORKFLOW_PATH, "head_sha": SOURCE,
                "event": "pull_request", "conclusion": "success",
                "jobs": [{"name": name, "conclusion": "success", "steps_count": 4}
                         for name in acceptance.CI_JOBS],
            }
    return matrix


class PlatformAcceptanceTests(unittest.TestCase):
    def test_new_matrix_keeps_every_required_platform_open(self) -> None:
        report = acceptance.assess(acceptance.initial_report(SOURCE))
        self.assertFalse(report["release_ready"])
        self.assertEqual(len([item for item in report["blockers"] if "untested" in item]), 5)

    def test_complete_native_evidence_can_qualify_same_source(self) -> None:
        report = acceptance.assess(passing_matrix())
        self.assertTrue(report["release_ready"], report["blockers"])
        self.assertEqual(len(report["evidence_digest"]), 64)

    def test_installed_candidate_can_qualify_when_hosted_jobs_never_started(self) -> None:
        matrix = passing_matrix()
        matrix["validation_mode"] = "installed-candidate"
        matrix["candidate_archive_sha256"] = HASH
        hosted = next(row for row in matrix["rows"] if row["id"] == "hosted-ci")
        hosted.clear()
        hosted.update({"id": "hosted-ci", "status": "untested", "reason": "Account billing blocked job startup"})

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "candidate.tar.gz"
            archive.write_bytes(b"qualification-evidence")
            report = acceptance.assess(matrix, candidate_archive=archive)

        self.assertTrue(report["release_ready"], report["blockers"])
        self.assertEqual(report["validation_mode"], "installed-candidate")
        self.assertEqual(report["candidate_archive_sha256"], HASH)

    def test_installed_candidate_rejects_missing_or_wrong_archive_and_executed_ci_failure(self) -> None:
        matrix = passing_matrix()
        matrix["validation_mode"] = "installed-candidate"
        matrix["candidate_archive_sha256"] = HASH
        hosted = next(row for row in matrix["rows"] if row["id"] == "hosted-ci")
        hosted.clear()
        hosted.update({"id": "hosted-ci", "status": "untested", "reason": "No hosted execution"})
        self.assertFalse(acceptance.assess(matrix)["release_ready"])
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "candidate.tar.gz"
            archive.write_bytes(b"different archive")
            self.assertFalse(acceptance.assess(matrix, candidate_archive=archive)["release_ready"])
            archive.write_bytes(b"qualification-evidence")
            hosted["ci_run"] = {"jobs": [{"name": "CI contract preflight", "steps_count": 3,
                                           "conclusion": "failure"}]}
            self.assertFalse(acceptance.assess(matrix, candidate_archive=archive)["release_ready"])

    def test_verify_cli_binds_candidate_to_expected_source_and_archive_bytes(self) -> None:
        matrix = passing_matrix()
        matrix["validation_mode"] = "installed-candidate"
        matrix["candidate_archive_sha256"] = HASH
        hosted = next(row for row in matrix["rows"] if row["id"] == "hosted-ci")
        hosted.clear()
        hosted.update({"id": "hosted-ci", "status": "untested", "reason": "No hosted execution"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "candidate.tar.gz"
            archive.write_bytes(b"qualification-evidence")
            source = root / "matrix.json"
            source.write_text(json.dumps(matrix), encoding="utf-8")
            output = root / "assessed.json"
            command = [sys.executable, str(SCRIPTS / "platform_acceptance.py"), "verify",
                       "--input", str(source), "--output", str(output),
                       "--candidate-archive", str(archive),
                       "--expected-source-commit", SOURCE]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertTrue(json.loads(output.read_text())["release_ready"])
            command[-1] = "b" * 40
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_init_cli_records_immutable_candidate_archive_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "candidate.tar.gz"
            archive.write_bytes(b"qualification-evidence")
            output = root / "matrix.json"
            command = [sys.executable, str(SCRIPTS / "platform_acceptance.py"), "init",
                       "--source-commit", SOURCE, "--output", str(output),
                       "--validation-mode", "installed-candidate",
                       "--candidate-archive", str(archive)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["validation_mode"], "installed-candidate")
            self.assertEqual(report["candidate_archive_sha256"], HASH)

    def test_arm_or_container_linux_cannot_certify_native_x86_64(self) -> None:
        matrix = passing_matrix()
        linux = matrix["rows"][0]
        linux["hosts"][0]["architecture"] = "arm64"
        linux["hosts"][1]["environment"] = "container"
        report = acceptance.assess(matrix)
        self.assertFalse(report["release_ready"])
        self.assertTrue(any("native Linux x86_64" in item for item in report["blockers"]))

    def test_quick_validation_cannot_replace_complete_linux_gate(self) -> None:
        matrix = passing_matrix()
        linux = matrix["rows"][0]
        linux["commands"][0]["argv"].append("--quick")
        report = acceptance.assess(matrix)
        self.assertFalse(report["release_ready"])
        self.assertTrue(any("complete validation" in item for item in report["blockers"]))

    def test_native_runtime_identity_or_second_effect_blocks_release(self) -> None:
        matrix = passing_matrix()
        route = next(row for row in matrix["rows"] if row["id"] == "windows-native-device")
        route["routing"]["effect_count_after"] = 2
        route["routing"]["credential_revision_after"] = "different"
        self.assertFalse(acceptance.assess(matrix)["release_ready"])

    def test_wsl_evidence_cannot_replace_the_native_windows_device_row(self) -> None:
        matrix = passing_matrix()
        native = next(row for row in matrix["rows"] if row["id"] == "windows-native-device")
        native.update(status="untested", reason="Native device workflow not yet qualified")
        self.assertFalse(acceptance.assess(matrix)["release_ready"])
        self.assertTrue(any("windows-native-device" in item for item in acceptance.assess(matrix)["blockers"]))

    def test_wsl_row_is_optional_and_native_route_must_stay_on_windows(self) -> None:
        matrix = passing_matrix()
        self.assertTrue(acceptance.assess(matrix)["release_ready"])
        native = next(row for row in matrix["rows"] if row["id"] == "windows-native-device")
        native["hosts"][1] = host("runtime", "Linux", "x86_64", "wsl2")
        native["routing"]["selected_wsl"] = "Ubuntu-24.04"
        self.assertFalse(acceptance.assess(matrix)["release_ready"])

    def test_desktop_outcome_must_match_the_plugin_run(self) -> None:
        matrix = passing_matrix()
        desktop = next(row for row in matrix["rows"] if row["id"] == "desktop-synthetic")
        desktop["same_run_outcome"]["desktop_outcome_sha256"] = "b" * 64
        report = acceptance.assess(matrix)
        self.assertFalse(report["release_ready"])
        self.assertTrue(any("same Run/Outcome" in item for item in report["blockers"]))

    def test_billing_skipped_ci_job_cannot_pass_even_with_success_claim(self) -> None:
        matrix = passing_matrix()
        ci = next(row for row in matrix["rows"] if row["id"] == "hosted-ci")
        ci["ci_run"]["jobs"][2]["conclusion"] = "skipped"
        ci["ci_run"]["jobs"][2]["steps_count"] = 0
        self.assertFalse(acceptance.assess(matrix)["release_ready"])
        self.assertFalse(acceptance.evaluate_ci_run(
            {"id": 123, "path": acceptance.WORKFLOW_PATH, "head_sha": SOURCE,
             "event": "pull_request", "conclusion": "failure"},
            {"jobs": [{"name": name, "conclusion": "skipped", "steps": []}
                      for name in acceptance.CI_JOBS]},
            source_commit=SOURCE,
        )["passed"])

    def test_wrong_commit_or_package_identity_blocks_release(self) -> None:
        matrix = passing_matrix()
        windows = next(row for row in matrix["rows"] if row["id"] == "windows-bootstrap")
        windows["package_sha256"] = "b" * 64
        windows["source_commit"] = "b" * 40
        report = acceptance.assess(matrix)
        self.assertFalse(report["release_ready"])
        self.assertTrue(any("package digests differ" in item for item in report["blockers"]))

    def test_missing_nonpassing_reason_is_reported_without_crashing(self) -> None:
        matrix = acceptance.initial_report(SOURCE)
        matrix["rows"][0].pop("reason")
        report = acceptance.assess(matrix)
        self.assertFalse(report["release_ready"])
        self.assertTrue(any("reason missing" in item for item in report["blockers"]))

    def test_command_capture_records_real_exit_counts_and_artifact_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact.json"
            artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")
            output = acceptance.capture_command(
                [sys.executable, "-c", "print('Ran 3 tests\\n# tests 2')"],
                cwd=root, log_dir=root / "private-logs", artifacts={"report": artifact},
            )
            self.assertEqual(output["exit_code"], 0)
            self.assertEqual(output["test_counts"], {"python": 3, "node": 2})
            self.assertTrue(output["toolchain"]["python"].startswith("Python "))
            self.assertEqual(output["artifacts"]["report"], acceptance.file_digest(artifact))
            self.assertEqual(output["stdout_sha256"], acceptance.file_digest(root / "private-logs/stdout.log"))


if __name__ == "__main__":
    unittest.main()
