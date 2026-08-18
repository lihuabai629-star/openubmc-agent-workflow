from __future__ import annotations

import importlib.util
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
    def test_all_four_gates_are_required_for_promotion(self) -> None:
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
            )

        self.assertTrue(report["promotable"])
        self.assertEqual(
            [item["name"] for item in report["gates"]],
            ["clean_install", "upgrade", "rollback", "replay_smoke"],
        )
        self.assertTrue(all(item["status"] == "passed" for item in report["gates"]))
        self.assertEqual(len(calls), 5)

    def test_failure_blocks_later_gates_and_promotion(self) -> None:
        call_count = 0

        def fail_upgrade(command, *, cwd):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(
                command,
                19 if call_count == 3 else 0,
                "",
                "upgrade failed" if call_count == 3 else "",
            )

        with tempfile.TemporaryDirectory() as directory:
            report = release_gate.execute_release_gate(
                current_ref="v1.2.0",
                previous_ref="v1.1.1",
                workspace=Path.cwd(),
                work_root=Path(directory),
                executor=fail_upgrade,
            )

        self.assertFalse(report["promotable"])
        self.assertEqual(
            [item["status"] for item in report["gates"]],
            ["passed", "failed", "skipped", "skipped"],
        )
        self.assertEqual(call_count, 3)

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


if __name__ == "__main__":
    unittest.main()
