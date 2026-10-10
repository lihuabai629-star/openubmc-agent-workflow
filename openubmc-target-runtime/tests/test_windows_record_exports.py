"""Native Windows export ACL, idempotency and explicit retention acceptance."""
import os
from pathlib import Path
import tempfile
import time
import unittest

from openubmc_target_runtime.host_continuity import HostContinuity
from openubmc_target_runtime.record_export import RecordExportStore, export_task_records


@unittest.skipUnless(os.name == "nt", "requires native Windows ACL APIs")
class WindowsRecordExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        handoff = HostContinuity(self.root / "host", record_schema_version=2).handoff("task", read_run=lambda run: None)
        self.document = export_task_records(handoff, producer_commit="b" * 40)

    def test_export_creates_private_acl_and_identical_write_reuses_same_file(self):
        from openubmc_target_runtime.windows_private import verify_private_path
        store = RecordExportStore(self.root / "exports")
        path = store.write(self.document)
        verify_private_path(store.root)
        verify_private_path(path)
        self.assertEqual(store.write(self.document), path)
        self.assertEqual(path.stat().st_nlink, 1)

    def test_preview_preserves_export_and_apply_only_removes_qualified_file(self):
        store = RecordExportStore(self.root / "exports")
        path = store.write(self.document)
        other = store.root / "user.json"
        other.write_text("user document")
        cutoff = time.time() + 10
        self.assertEqual(store.prune(before_timestamp=cutoff), [path.name])
        self.assertTrue(path.exists())
        self.assertEqual(store.prune(before_timestamp=cutoff, dry_run=False), [path.name])
        self.assertFalse(path.exists())
        self.assertTrue(other.exists())

    def test_existing_shared_directory_is_rejected_without_repairing_it(self):
        from openubmc_target_runtime.windows_private import WindowsPrivateError
        target = self.root / "shared"
        target.mkdir()
        with self.assertRaises((ValueError, WindowsPrivateError)):
            RecordExportStore(target).write(self.document)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
