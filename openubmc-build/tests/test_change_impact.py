"""Task-scoped impact analysis through the existing changed-component CLI."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/detect_changed_components.py"


class ImpactCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("interface", "consumer", "leaf", "unrelated"):
            component = self.root / name
            component.mkdir()
            (component / "conanfile.py").write_text(
                'raise RuntimeError("must not execute")\n'
            )
            (component / "mds").mkdir()
            (component / "mds/model.json").write_text("{}")
            (component / "src.lua").write_text("return 1\n")
            subprocess.run(["git", "init", "-q", str(component)], check=True)
            subprocess.run(["git", "-C", str(component), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(component),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
        (self.root / "dependencies.json").write_text(
            '{"consumer":["interface"],"leaf":["consumer"]}'
        )
        self.graph = {
            "schema": "openubmc.component-dependencies.v1",
            "complete": True,
            "components": {
                name: name for name in ("interface", "consumer", "leaf", "unrelated")
            },
            "edges": [
                {
                    "provider": "interface",
                    "consumer": "consumer",
                    "evidence_path": "dependencies.json",
                },
                {
                    "provider": "consumer",
                    "consumer": "leaf",
                    "evidence_path": "dependencies.json",
                },
            ],
            "dynamic_dependencies": [],
        }

    def analyze(self, *paths):
        graph = self.root / "graph.json"
        graph.write_text(json.dumps(self.graph))
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(self.root),
                "--impact",
                "--dependency-graph",
                str(graph),
                "--json",
                *[arg for path in paths for arg in ("--path", path)],
            ],
            capture_output=True,
            text=True,
        )

    def test_contract_edit_expands_proven_consumers_without_unrelated_dirt(self):
        (self.root / "interface/mds/model.json").write_text('{"changed":true}')
        (self.root / "unrelated/src.lua").write_text("return 99\n")
        result = self.analyze("interface/mds/model.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(
            [row["component"] for row in report["components"]],
            ["consumer", "interface", "leaf"],
        )
        self.assertEqual(report["changed_files"], ["interface/mds/model.json"])
        self.assertEqual(report["gaps"], [])
        self.assertEqual(
            {row["component"]: row["needs_generation"] for row in report["components"]},
            {"interface": True, "consumer": True, "leaf": True},
        )
        self.assertTrue(all(row["source"]["git_head"] for row in report["components"]))
        self.assertEqual(len(report["dependency_edges"]), 2)

    def test_incomplete_dynamic_cycle_and_unknown_paths_stay_explicit(self):
        self.graph["complete"] = False
        self.graph["dynamic_dependencies"] = ["leaf"]
        self.graph["edges"].append(
            {
                "provider": "leaf",
                "consumer": "interface",
                "evidence_path": "dependencies.json",
            }
        )
        result = self.analyze("interface/mds/model.json", "unknown.lua")
        self.assertEqual(result.returncode, 0, result.stderr)
        gaps = json.loads(result.stdout)["gaps"]
        self.assertIn("dependency_graph_incomplete", gaps)
        self.assertIn("dynamic_dependency:leaf", gaps)
        self.assertIn("dependency_cycle", gaps)
        self.assertIn("unmapped_path:unknown.lua", gaps)

    def test_local_code_edit_keeps_upstream_binding_without_expanding_consumers(self):
        report = json.loads(self.analyze("consumer/src.lua").stdout)
        self.assertEqual(
            [row["component"] for row in report["components"]], ["consumer"]
        )
        self.assertFalse(report["components"][0]["needs_generation"])
        self.assertEqual(set(report["components"][0]["dependencies"]), {"interface"})
        self.assertEqual(report["dependency_edges"][0]["provider"], "interface")

    def test_all_model_inputs_expand_consumers_not_only_named_model_files(self):
        (self.root / "interface/mds/resources.json").write_text("{}")
        report = json.loads(self.analyze("interface/mds/resources.json").stdout)
        self.assertEqual(
            [item["component"] for item in report["components"]],
            ["consumer", "interface", "leaf"],
        )

    def test_graph_inputs_are_bounded_and_invalid_types_fail_without_tracebacks(self):
        valid = self.graph
        for invalid in (
            [],
            {"schema": "openubmc.component-dependencies.v1", "components": []},
            {
                **valid,
                "edges": [
                    {
                        "provider": [],
                        "consumer": "leaf",
                        "evidence_path": "dependencies.json",
                    }
                ],
            },
        ):
            with self.subTest(graph=invalid):
                self.graph = invalid
                result = self.analyze("interface/mds/model.json")
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
        self.graph = valid
        (self.root / "dependencies.json").write_bytes(b"x" * (1024 * 1024 + 1))
        result = self.analyze("interface/mds/model.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("limit", result.stderr)

    def test_analysis_never_executes_repository_clean_filters(self):
        component = self.root / "interface"
        marker = self.root / "filter-executed"
        hook = self.root / "clean-filter.sh"
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\ncat\n')
        hook.chmod(0o700)
        (component / ".gitattributes").write_text("*.lua filter=fixture\n")
        subprocess.run(
            ["git", "-C", str(component), "config", "filter.fixture.clean", str(hook)],
            check=True,
        )
        result = self.analyze("interface/src.lua")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            marker.exists(), "read-only analysis executed a repository filter"
        )

    def test_analysis_never_executes_repository_fsmonitor(self):
        component = self.root / "interface"
        marker = self.root / "monitor-executed"
        hook = self.root / "monitor.sh"
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        hook.chmod(0o700)
        subprocess.run(
            ["git", "-C", str(component), "config", "core.fsmonitor", str(hook)],
            check=True,
        )
        result = self.analyze("interface/src.lua")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            marker.exists(), "read-only analysis executed a repository monitor"
        )


if __name__ == "__main__":
    unittest.main()
