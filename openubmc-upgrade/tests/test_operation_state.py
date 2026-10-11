from __future__ import annotations

from pathlib import Path
import os
import stat
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-upgrade"))

from openubmc_upgrade.operation_state import UpgradeOperationStateStore  # noqa: E402


class UpgradeOperationStateStoreTests(unittest.TestCase):
    def test_state_is_durable_private_and_identity_fields_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = UpgradeOperationStateStore(Path(raw))
            store.initialize(
                task_id="task-1",
                operation_id="operation-1",
                protocol="webui",
                verification_mode="task-completion",
                artifact_file_name="component.hpm",
                mutation_fingerprint="fingerprint-1",
            )
            updated = store.update(
                task_id="task-1",
                operation_id="operation-1",
                values={
                    "baseline_task_identities": [
                        ["task", "component", "component.hpm", "", "1.0"]
                    ],
                    "upload_accepted": True,
                },
            )
            loaded = store.load("task-1", "operation-1")

            self.assertEqual(updated, loaded)
            self.assertEqual(loaded["protocol"], "webui")
            self.assertTrue(loaded["upload_accepted"])
            if os.name == "nt":
                from openubmc_target_runtime.windows_private import verify_private_path
                verify_private_path(store.root)
                verify_private_path(store._path("task-1", "operation-1"))
            else:
                mode = stat.S_IMODE(store.root.stat().st_mode)
                self.assertEqual(mode & 0o077, 0)
            with self.assertRaisesRegex(RuntimeError, "immutable field protocol"):
                store.update(
                    task_id="task-1",
                    operation_id="operation-1",
                    values={"protocol": "redfish"},
                )


if __name__ == "__main__":
    unittest.main()
