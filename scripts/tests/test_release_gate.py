from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


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
                    source_commit="a" * 40,
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
        self.assertEqual(
            ab_evidence[ab_evidence.index("--source-ref") + 1],
            "a" * 40,
        )
        self.assertEqual(
            Path(ab_evidence[ab_evidence.index("--attestation-public-key") + 1]),
            root / "agent-gateway-ab-attestation.pub",
        )

    def test_release_source_commit_is_read_from_the_lock_only_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "release-gate@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Gate Test"],
                cwd=root,
                check=True,
            )
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "source"], cwd=root, check=True
            )
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            (root / "release-lock.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "release-lock.json"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "lock"], cwd=root, check=True
            )
            lock_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            with patch.object(
                release_gate,
                "verify_release_lock",
                return_value={"source_commit": source_commit},
            ):
                resolved = release_gate._resolve_release_source_commit(
                    root,
                    lock_commit,
                )

        self.assertEqual(resolved, source_commit)

    def test_invalid_release_lock_fails_instead_of_falling_back(self) -> None:
        with patch.object(
            release_gate,
            "_resolve_commit",
            return_value="b" * 40,
        ), patch.object(
            release_gate,
            "verify_release_lock",
            side_effect=release_gate.ReleaseLockError("bad lock"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid immutable release ref"):
                release_gate._resolve_release_source_commit(
                    Path.cwd(),
                    "release-ref",
                )

    def test_self_referencing_release_lock_is_rejected(self) -> None:
        release_commit = "c" * 40
        with patch.object(
            release_gate,
            "_resolve_commit",
            return_value=release_commit,
        ), patch.object(
            release_gate,
            "verify_release_lock",
            return_value={"source_commit": release_commit},
        ):
            with self.assertRaisesRegex(ValueError, "lock-only child"):
                release_gate._resolve_release_source_commit(
                    Path.cwd(),
                    "release-ref",
                )

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
        workflow = yaml.load(
            (Path.cwd() / ".github" / "workflows" / "release.yml").read_text(
                encoding="utf-8"
            ),
            Loader=yaml.BaseLoader,
        )
        self.assertIsInstance(workflow, dict)
        dispatch = workflow["on"]["workflow_dispatch"]
        self.assertEqual(
            set(dispatch["inputs"]),
            {
                "current_ref",
                "previous_ref",
                "ab_bundle_base64",
                "ab_bundle_sha256",
                "promote",
            },
        )
        self.assertEqual(workflow["permissions"]["actions"], "read")
        self.assertEqual(workflow["permissions"]["checks"], "read")

        steps = {
            step.get("name", step.get("uses", "")): step
            for step in workflow["jobs"]["release-gate"]["steps"]
        }
        self.assertIn("actions/checkout@v7", steps)
        self.assertEqual(
            steps["actions/setup-python@v7"]["with"]["python-version"],
            "3.12.13",
        )
        restore = steps["Restore execute AB qualification evidence"]["run"]
        self.assertIn("sha256sum --check --strict", restore)
        self.assertIn("scripts/restore_ab_bundle.py", restore)
        trust_root = steps["Restore AB attestation trust root"]
        self.assertEqual(
            trust_root["env"]["AB_ATTESTATION_PUBLIC_KEY_BASE64"],
            "${{ vars.AB_ATTESTATION_PUBLIC_KEY_BASE64 }}",
        )
        self.assertIn("base64 --decode", trust_root["run"])
        self.assertIn("$RUNNER_TEMP/agent-gateway-ab-attestation.pub", trust_root["run"])
        gate = steps["Run immutable release gates"]["run"]
        self.assertIn("--ab-evidence agent-gateway-ab-evidence/summary.json", gate)
        self.assertIn(
            '--ab-attestation-public-key "$RUNNER_TEMP/agent-gateway-ab-attestation.pub"',
            gate,
        )
        self.assertIn("--github-repository \"${{ github.repository }}\"", gate)
        self.assertIn("--work-root release-gate-work", gate)
        uploaded = set(
            steps["Upload release evidence"]["with"]["path"].splitlines()
        )
        self.assertEqual(
            uploaded,
            {
                "agent-gateway-ab-evidence/summary.json",
                "agent-gateway-ab-evidence/all_metrics.json",
                "agent-gateway-ab-evidence/run_evidence.json",
                "agent-gateway-ab-evidence/schedule.json",
                "agent-gateway-ab-evidence.tar.xz",
                "release-gate.json",
                "release-gate-work/github-ci-evidence.json",
                "release-gate-work/runtime-qualification.json",
            },
        )
        self.assertEqual(
            release_gate.execute_release_gate.__kwdefaults__["github_repository"],
            "lihuabai629-star/openubmc-agent-workflow",
        )


if __name__ == "__main__":
    unittest.main()
