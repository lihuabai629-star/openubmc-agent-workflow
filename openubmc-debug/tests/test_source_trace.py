from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "source_trace.py"


class SourceTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def trace(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--source-root", str(self.root),
             "--symbol", "update", *args],
            text=True, capture_output=True, check=True,
        )
        return json.loads(proc.stdout)

    def test_reports_direct_caller_and_declaration_with_source_identity(self) -> None:
        self.write("src/drive.lua", "local function update()\nend\nlocal function scan()\n  update()\nend\n")
        result = self.trace()
        self.assertEqual(result["status"], "references_available")
        self.assertEqual(
            [(r["kind"], r["line"], r.get("caller")) for r in result["references"]],
            [("declaration", 1, None), ("call_candidate", 4, "scan")],
        )
        self.assertTrue(result["source"]["snapshot_digest"].startswith("sha256:"))
        self.assertFalse(result["proves_runtime_execution"])
        self.assertEqual(result["references"][1]["resolution"], "static_reference")

    def test_source_snapshot_is_commit_and_content_bound(self) -> None:
        self.write("drive.lua", "function update() end\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "drive.lua"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "fixture"], check=True)
        original = self.trace()
        self.write("drive.lua", "function update()\n  return 1\nend\n")
        changed = self.trace()
        self.assertEqual(original["source"]["revision"], changed["source"]["revision"])
        self.assertFalse(original["source"]["dirty"])
        self.assertTrue(changed["source"]["dirty"])
        self.assertNotEqual(original["source"]["snapshot_digest"], changed["source"]["snapshot_digest"])

    def test_ignores_comments_and_strings_and_preserves_nested_caller(self) -> None:
        self.write("src/drive.lua", """-- function update() end
local note = "update()"
local other = [=[function update() update() end]=]
--[==[
update()
]==]
local function scan()
  if ready then
    update()
  end
  update()
end
""")
        result = self.trace()
        self.assertEqual([(r["line"], r["caller"]) for r in result["references"]],
                         [(9, "scan"), (11, "scan")])

    def test_registration_and_duplicate_names_remain_unresolved_candidates(self) -> None:
        self.write("src/a.lua", "function Drive.update() end\nregistry:register('drive', Drive.update)\nDrive.update()\n")
        self.write("generated/b.lua", "function Other.update() end\n")
        result = self.trace()
        self.assertEqual([r["kind"] for r in result["references"]],
                         ["declaration", "declaration", "registration_candidate", "call_candidate"])
        self.assertTrue(result["references"][0]["generated"])
        self.assertIn("ambiguous_declarations", [g["code"] for g in result["gaps"]])
        registration = result["references"][2]
        self.assertEqual(registration["registrar"], "registry:register")
        self.assertEqual(registration["symbol"], "Drive.update")
        self.assertFalse(result["proves_runtime_execution"])

    def test_reports_limits_unsupported_sources_and_external_symlinks_as_gaps(self) -> None:
        self.write("a.lua", "function update() end\nupdate()\nupdate()\n")
        self.write("b.c", "void update(void) {}\n")
        self.write("c.lua", "--" + "x" * 200 + "\nupdate()\n")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "private.lua"
            target.write_text("function update() end\n")
            (self.root / "linked.lua").symlink_to(target)
            result = self.trace("--max-matches", "1", "--max-file-bytes", "128")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["references"]), 1)
        self.assertTrue(result["truncated"])
        self.assertTrue({"match_limit", "unsupported_source", "file_byte_limit", "symlink_skipped"}
                        <= {g["code"] for g in result["gaps"]})
        self.assertNotIn("private.lua", json.dumps(result))

    def test_file_count_limit_never_claims_no_references(self) -> None:
        self.write("a.lua", "local n = 1\n")
        self.write("b.lua", "update()\n")
        result = self.trace("--max-files", "1")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["references"], [])
        self.assertIn("file_count_limit", [g["code"] for g in result["gaps"]])

    def test_aliases_remain_unresolved_and_qualified_queries_are_exact(self) -> None:
        self.write("a.lua", "function Drive.update() end\nlocal callback = Drive.update\ncallback()\nOther.update()\n")
        result = self.trace("--symbol", "Drive.update")
        self.assertEqual([(r["kind"], r["symbol"]) for r in result["references"]],
                         [("declaration", "Drive.update"), ("value_reference", "Drive.update")])
        self.assertIn("unresolved_dispatch", [g["code"] for g in result["gaps"]])

    def test_source_search_points_to_trace_only_for_symbol_queries(self) -> None:
        self.write("a.lua", "function update() end\n")
        code = """import json,sys
sys.path.insert(0,sys.argv[1])
from _workflow_source import search_source_terms
print(json.dumps(search_source_terms(sys.argv[2], ['update', 'Drive failure text'], 10, 2)))
"""
        proc = subprocess.run([sys.executable, "-c", code, str(SCRIPT.parent), str(self.root)],
                              check=True, capture_output=True, text=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["trace_candidates"], [{"symbol": "update", "helper": "source_trace.py"}])

    def test_git_metadata_does_not_execute_a_configured_project_monitor(self) -> None:
        self.write("a.lua", "update()\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("monitor", "#!/bin/sh\ntouch monitor-executed\n")
        (self.root / "monitor").chmod(0o755)
        subprocess.run(["git", "-C", str(self.root), "config", "core.fsmonitor", str(self.root / "monitor")], check=True)
        self.trace()
        self.assertFalse((self.root / "monitor-executed").exists())

    def test_git_metadata_never_executes_clean_filters(self) -> None:
        self.write("a.lua", "function update() return 1 end\n")
        self.write(".gitattributes", "*.lua filter=fixture\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "filter.fixture.clean",
                        "sh -c 'touch filter-executed; cat'"], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "a.lua", ".gitattributes"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "fixture"], check=True)
        (self.root / "filter-executed").unlink(missing_ok=True)
        self.write("a.lua", "function update() return 2 end\n")
        result = self.trace()
        self.assertFalse((self.root / "filter-executed").exists())
        self.assertTrue(result["source"]["dirty"])

    def test_dynamic_member_calls_are_explicitly_unresolved(self) -> None:
        self.write("drive.lua", 'Drive["update"]()\n')
        result = self.trace()
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unresolved_dispatch", [g["code"] for g in result["gaps"]])

    def test_parsing_large_last_file_honors_time_budget(self) -> None:
        self.write("drive.lua", "update()\n" * 110000)
        result = self.trace("--max-file-bytes", "1048576", "--timeout", "0.001")
        self.assertIn("time_limit", [g["code"] for g in result["gaps"]])
        self.assertEqual(result["status"], "incomplete")

    def test_marks_openubmc_gen_output(self) -> None:
        self.write("gen/drive.lua", "function update() end\n")
        self.assertTrue(self.trace()["references"][0]["generated"])


if __name__ == "__main__":
    unittest.main()
