from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runtime_qualification.py"
SPEC = importlib.util.spec_from_file_location("runtime_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class RuntimeQualificationTests(unittest.TestCase):
    def test_all_safety_qualifications_must_pass_with_zero_violations(self) -> None:
        calls: list[tuple[str, ...]] = []

        def succeed(command, *, cwd):
            self.assertTrue(cwd.is_dir())
            calls.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        report = qualification.qualify_runtime(Path.cwd(), executor=succeed)

        self.assertTrue(report["promotable"])
        self.assertEqual(
            report["violations"],
            {
                "duplicate_dangerous_effects": 0,
                "false_successes": 0,
                "wrong_target_or_artifact_mutations": 0,
                "unknown_new_identity_retries": 0,
            },
        )
        self.assertTrue(report["ordinary_partial_result_accepted"])
        self.assertEqual(len(calls), 5)

    def test_failed_safety_qualification_blocks_promotion(self) -> None:
        call_count = 0

        def fail_second(command, *, cwd):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(
                command,
                7 if call_count == 2 else 0,
                "",
                "qualified invariant failed" if call_count == 2 else "",
            )

        report = qualification.qualify_runtime(Path.cwd(), executor=fail_second)

        self.assertFalse(report["promotable"])
        self.assertGreater(report["violations"]["false_successes"], 0)
        self.assertEqual(call_count, 5)


if __name__ == "__main__":
    unittest.main()
