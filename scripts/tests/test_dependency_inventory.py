"""Keep cache verification byte-for-byte complete with a single directory walk."""
import hashlib
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
inventory = runpy.run_path(str(ROOT / 'plugin/openubmc/scripts/pluginctl.py'))['dependency_inventory']


class DependencyInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'cache'
        self.root.mkdir()

    def identity(self, path):
        value = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if sys.platform != 'win32':
            value['mode'] = path.stat().st_mode & 0o777
        return value

    def test_nested_files_modes_and_nested_receipts_are_all_covered(self):
        nested = self.root / 'node_modules/嵌套 space'
        nested.mkdir(parents=True)
        file = nested / 'module.js'
        file.write_bytes(b'verified bytes')
        file.chmod(0o500)
        receipt = nested / 'receipt.json'
        receipt.write_bytes(b'nested content is not a cache receipt')
        (self.root / 'receipt.json').write_bytes(b'root receipt is excluded')
        self.assertEqual(inventory(self.root), {
            'node_modules/嵌套 space/module.js': self.identity(file),
            'node_modules/嵌套 space/receipt.json': self.identity(receipt),
        })

    def test_same_size_and_timestamp_drift_is_detected_by_hash(self):
        file = self.root / 'module.js'
        file.write_bytes(b'first')
        before = inventory(self.root)
        times = file.stat()
        file.write_bytes(b'other')
        os.utime(file, ns=(times.st_atime_ns, times.st_mtime_ns))
        self.assertNotEqual(inventory(self.root), before)

    def test_a_directory_named_receipt_does_not_hide_its_contents(self):
        nested = self.root / 'receipt.json'
        nested.mkdir()
        file = nested / 'unlisted.js'
        file.write_bytes(b'not metadata')
        self.assertEqual(inventory(self.root), {'receipt.json/unlisted.js': self.identity(file)})

    def symlink(self, name, target, *, directory=False):
        path = self.root / name
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation is unavailable')
        return path

    def test_internal_links_are_recorded_without_following_them(self):
        nested = self.root / 'package'
        nested.mkdir()
        file = nested / 'module.js'
        file.write_bytes(b'package')
        self.symlink('linked-package', 'package', directory=True)
        self.symlink('linked-file', 'package/module.js')
        self.assertEqual(inventory(self.root), {
            'package/module.js': self.identity(file),
            'linked-package': {'link': 'package'},
            'linked-file': {'link': 'package/module.js'},
        })

    def test_escaping_directory_and_file_links_are_rejected(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (outside / 'module.js').write_bytes(b'outside')
        for name, target, directory in [('escape-dir', outside, True), ('escape-file', outside / 'module.js', False)]:
            with self.subTest(name=name):
                link = self.symlink(name, target, directory=directory)
                with self.assertRaisesRegex(ValueError, 'escapes its cache'):
                    inventory(self.root)
                link.unlink()


if __name__ == '__main__':
    unittest.main()
