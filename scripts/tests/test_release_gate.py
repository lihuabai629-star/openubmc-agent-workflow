from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "release_gate.py"
SPEC = importlib.util.spec_from_file_location("openubmc_release_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_gate)


class ReleaseGateTests(unittest.TestCase):
    def test_all_release_gates_are_required_for_promotion(self) -> None:
        calls: list[tuple[str, ...]] = []

        def succeed(command, *, cwd):
            self.assertTrue(cwd.is_dir())
            calls.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = release_gate.execute_release_gate(
                current_ref="v1.2.0",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=root,
                executor=succeed,
                source_commit="source-commit-test",
            )

        self.assertTrue(report["promotable"])
        self.assertEqual(
            [item["name"] for item in report["gates"]],
            [
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
            ],
        )
        self.assertTrue(all(item["status"] == "passed" for item in report["gates"]))
        self.assertEqual(len(calls), 14)
        github_ci = calls[0]
        self.assertIn("github_ci_evidence.py", github_ci[1])
        self.assertEqual(
            github_ci[github_ci.index("--commit") + 1],
            "source-commit-test",
        )
        self.assertEqual(report["source_commit"], "source-commit-test")
        self.assertRegex(report["environment_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_failure_blocks_later_gates_and_promotion(self) -> None:
        call_count = 0

        def fail_upgrade(command, *, cwd):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(
                command,
                19 if call_count == 4 else 0,
                "",
                "upgrade failed" if call_count == 4 else "",
            )

        with tempfile.TemporaryDirectory() as directory:
            report = release_gate.execute_release_gate(
                current_ref="v1.2.0",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=fail_upgrade,
                source_commit="source-commit-test",
            )

        self.assertFalse(report["promotable"])
        self.assertEqual(
            [item["status"] for item in report["gates"]],
            ["passed", "passed", "failed"] + ["skipped"] * 10,
        )
        self.assertEqual(call_count, 4)

    def test_upgrade_installs_previous_then_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gates = dict(
                release_gate.gate_commands(
                    current_ref="v1.2.0",
                    previous_ref="v1.1.1",
                    clean_home=root / "clean",
                    lifecycle_home=root / "lifecycle",
                )
            )

        upgrade = gates["upgrade"]
        self.assertEqual(upgrade[0][upgrade[0].index("--ref") + 1], "v1.1.1")
        self.assertEqual(upgrade[1][upgrade[1].index("--ref") + 1], "v1.2.0")
        rollback = gates["rollback"][0]
        self.assertIn("rollback", rollback)
        self.assertIn("--skip-tool-install", rollback)
        qualification = gates["runtime_safety_qualification"][0]
        self.assertIn("runtime_qualification.py", qualification[1])
        self.assertIn("--output", qualification)
        ab_evidence = gates["agent_gateway_ab_evidence"][0]
        self.assertIn("agent_gateway_ab.py", ab_evidence[1])
        self.assertIn("verify", ab_evidence)

    def test_release_report_digests_runtime_qualification_evidence(self) -> None:
        def succeed(command, *, cwd):
            if "runtime_qualification.py" in " ".join(command):
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps({"promotable": True, "violations": {}}),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory:
            report = release_gate.execute_release_gate(
                current_ref="candidate",
                previous_ref="previous",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=succeed,
                source_commit="source-commit-test",
            )

        artifact = report["artifacts"]["runtime_qualification"]
        self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(artifact["size_bytes"], 0)

    def test_release_workflow_restores_and_requires_execute_ab_evidence(self) -> None:
        workflow = (Path.cwd() / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        for name in (
            "ab_summary_base64",
            "ab_metrics_base64",
            "ab_schedule_base64",
        ):
            self.assertIn(name, workflow)
        self.assertIn("--ab-evidence agent-gateway-ab-evidence/summary.json", workflow)
        self.assertIn("--github-repository \"${{ github.repository }}\"", workflow)
        self.assertIn("--work-root release-gate-work", workflow)
        self.assertIn("release-gate-work/github-ci-evidence.json", workflow)
        self.assertIn("python-version: \"3.12.13\"", workflow)
        self.assertEqual(
            release_gate.execute_release_gate.__kwdefaults__["github_repository"],
            "lihuabai629-star/openubmc-agent-workflow",
        )


if __name__ == "__main__":
    unittest.main()
