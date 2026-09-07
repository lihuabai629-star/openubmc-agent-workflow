from __future__ import annotations

import io
from pathlib import Path
import sys
import tarfile
import tempfile
import tracemalloc
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pull_bundle


class LogResourceBudgetTests(unittest.TestCase):
    def test_extraction_limits_members_and_total_bytes_without_partial_output(self):
        for limits in ({"max_members": 1}, {"max_bytes": 10}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "bundle.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    for name in ("first", "second"):
                        member = tarfile.TarInfo(f"dump_info/LogDump/{name}.log")
                        member.size = 8
                        archive.addfile(member, io.BytesIO(b"failure\n"))
                with self.assertRaises(pull_bundle.BundlePullError) as raised:
                    pull_bundle.extract_archive(archive_path, root / "output", **limits)
                self.assertEqual(raised.exception.code, "extract_budget_exceeded")
                self.assertEqual(list((root / "output").iterdir()), [])

    def test_analysis_retains_latest_evidence_with_bounded_candidate_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "dump_info/LogDump/app.log"
            path.parent.mkdir(parents=True)
            with path.open("w") as stream:
                for number in range(15000):
                    stream.write(f"login error {number}: " + "x" * 160 + "\n")
            reference = {"files": [{"name": "app.log", "paths": ["dump_info/LogDump/app.log"], "keywords": ["login"]}]}
            tracemalloc.start()
            try:
                result = pull_bundle.analyze_bundle(root, "login", reference_data=reference, max_lines=3)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual([line["line_number"] for line in result["selected_logs"][0]["evidence_lines"]], [15000, 14999, 14998])
            self.assertLess(peak, 2 * 1024 * 1024)

    def test_scan_budget_stops_gzip_reads_and_reports_incomplete_coverage(self):
        import gzip
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "app.log.gz"
            with gzip.open(path, "wb") as stream:
                stream.write(b"login error\n" * 10000)
            reference = {"files": [{"name": "app", "paths": ["app.log.gz"], "keywords": ["login"]}]}
            result = pull_bundle.analyze_bundle(root, "login", reference_data=reference, scan_max_bytes=24)
            self.assertFalse(result["coverage"]["complete"])
            self.assertIn("scan_bytes_exceeded", result["coverage"]["reasons"])
            self.assertEqual(result["coverage"]["scanned_bytes"], 24)
            self.assertEqual([line["line_number"] for line in result["selected_logs"][0]["evidence_lines"]], [2, 1])

    def test_file_and_discovery_limits_report_partial_analysis(self):
        for limits, reason in (({"scan_max_files": 2}, "scan_files_exceeded"),
                               ({"discovery_max_entries": 3}, "discovery_entries_exceeded")):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for number in range(20):
                    (root / f"app.log.{number}").write_text("login error\n")
                reference = {"files": [{"name": "app", "paths": ["app.log"], "keywords": ["login"]}]}
                result = pull_bundle.analyze_bundle(root, "login", reference_data=reference, **limits)
                self.assertFalse(result["coverage"]["complete"])
                self.assertIn(reason, result["coverage"]["reasons"])
                if "scan_max_files" in limits:
                    self.assertEqual(result["coverage"]["scanned_files"], 2)
                else:
                    self.assertLessEqual(result["coverage"]["discovered_entries"], 3)

    def test_candidate_memory_stays_bounded_across_rotated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for number in range(60):
                (root / f"app.log.{number}").write_text(("login error " + "x" * 16000 + "\n") * 3)
            reference = {"files": [{"name": "app", "paths": ["app.log"], "keywords": ["login"]}]}
            tracemalloc.start()
            try:
                result = pull_bundle.analyze_bundle(root, "login", reference_data=reference, max_lines=2)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertTrue(result["coverage"]["complete"])
            self.assertEqual(len(result["selected_logs"][0]["evidence_lines"]), 2)
            self.assertLess(peak, 1024 * 1024)

    def test_oversized_line_is_not_buffered_or_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.log").write_bytes(b"x" * (4 * 1024 * 1024))
            reference = {"files": [{"name": "app", "paths": ["app.log"], "keywords": ["login"]}]}
            tracemalloc.start()
            try:
                result = pull_bundle.analyze_bundle(root, "login", reference_data=reference, max_line_bytes=1024)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertFalse(result["coverage"]["complete"])
            self.assertIn("line_bytes_exceeded", result["coverage"]["reasons"])
            self.assertLess(peak, 256 * 1024)

    def test_corrupt_gzip_reports_read_failure_instead_of_empty_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.log.gz").write_bytes(b"invalid gzip")
            reference = {"files": [{"name": "app", "paths": ["app.log.gz"], "keywords": ["login"]}]}
            result = pull_bundle.analyze_bundle(root, "login", reference_data=reference)
            self.assertFalse(result["coverage"]["complete"])
            self.assertIn("file_read_failed", result["coverage"]["reasons"])

    def test_tar_extended_headers_have_a_stream_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "bundle.tar.gz"
            with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo("dump_info/app.log")
                member.pax_headers = {"comment": "x" * 100_000}
                archive.addfile(member)
            with self.assertRaises(pull_bundle.BundlePullError) as raised:
                pull_bundle.extract_archive(path, root / "output", max_stream_bytes=4096)
            self.assertEqual(raised.exception.code, "extract_budget_exceeded")
            self.assertEqual(list((root / "output").iterdir()), [])

    def test_corrupt_deflate_reports_partial_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.log.gz").write_bytes(bytes.fromhex("1f8b0800000000000003") + b"\x07" * 20)
            reference = {"files": [{"name": "app", "paths": ["app.log.gz"], "keywords": ["login"]}]}
            result = pull_bundle.analyze_bundle(root, "login", reference_data=reference)
            self.assertFalse(result["coverage"]["complete"])
            self.assertIn("file_read_failed", result["coverage"]["reasons"])

    def test_repeated_pax_headers_fail_cleanly_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "bundle.tar"
            with path.open("wb") as stream:
                first = tarfile.TarInfo("dump_info/first.log")
                first.size = 1
                stream.write(first.tobuf())
                stream.write(b"x" + b"\0" * 511)
                for _ in range(700):
                    stream.write(tarfile.TarInfo.create_pax_global_header({"comment": "fixture"}))
                stream.write(tarfile.TarInfo("dump_info/last.log").tobuf())
                stream.write(b"\0" * 1024)
            with self.assertRaises(pull_bundle.BundlePullError) as raised:
                pull_bundle.extract_archive(path, root / "output")
            self.assertEqual(raised.exception.code, "extract_budget_exceeded")
            self.assertEqual(list((root / "output").iterdir()), [])

    def test_recursive_discovery_handles_deep_trees_and_symlink_loops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            loop = root / "loop.log"
            loop.symlink_to("loop.log")
            reference = {"files": [{"name": "app", "paths": ["loop.log"], "keywords": ["login"]}]}
            result = pull_bundle.analyze_bundle(root, "login", reference_data=reference)
            self.assertFalse(result["coverage"]["complete"])
            directories = []
            current = root
            try:
                for _ in range(1050):
                    current = current / "d"
                    current.mkdir()
                    directories.append(current)
                (current / "app.log").write_text("login error\n")
                reference["files"][0]["paths"] = ["**/app.log"]
                result = pull_bundle.analyze_bundle(root, "login", reference_data=reference)
                self.assertEqual(len(result["selected_logs"][0]["evidence_lines"]), 1)
            finally:
                (current / "app.log").unlink(missing_ok=True)
                for directory in reversed(directories):
                    directory.rmdir()
