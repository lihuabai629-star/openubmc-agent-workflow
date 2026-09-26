from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from _workflow_source import navigate_source, search_source_terms


class SourceNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        self.index = self.base / "source-index.sqlite3"

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def repository(self, name, files):
        repo = self.root / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q")
        for relative, content in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git(repo, "add", ".")
        self.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "fixture")
        return repo

    def catalog(self):
        directory = self.root / ".openubmc"
        directory.mkdir(exist_ok=True)
        (directory / "source-catalog.json").write_text(json.dumps({
            "schema_version": 1, "product": "board-a",
            "repositories": [
                {"path": "a-community", "origin": "community", "component": "drive",
                 "products": ["board-a"], "role": "reference"},
                {"path": "b-other", "origin": "internal", "component": "drive",
                 "products": ["board-b"], "role": "implementation"},
                {"path": "z-product", "origin": "internal", "component": "drive",
                 "products": ["board-a"], "role": "implementation"},
            ],
        }))

    def fixtures(self):
        content = {
            "src/drive.lua": "function Drive.update()\n  emit_alarm(0xE001)\nend\n",
            "model/model.json": '{"path":"/com/openubmc/Drive","code":"0xE001"}\n',
            "src/drive.cpp": "void Drive::update() { report(0xE001); }\n",
        }
        repos = {name: self.repository(name, content) for name in
                 ("a-community", "b-other", "z-product")}
        self.catalog()
        return repos

    def navigate(self, query, **kwargs):
        return navigate_source(str(self.root), query, index_path=str(self.index),
                               timeout=10, **kwargs)

    def test_incremental_content_and_live_commit_dirty_identity(self):
        repos = self.fixtures()
        first = self.navigate("0xE001")
        self.assertEqual(first["index"]["updated_files"], 9)
        second = self.navigate("0xE001")
        self.assertEqual(second["index"]["updated_files"], 0)
        self.assertEqual(second["index"]["unchanged_files"], 9)
        product = repos["z-product"]
        original = next(item for item in second["results"] if item["path"].startswith("z-product/"))
        original_commit = original["source"]["commit"]
        original_digest = original["content_digest"]
        (product / "src/drive.lua").write_text("function Drive.update()\n  emit_alarm(0xE001) -- edited\nend\n")
        changed = self.navigate("0xE001")
        self.assertEqual(changed["index"]["updated_files"], 1)
        dirty = next(item for item in changed["results"] if item["path"] == "z-product/src/drive.lua")
        self.assertNotEqual(dirty["content_digest"], original_digest)
        self.assertEqual(dirty["source"]["commit"], original_commit)
        self.assertTrue(dirty["source"]["dirty"])
        self.assertEqual(dirty["source"]["dirty_scope"], "matched_file")
        self.git(product, "add", "src/drive.lua")
        self.git(product, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "edit")
        committed = self.navigate("0xE001")
        self.assertEqual(committed["index"]["updated_files"], 0)
        current = next(item for item in committed["results"] if item["path"] == "z-product/src/drive.lua")
        self.assertNotEqual(current["source"]["commit"], original_commit)
        self.assertFalse(current["source"]["dirty"])
        self.assertEqual(current["source"]["branch"], self.git(product, "branch", "--show-current"))

    def test_exact_error_mdb_path_and_function_choose_product_source(self):
        self.fixtures()
        for query in ("0xE001", "/com/openubmc/Drive", "Drive.update", "Drive::update"):
            with self.subTest(query=query):
                result = self.navigate(query)
                self.assertEqual(result["status"], "complete")
                self.assertTrue(result["results"][0]["path"].startswith("z-product/"))
                self.assertEqual(result["results"][0]["source"]["applicability"],
                                 "product_source_candidate")
                self.assertTrue(result["results"][0]["evidence_ref"].startswith("source:"))
                self.assertTrue(result["results"][0]["freshness"]["checked_at"])
                others = [item for item in result["results"] if item["path"].startswith("b-other/")]
                self.assertTrue(all(item["source"]["applicability"] == "reference" for item in others))

    def test_kb_fusion_deduplicates_and_outage_keeps_source(self):
        self.fixtures()
        failed = self.navigate("Drive update error", kb_receipt={"structuredContent": {
            "ok": False, "error": {"code": "KB_SERVICE_UNAVAILABLE"}}})
        self.assertEqual(failed["status"], "partial")
        self.assertEqual(failed["kb"]["code"], "KB_SERVICE_UNAVAILABLE")
        self.assertGreater(failed["source_count"], 0)
        receipt = {"structuredContent": {"ok": True, "result": {"references": [
            {"reference_id": "1", "file_path": "community/howto.md"},
            {"reference_id": "1", "file_path": "community/howto.md"},
            {"reference_id": "2", "file_path": "community/incident.md"},
        ]}}}
        fused = self.navigate("Drive update error", kb_receipt=receipt)
        self.assertEqual(fused["knowledge_count"], 2)
        self.assertEqual(fused["results"][0]["kind"], "source")
        self.assertTrue(all(item["applicability"] == "unverified" for item in fused["results"]
                            if item["kind"] == "knowledge_candidate"))
        self.assertFalse(any(item.get("source", {}).get("applicability") == "product_source_candidate"
                             for item in fused["results"] if item["kind"] == "knowledge_candidate"))

    def test_index_outage_uses_existing_search_with_provenance(self):
        self.fixtures()
        result = navigate_source(str(self.root), "0xE001", index_path=str(self.root), timeout=10)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["fallback"])
        self.assertIn(result["index"]["fallback_method"], {"rg", "python"})
        self.assertGreater(result["source_count"], 0)
        self.assertTrue(result["results"][0]["path"].startswith("z-product/"))

    def test_non_ascii_query_falls_back_and_deleted_file_is_removed(self):
        repos = self.fixtures()
        (repos["z-product"] / "src/drive.lua").write_text(
            "function Drive.update()\n  emit_alarm(0xE001) -- 故障恢复路径\nend\n")
        chinese = self.navigate("故障恢复路径")
        self.assertTrue(chinese["fallback"])
        self.assertTrue(any(item["path"] == "z-product/src/drive.lua" for item in chinese["results"]))
        self.navigate("Drive::update")
        (repos["z-product"] / "src/drive.cpp").unlink()
        deleted = self.navigate("Drive::update")
        self.assertEqual(deleted["index"]["removed_files"], 1)
        self.assertFalse(any(item["path"] == "z-product/src/drive.cpp" for item in deleted["results"]))

    def test_local_index_cache_is_private(self):
        self.fixtures()
        self.navigate("0xE001")
        self.assertEqual(os.stat(self.index).st_mode & 0o777, 0o600)

    def test_fixed_fixture_improves_correct_top_source_over_baseline(self):
        self.fixtures()
        queries = ("0xE001", "/com/openubmc/Drive", "Drive.update")
        baseline = []
        improved = []
        with patch("_workflow_source.shutil.which", return_value=None):
            for query in queries:
                result = search_source_terms(str(self.root), [query], 20, 10)
                baseline.append(bool(result["matches"]) and
                                result["matches"][0]["path"].startswith("z-product/"))
        for query in queries:
            result = self.navigate(query)
            improved.append(result["results"][0]["path"].startswith("z-product/"))
        self.assertEqual((sum(baseline), sum(improved)), (0, 3))

    def test_packaged_helper_query_entrypoint(self):
        self.fixtures()
        receipt = self.base / "kb-result.json"
        receipt.write_text(json.dumps({"structuredContent": {"ok": True, "result": {
            "references": [{"reference_id": "7", "file_path": "kb/drive.md"}]}}}))
        process = subprocess.run([
            sys.executable, str(SCRIPTS / "source_trace.py"), "--source-root", str(self.root),
            "--query", "Drive update error", "--index-path", str(self.index),
            "--kb-result-file", str(receipt), "--timeout", "10",
        ], check=True, capture_output=True, text=True)
        result = json.loads(process.stdout)
        self.assertEqual(result["schema"], "openubmc.source-navigation.v1")
        self.assertEqual(result["results"][0]["source"]["component"], "drive")
        self.assertEqual(result["knowledge_count"], 1)


if __name__ == "__main__":
    unittest.main()
