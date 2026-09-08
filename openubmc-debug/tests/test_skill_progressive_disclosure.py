from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")


def json_examples(markdown: str) -> list[object]:
    return [
        json.loads(block.split("```", 1)[0])
        for block in markdown.split("```json")[1:]
    ]


class SkillProgressiveDisclosureTests(unittest.TestCase):
    def test_entrypoint_is_small_and_keeps_the_core_decisions(self) -> None:
        self.assertLessEqual(len(SKILL.encode("utf-8")), 10 * 1024)
        self.assertLessEqual(len(SKILL.splitlines()), 180)
        for concept in (
            "observe",
            "execute",
            "same `observe` call",
            "two to four complementary",
            "source-only",
            "live-patch",
            "build-upgrade",
            "strongest evidenced owner",
            "closeout_markdown",
            "Keep all remote actions read-only",
        ):
            self.assertIn(concept, SKILL)

    def test_exact_observation_uses_the_stable_mcp_semantics(self) -> None:
        normalized = " ".join(SKILL.split())
        self.assertIn(
            "Use the default `openubmc-target-runtime` MCP through its semantic Agent Interface",
            normalized,
        )
        self.assertIn(
            "A narrow MDB or capability query should complete in one call",
            normalized,
        )
        self.assertIn("same `observe` call", normalized)
        self.assertIn("Do not run a separate capability preflight", normalized)
        self.assertNotIn("native MCP tool-call channel", normalized)
        self.assertNotIn("JavaScript or a local helper", normalized)
        self.assertNotIn("tools.openubmc_target_runtime_observe", normalized)

    def test_transport_and_runtime_mechanics_are_disclosed_on_demand(self) -> None:
        for detail in (
            "--mdb-concurrency",
            "TaskContext",
            "bounded LRU",
            "/bmc/kepler/Systems/1/Events",
            "workflow.advance",
            "phase_record",
        ):
            self.assertNotIn(detail, SKILL)
        self.assertIn("references/agent-gateway.md", SKILL)
        self.assertIn("references/remote-automation.md", SKILL)

    def test_every_packaged_reference_is_discoverable_once(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        references = {
            relative
            for relative in manifest["files"]
            if relative.startswith("references/")
        }
        self.assertTrue(references)
        for relative in references:
            self.assertEqual(SKILL.count(relative), 1, relative)

    def test_reference_documents_do_not_create_second_hop_routing(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        packaged_names = {
            Path(relative).name
            for relative in manifest["files"]
            if relative.startswith("references/")
        }
        for path in (ROOT / "references").glob("*.md"):
            content = path.read_text(encoding="utf-8")
            second_hops = sorted(
                name for name in packaged_names - {path.name} if name in content
            )
            self.assertEqual(second_hops, [], path.name)

    def test_remote_automation_uses_only_the_semantic_resume_path(self) -> None:
        content = (ROOT / "references" / "remote-automation.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("execute(kind=resume)", content)
        self.assertNotIn("Compatibility `debug_collect`", content)
        for retired in ("phase_record", "workflow.advance", "workflow.next"):
            self.assertNotIn(retired, content)

    def test_agent_gateway_reference_matches_actionable_preflight_contract(self) -> None:
        content = (ROOT / "references" / "agent-gateway.md").read_text(
            encoding="utf-8"
        )
        examples = json_examples(content)
        observe = next(example for example in examples if 'selectors' in example)
        resume = next(example for example in examples if example.get('kind') == 'resume')
        no_progress = next(example for example in examples if 'response_required' in example)
        reconcile = next(example for example in examples if example.get('command') == 'reconcile')
        artifact = next(example for example in examples
                        if 'artifact_ref' in example.get('response', {}).get('payload', {}))

        self.assertEqual(
            observe["selectors"][0]["names"],
            ["ssh", "mdbctl"],
        )
        self.assertEqual(resume["deadline"], 120)
        self.assertTrue(no_progress["response_required"])
        self.assertEqual(no_progress["progress"]["status"], "no_progress")
        self.assertEqual(reconcile["command"], "reconcile")
        self.assertEqual(reconcile["run_id"], "<current Run ID>")
        self.assertIn("artifact_ref", artifact["response"]["payload"])


if __name__ == "__main__":
    unittest.main()
