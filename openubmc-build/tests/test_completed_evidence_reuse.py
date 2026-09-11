from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    from .support import init_repo
except ImportError:
    from support import init_repo


BUILD_ROOT = Path(__file__).resolve().parents[1]
CREATOR = BUILD_ROOT / "scripts/create_build_plan.py"
RUNNER = BUILD_ROOT / "scripts/run_build_attempt.py"


class CompletedEvidenceReuseTests(unittest.TestCase):
    def fixture(self, root: Path, *, mode: str = "validate", kind: str = "official-ut") -> dict[str, Path]:
        repo = root / "component"
        repo.mkdir()
        init_repo(repo)
        toolchain = root / "toolchain.lock"
        toolchain.write_text("immutable-toolchain-v1\n")
        count = root / "executions.txt"
        output = root / "result.bin"
        plan = root / "plan.json"
        executable = root / "local-python"
        shutil.copy2(sys.executable, executable)
        command = (
            "from pathlib import Path; import sys, os; "
            "count, output = map(Path, sys.argv[1:]); "
            "count.write_text(count.read_text() + 'run\\n' if count.exists() else 'run\\n'); "
            "output.write_bytes(b'validated output'); print('local checks passed'); "
            "sys.exit(1 if os.environ.get('FIXTURE_FAIL_BUILD') else 0)"
        )
        created = self.invoke(CREATOR, "--mode", mode, "--workspace", f"component={repo}",
                              "--cwd", str(repo), "--output", str(plan),
                              "--reuse-evidence", kind, "--evidence-input", f"toolchain={toolchain}",
                              "--evidence-output", f"result={output}", "--", str(executable),
                              "-c", command, str(count), str(output))
        self.assertEqual(created.returncode, 0, created.stderr)
        return {"repo": repo, "toolchain": toolchain, "count": count, "output": output,
                "plan": plan, "executable": executable}

    def invoke(self, script: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *arguments], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)

    def attempt(self, fixture: dict[str, Path], *, env: dict[str, str] | None = None) -> dict[str, object]:
        result = self.invoke(RUNNER, "--plan", str(fixture["plan"]), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_reuses_completed_local_evidence_without_reexecution_or_new_completion_time(self) -> None:
        for mode, kind in (("validate", "official-ut"), ("component-package", "compile")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                fixture = self.fixture(Path(raw), mode=mode, kind=kind)
                first = self.attempt(fixture)
                second = self.attempt(fixture)
                self.assertFalse(first["reused"])
                self.assertTrue(second["reused"])
                self.assertEqual(second["attempt_id"], first["attempt_id"])
                self.assertEqual(second["finished_at"], first["finished_at"])
                self.assertEqual(fixture["count"].read_text(), "run\n")

    def test_explicit_fresh_attempt_can_run_after_completed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.fixture(Path(raw))
            first = self.attempt(fixture)
            result = self.invoke(RUNNER, "--plan", str(fixture["plan"]), "--fresh")
            self.assertEqual(result.returncode, 0, result.stderr)
            second = json.loads(result.stdout)
            self.assertFalse(second["reused"])
            self.assertNotEqual(second["attempt_id"], first["attempt_id"])
            self.assertEqual(fixture["count"].read_text(), "run\nrun\n")

    def test_environment_equality_is_private_and_changes_execute_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.fixture(root)
            environment = {**os.environ, "BMC_PASSWORD": "sensitive-fixture-only-835791"}
            first = self.attempt(fixture, env=environment)
            self.assertTrue(self.attempt(fixture, env=environment)["reused"])
            changed = {**environment, "BMC_PASSWORD": "different-sensitive-fixture-971538"}
            second = self.attempt(fixture, env=changed)
            self.assertFalse(second["reused"])
            self.assertNotEqual(second["attempt_id"], first["attempt_id"])
            self.assertEqual(fixture["count"].read_text(), "run\nrun\n")
            for path in root.rglob("*.json"):
                content = path.read_text()
                self.assertNotIn(environment["BMC_PASSWORD"], content)
                self.assertNotIn(changed["BMC_PASSWORD"], content)
                self.assertNotIn("BMC_PASSWORD", content)
            self.assertNotIn("environment", json.dumps(second))

    def test_output_log_command_or_completion_tampering_prevents_reuse(self) -> None:
        for changed in ("output", "log", "command", "state", "proof", "missing-proof", "missing-key"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as raw:
                fixture = self.fixture(Path(raw))
                first = self.attempt(fixture)
                state_path = Path(first["state_path"])
                if changed == "output":
                    fixture["output"].write_bytes(b'tampered result')
                elif changed == "log":
                    Path(first["log_path"]).write_text("replacement log\n")
                elif changed == "command":
                    Path(first["command_path"]).write_text("{}\n")
                elif changed == "state":
                    state = json.loads(state_path.read_text())
                    state["finished_at"] = "2099-01-01T00:00:00Z"
                    state_path.write_text(json.dumps(state))
                elif changed == "proof":
                    proof_path = state_path.parent / "completed-evidence.json"
                    proof = json.loads(proof_path.read_text())
                    proof["proof"]["outputs"] = {}
                    proof_path.write_text(json.dumps(proof))
                elif changed == "missing-proof":
                    (state_path.parent / "completed-evidence.json").unlink()
                elif changed == "missing-key":
                    (state_path.parent.parent.parent / ".evidence-reuse-key").unlink()
                second = self.attempt(fixture)
                self.assertFalse(second["reused"])
                self.assertNotEqual(second["attempt_id"], first["attempt_id"])
                self.assertEqual(fixture["count"].read_text(), "run\nrun\n")

    def test_failed_or_unknown_latest_attempt_cannot_revive_older_success(self) -> None:
        for latest in ("failed", "unknown", "backdated-failure"):
            with self.subTest(latest=latest), tempfile.TemporaryDirectory() as raw:
                fixture = self.fixture(Path(raw))
                first = self.attempt(fixture)
                failed = self.invoke(RUNNER, "--plan", str(fixture["plan"]),
                                     env={**os.environ, "FIXTURE_FAIL_BUILD": "1"})
                self.assertNotEqual(failed.returncode, 0)
                failed_state = Path(json.loads(failed.stdout)["state_path"])
                if latest == "unknown":
                    state = json.loads(failed_state.read_text())
                    state["status"] = "running"
                    state["rc"] = None
                    failed_state.write_text(json.dumps(state))
                elif latest == "backdated-failure":
                    state = json.loads(failed_state.read_text())
                    state["prepared_at"] = "1900-01-01T00:00:00Z"
                    failed_state.write_text(json.dumps(state))
                recovered = self.attempt(fixture)
                self.assertFalse(recovered["reused"])
                self.assertNotEqual(recovered["attempt_id"], first["attempt_id"])
                self.assertEqual(fixture["count"].read_text(), "run\nrun\nrun\n")

    def test_source_and_frozen_dependency_drift_reject_execution_and_reuse(self) -> None:
        for changed in ("source", "dependency", "executable"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as raw:
                fixture = self.fixture(Path(raw))
                self.attempt(fixture)
                if changed == "executable":
                    with fixture["executable"].open("ab") as handle:
                        handle.write(b"changed executable identity")
                else:
                    path = fixture["repo"] / "tracked.txt" if changed == "source" else fixture["toolchain"]
                    path.write_text("different input\n")
                result = self.invoke(RUNNER, "--plan", str(fixture["plan"]))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("drift", result.stderr)
                self.assertEqual(fixture["count"].read_text(), "run\n")

    def test_reuse_is_rejected_for_nonlocal_modes_or_incomplete_declarations(self) -> None:
        cases = [(mode, ["--reuse-evidence", "compile"], "evidence_reuse_mode_unsupported")
                 for mode in ("product-artifact", "publish", "diagnose")]
        cases.extend([
            ("validate", ["--reuse-evidence", "official-ut"], "evidence_reuse_inputs_incomplete"),
            ("validate", ["--evidence-output", "report=/tmp/unused-report"], "evidence_reuse_not_declared"),
        ])
        for mode, extra, expected in cases:
            with self.subTest(mode=mode, expected=expected), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                repo = root / "component"
                repo.mkdir()
                init_repo(repo)
                plan = root / "plan.json"
                created = self.invoke(CREATOR, "--mode", mode, "--workspace", f"component={repo}",
                                      "--cwd", str(repo), "--output", str(plan), *extra,
                                      "--", sys.executable, "-c", "print('local fixture')")
                self.assertNotEqual(created.returncode, 0)
                self.assertIn(expected, created.stderr)
                self.assertFalse(plan.exists())

    def test_ordinary_plans_keep_executing_each_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "component"
            repo.mkdir()
            init_repo(repo)
            plan = root / "plan.json"
            created = self.invoke(CREATOR, "--mode", "validate", "--workspace", f"component={repo}",
                                  "--cwd", str(repo), "--output", str(plan),
                                  "--", sys.executable, "-c", "print('local fixture')")
            self.assertEqual(created.returncode, 0, created.stderr)
            first = self.attempt({"plan": plan})
            second = self.attempt({"plan": plan})
            self.assertFalse(first["reused"])
            self.assertFalse(second["reused"])
            self.assertNotEqual(first["attempt_id"], second["attempt_id"])

    def test_evidence_outputs_cannot_overwrite_plan_or_private_attempt_records(self) -> None:
        for collision in ("plan.json", "plans/anything/state.json", "locks/key"):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                repo = root / "component"
                repo.mkdir()
                init_repo(repo)
                toolchain = root / "compiler.lock"
                toolchain.write_text("immutable compiler fixture")
                plan = root / "plan.json"
                created = self.invoke(CREATOR, "--mode", "validate", "--workspace", f"component={repo}",
                                      "--cwd", str(repo), "--output", str(plan), "--reuse-evidence", "compile",
                                      "--evidence-input", f"toolchain={toolchain}",
                                      "--evidence-output", f"result={root / collision}",
                                      "--", sys.executable, "-c", "print('local fixture')")
                self.assertNotEqual(created.returncode, 0)
                self.assertIn("evidence_output_overwrites_evidence", created.stderr)
                self.assertFalse(plan.exists())


if __name__ == "__main__":
    unittest.main()
