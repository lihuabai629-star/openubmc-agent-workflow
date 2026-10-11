from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _source_catalog import SourceCatalog
from _workflow_source import search_source_terms
from _workflow_correlation import _implementation_alignment


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def repository(self, name, text="if AlarmId then emit_alarm(AlarmId) end\n"):
        repo = self.root / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q")
        (repo / "alarm.lua").write_text(text, encoding="utf-8", newline="\n")
        self.git(repo, "add", "alarm.lua")
        self.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "fixture")
        return repo

    def catalog(self, entries, product="board-a"):
        directory = self.root / ".openubmc"
        directory.mkdir(exist_ok=True)
        (directory / "source-catalog.json").write_text(json.dumps({
            "schema_version": 1, "product": product, "repositories": entries,
        }))

    def entry(self, path, **kwargs):
        return {"path": path, "origin": "internal", "component": "network_adapter",
                "role": "implementation", "products": ["board-a"], **kwargs}

    def search(self):
        return search_source_terms(str(self.root), ["AlarmId"], 30, 10)

    def test_mixed_roots_are_distinguished_and_community_remains_reference(self):
        community = self.repository("community/network")
        product = self.repository("product/network")
        self.git(community, "remote", "add", "origin", "https://fixture:secret@gitcode.com/openUBMC/network.git?token=fixture")
        self.catalog([self.entry("community/network", origin="community", role="reference"),
                      self.entry("product/network")])
        result = self.search()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["matches"]), 2)
        sources = {Path(m["source"]["repository"]): m["source"] for m in result["matches"]}
        self.assertEqual(sources[community]["applicability"], "reference")
        self.assertEqual(sources[product]["applicability"], "product_source_candidate")
        self.assertEqual(sources[community]["remote"], "gitcode.com/openUBMC/network")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("token=", json.dumps(result))
        paths, _ = _implementation_alignment(result["matches"])
        self.assertEqual(paths, ["product/network/alarm.lua"])
        self.assertFalse(sources[product]["modified"])
        self.assertFalse(sources[product]["proves_deployed_source"])

    def test_wrong_product_and_pinned_commit_are_reference_only(self):
        self.repository("other-product")
        self.repository("wrong-version")
        self.catalog([self.entry("other-product", products=["board-b"]),
                      self.entry("wrong-version", commit="0" * 40)])
        result = self.search()
        self.assertTrue(result["ok"])
        self.assertTrue(all(m["source"]["applicability"] == "reference" for m in result["matches"]))
        self.assertEqual(_implementation_alignment(result["matches"]), ([], []))

    def test_two_selected_implementations_do_not_silently_select_a_winner(self):
        self.repository("internal-one")
        self.repository("internal-two")
        self.catalog([self.entry("internal-one"), self.entry("internal-two")])
        result = self.search()
        self.assertTrue(all(m["source"]["ambiguous"] for m in result["matches"]))
        self.assertIn("multiple_implementations_for_component", result["provenance"]["warnings"])
        self.assertEqual(_implementation_alignment(result["matches"]), ([], []))

    def test_missing_or_invalid_catalog_does_not_block_search_or_guess_origin(self):
        self.repository("looks-like-community")
        for invalid in (False, True):
            if invalid:
                self.catalog([self.entry("../escape")])
            result = self.search()
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["matches"]), 1)
            self.assertEqual(result["matches"][0]["source"]["origin"], "unknown")
            self.assertEqual(result["matches"][0]["source"]["applicability"], "unknown")
            if invalid:
                self.assertIn("catalog_unavailable_or_invalid", result["provenance"]["warnings"])

    def test_worktree_changes_are_visible_without_running_clean_filters(self):
        repo = self.repository("internal")
        self.git(repo, "config", "filter.audit.clean", "touch filter-executed; cat")
        (repo / ".gitattributes").write_text("*.lua filter=audit\n")
        (repo / "alarm.lua").write_text("if AlarmId then emit_alarm(AlarmId) end -- changed\n", encoding="utf-8", newline="\n")
        self.catalog([self.entry("internal")])
        result = self.search()
        self.assertTrue(result["matches"][0]["source"]["modified"])
        self.assertTrue(result["matches"][0]["source"]["content_digest"].startswith("sha256:"))
        self.assertFalse((repo / "filter-executed").exists())

    def test_linked_worktree_uses_its_own_branch_and_commit(self):
        repo = self.repository("main")
        self.git(repo, "worktree", "add", "-qb", "product-branch", str(self.root / "linked"))
        self.catalog([self.entry("linked"), self.entry("main", role="reference")])
        result = self.search()
        linked = next(m for m in result["matches"] if m["path"].startswith("linked/"))
        self.assertEqual(linked["source"]["branch"], "product-branch")
        self.assertEqual(linked["source"]["applicability"], "product_source_candidate")

    def test_python_search_fallback_keeps_provenance(self):
        self.repository("internal")
        self.catalog([self.entry("internal")])
        with patch("_workflow_source.shutil.which", return_value=None):
            result = self.search()
        self.assertEqual(result["method"], "python")
        self.assertEqual(result["matches"][0]["source"]["origin"], "internal")

    def test_metadata_deadline_is_nonblocking_and_does_not_claim_a_revision(self):
        self.repository("internal")
        matches = [{"path": "internal/alarm.lua"}]
        result = SourceCatalog(self.root, timeout=0).annotate(matches)
        self.assertIn("provenance_time_limit", result["warnings"])
        self.assertIsNone(matches[0]["source"].get("commit"))
        self.assertEqual(matches[0]["source"]["applicability"], "unknown")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_nonregular_catalog_is_ignored_without_waiting_for_a_writer(self):
        directory = self.root / ".openubmc"
        directory.mkdir()
        os.mkfifo(directory / "source-catalog.json")
        result = SourceCatalog(self.root).annotate([])
        self.assertIn("catalog_unavailable_or_invalid", result["warnings"])

    def test_linked_source_is_not_claimed_as_a_product_file(self):
        repo = self.repository("internal")
        link = repo / "linked.lua"
        try:
            link.symlink_to(repo / "alarm.lua")
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks is unavailable")
        self.catalog([self.entry("internal")])
        match = {"path": "internal/linked.lua"}
        SourceCatalog(self.root).annotate([match])
        self.assertEqual(match["source"], {"applicability": "unknown", "reason": "source_reparse_path"})


if __name__ == "__main__":
    unittest.main()
