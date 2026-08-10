from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "behavior_eval_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_behavior_eval", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load behavior evaluation runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BehaviorEvalRunnerTests(unittest.TestCase):
    def test_message_patterns_can_start_after_the_target_skill_read(self) -> None:
        runner = load_runner()
        case = {
            "id": "source-checkout-message-scope",
            "expect": {
                "workspace_changes": [],
                "messages": {
                    "scope": "after_target_skill_read",
                    "required_patterns": ["done"],
                    "forbidden_patterns": ["plan"],
                },
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "agent_messages": ["plan before reading", "done"],
            "agent_messages_after_target_skill_read": ["done"],
            "skills_read": [],
            "references_read": [],
            "commands": [],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertTrue(assessment["case_pass"], assessment["failures"])
        self.assertEqual(assessment["agent_message_count"], 2)

    def test_assessment_enforces_message_and_command_efficiency_limits(self) -> None:
        runner = load_runner()
        case = {
            "id": "lightweight-change",
            "expect": {
                "workspace_changes": [],
                "max_commands": 1,
                "max_agent_messages": 1,
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "agent_messages": ["开始。", "完成。"],
            "skills_read": [],
            "references_read": [],
            "commands": ["sed -n 1,20p file", "git diff -- file"],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertFalse(assessment["case_pass"])
        self.assertIn("too_many_commands:2>1", assessment["failures"])
        self.assertIn("too_many_agent_messages:2>1", assessment["failures"])
        self.assertEqual(assessment["command_count"], 2)
        self.assertEqual(assessment["agent_message_count"], 2)

    def test_assessment_can_reject_exact_command_repeats_without_a_count_limit(self) -> None:
        runner = load_runner()
        case = {
            "id": "no-repeat-probes",
            "expect": {
                "workspace_changes": [],
                "forbid_exact_command_repeats": True,
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "agent_messages": [],
            "skills_read": [],
            "references_read": [],
            "commands": [
                "command -v lua",
                "sed -n '1,20p' src/main.lua",
                "command   -v   lua",
            ],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertFalse(assessment["case_pass"])
        self.assertIn("repeated_command:command -v lua", assessment["failures"])

    def test_assessment_rejects_repeated_reads_of_unchanged_context(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "src").mkdir()
            (workspace / "docs").mkdir()
            (workspace / "src/main.lua").write_text("return 2\n", encoding="utf-8")
            (workspace / "docs/contract.md").write_text(
                "main returns two\n", encoding="utf-8"
            )
            case = {
                "id": "no-redundant-context-reads",
                "expect": {
                    "workspace_changes": ["src/main.lua"],
                    "forbid_redundant_unchanged_reads": True,
                },
            }
            trace = {
                "returncode": 0,
                "timed_out": False,
                "workspace_changes": ["src/main.lua"],
                "agent_messages": [],
                "skills_read": [],
                "references_read": [],
                "commands": [
                    "sed -n '1,40p' docs/contract.md",
                    "nl -ba src/main.lua",
                    "nl -ba docs/contract.md",
                    "sed -n '1,40p' src/main.lua",
                ],
                "command_results": [
                    {
                        "command": "sed -n '1,40p' docs/contract.md",
                        "status": "completed",
                        "exit_code": 0,
                    },
                    {
                        "command": "nl -ba src/main.lua",
                        "status": "completed",
                        "exit_code": 0,
                    },
                    {
                        "command": "nl -ba docs/contract.md",
                        "status": "completed",
                        "exit_code": 0,
                    },
                    {
                        "command": "sed -n '1,40p' src/main.lua",
                        "status": "completed",
                        "exit_code": 0,
                    },
                ],
                "errors": [],
                "post_checks": [],
            }

            assessment = runner.assess_trace(case, trace, workspace)

        self.assertFalse(assessment["case_pass"])
        self.assertIn(
            "redundant_unchanged_file_read:docs/contract.md",
            assessment["failures"],
        )
        self.assertNotIn(
            "redundant_unchanged_file_read:src/main.lua",
            assessment["failures"],
        )

    def test_rg_file_discovery_is_not_counted_as_a_content_read(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "AGENTS.md").write_text("rules\n", encoding="utf-8")

            paths = runner.workspace_file_read_paths(
                "rg --files -g AGENTS.md | sed -n '1,20p'", workspace
            )

        self.assertEqual(paths, ())

    def test_failed_reader_command_does_not_create_false_repeat_evidence(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "docs").mkdir()
            (workspace / "docs/contract.md").write_text(
                "contract\n", encoding="utf-8"
            )
            case = {
                "id": "failed-read-is-not-evidence",
                "expect": {
                    "workspace_changes": [],
                    "forbid_redundant_unchanged_reads": True,
                },
            }
            trace = {
                "returncode": 0,
                "timed_out": False,
                "workspace_changes": [],
                "agent_messages": [],
                "skills_read": [],
                "references_read": [],
                "commands": [
                    "sed -n '1,20p' missing.md && sed -n '1,20p' docs/contract.md",
                    "sed -n '1,20p' docs/contract.md",
                ],
                "command_results": [
                    {
                        "command": "sed -n '1,20p' missing.md && sed -n '1,20p' docs/contract.md",
                        "status": "failed",
                        "exit_code": 2,
                    },
                    {
                        "command": "sed -n '1,20p' docs/contract.md",
                        "status": "completed",
                        "exit_code": 0,
                    },
                ],
                "errors": [],
                "post_checks": [],
            }

            assessment = runner.assess_trace(case, trace, workspace)

        self.assertTrue(assessment["case_pass"], assessment["failures"])

    def test_assessment_rejects_new_discovery_after_successful_evidence(self) -> None:
        runner = load_runner()
        case = {
            "id": "stop-after-focused-validation",
            "expect": {
                "workspace_changes": [],
                "forbid_discovery_after_evidence": True,
                "command_evidence": [
                    {
                        "command_pattern": r"python3 tests/verify.py",
                        "output_pattern": "verified",
                    }
                ],
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "agent_messages": [],
            "skills_read": [],
            "references_read": [],
            "commands": [
                "python3 tests/verify.py",
                "git diff -- src/main.lua",
                "command -v lua",
            ],
            "command_results": [
                {
                    "command": "python3 tests/verify.py",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "verified\n",
                },
                {
                    "command": "git diff -- src/main.lua",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "",
                },
                {
                    "command": "command -v lua",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "/usr/bin/lua\n",
                },
            ],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertFalse(assessment["case_pass"])
        self.assertTrue(
            any(
                failure.startswith("discovery_after_evidence:command -v lua")
                for failure in assessment["failures"]
            ),
            assessment["failures"],
        )

    def test_claimed_read_only_run_fails_when_workspace_changed(self) -> None:
        runner = load_runner()
        case = {
            "id": "analysis-only",
            "expect": {"workspace_changes": []},
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": ["src/main.lua"],
            "agent_messages": ["已完成只读分析，没有修改文件。"],
            "skills_read": [],
            "references_read": [],
            "commands": [],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertFalse(assessment["case_pass"])
        self.assertEqual(
            assessment["failures"],
            ["unexpected_workspace_change:src/main.lua"],
        )

    def test_workspace_snapshot_reports_observable_file_changes(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "main.lua"
            source.parent.mkdir()
            source.write_text("return 1\n", encoding="utf-8")
            before = runner.snapshot_tree(root)

            source.write_text("return 2\n", encoding="utf-8")
            added = root / "test" / "main_spec.lua"
            added.parent.mkdir()
            added.write_text("assert(true)\n", encoding="utf-8")
            after = runner.snapshot_tree(root)

        self.assertEqual(
            runner.diff_snapshots(before, after),
            ["src/main.lua", "test/main_spec.lua"],
        )

    def test_workspace_snapshot_detects_file_mode_changes(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "verify.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o644)
            before = runner.snapshot_tree(root)

            script.chmod(0o755)
            after = runner.snapshot_tree(root)

        self.assertEqual(runner.diff_snapshots(before, after), ["verify.sh"])

    def test_event_parser_uses_observed_reads_not_agent_claims(self) -> None:
        runner = load_runner()
        events = "\n".join(
            [
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "command": "sed -n '1,80p' /skills/openubmc-developer/SKILL.md",
                            "aggregated_output": "---\nname: openubmc-developer\n---",
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "command": (
                                "sed -n '1,80p' "
                                "/skills/openubmc-developer/references/lua-component.md"
                            ),
                            "aggregated_output": "# Lua",
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "agent_message",
                            "text": "我还读取了另一个 Skill 和 persistence reference。",
                        }
                    }
                ),
            ]
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["skills_read"], ["openubmc-developer"])
        self.assertEqual(
            trace["skill_reads"],
            [
                {
                    "name": "openubmc-developer",
                    "path": "/skills/openubmc-developer/SKILL.md",
                    "resolved_path": "/skills/openubmc-developer/SKILL.md",
                }
            ],
        )
        self.assertEqual(
            trace["skill_paths_read"],
            ["/skills/openubmc-developer/SKILL.md"],
        )
        self.assertEqual(
            trace["references_read"],
            ["references/lua-component.md"],
        )

    def test_event_parser_counts_common_readers_and_spaced_skill_paths(self) -> None:
        runner = load_runner()
        events = "\n".join(
            [
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": (
                                "nl -ba '/skills/Open UBMC/"
                                "openubmc-developer/SKILL.md'"
                            ),
                            "aggregated_output": (
                                "---\nname: openubmc-developer\n---"
                            ),
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": (
                                "awk 'NR <= 20 {print}' /skills/"
                                "openubmc-developer/references/lua-component.md"
                            ),
                            "aggregated_output": "# Handwritten Lua Components",
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": (
                                "python3 tools/read_text.py /skills/"
                                "openubmc-developer/references/mdb-mds.md"
                            ),
                            "aggregated_output": "# MDB, MDS, and Modeled Contracts",
                        }
                    }
                ),
            ]
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["skills_read"], ["openubmc-developer"])
        self.assertEqual(
            trace["skill_paths_read"],
            ["/skills/Open UBMC/openubmc-developer/SKILL.md"],
        )
        self.assertEqual(
            trace["references_read"],
            ["references/lua-component.md", "references/mdb-mds.md"],
        )

    def test_event_parser_separates_nonfatal_budget_warning_and_deduplicates_commands(self) -> None:
        runner = load_runner()
        command = "sed -n '1,80p' /skills/openubmc-developer/SKILL.md"
        events = "\n".join(
            [
                json.dumps(
                    {
                        "item": {
                            "type": "error",
                            "message": (
                                "Skill descriptions were shortened to fit the skills "
                                "context budget warning."
                            ),
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "in_progress",
                            "command": command,
                            "aggregated_output": "",
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "command": command,
                            "aggregated_output": "---\nname: openubmc-developer\n---",
                        }
                    }
                ),
            ]
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["warnings"], [
            "Skill descriptions were shortened to fit the skills context budget warning."
        ])
        self.assertEqual(trace["errors"], [])
        self.assertEqual(trace["commands"], [command])

    def test_event_parser_records_top_level_transport_errors_separately(self) -> None:
        runner = load_runner()
        transport_message = (
            "Reconnecting... 1/5 (stream disconnected before completion: "
            "idle timeout waiting for SSE)"
        )
        events = "\n".join(
            [
                json.dumps({"type": "error", "message": transport_message}),
                json.dumps({"type": "error", "message": "non-transport failure"}),
            ]
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["transport_events"], [transport_message])
        self.assertEqual(trace["errors"], ["non-transport failure"])

    def test_event_parser_preserves_external_reference_owner(self) -> None:
        runner = load_runner()
        events = json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "status": "completed",
                    "command": (
                        "cat /skills/openubmc-build/references/handoff-contract.md"
                    ),
                    "aggregated_output": "# Build handoff",
                }
            }
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(
            trace["references_read"],
            ["openubmc-build:references/handoff-contract.md"],
        )

    def test_event_parser_normalizes_reference_below_observed_checkout_root(
        self,
    ) -> None:
        runner = load_runner()
        checkout = "/home/workspace/.codex-openubmc-developer-edit"
        events = "\n".join(
            [
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": (
                                f'/bin/bash -lc "cat {checkout}/SKILL.md"'
                            ),
                            "aggregated_output": (
                                "---\nname: openubmc-developer\n---"
                            ),
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": (
                                f'/bin/bash -lc "awk \'{{print}}\' {checkout}/references/'
                                'persistence-compatibility.md"'
                            ),
                            "aggregated_output": "# Persistence Compatibility",
                        }
                    }
                ),
            ]
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(
            trace["references_read"],
            ["references/persistence-compatibility.md"],
        )

    def test_reference_path_echo_is_not_counted_as_a_read(self) -> None:
        runner = load_runner()
        reference = "/skills/openubmc-developer/references/lua-component.md"
        events = json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "status": "completed",
                    "exit_code": 0,
                    "command": f"echo {reference}",
                    "aggregated_output": reference,
                }
            }
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["references_read"], [])

    def test_event_parser_uses_authoritative_target_path_when_output_is_suppressed(
        self,
    ) -> None:
        runner = load_runner()
        target = Path("/candidate/.codex-openubmc-developer-edit/SKILL.md")
        events = "\n".join(
            [
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": f"sed -n '1,260p' {target}",
                            "aggregated_output": "",
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "agent_message",
                            "text": "HEAD 7bb4af7",
                        }
                    }
                ),
            ]
        )

        trace = runner.parse_event_stream(events, target)

        self.assertEqual(trace["skills_read"], ["openubmc-developer"])
        self.assertEqual(
            trace["agent_messages_after_target_skill_read"], ["HEAD 7bb4af7"]
        )

    def test_successful_reference_read_counts_when_event_output_is_suppressed(self) -> None:
        runner = load_runner()
        target = Path("/candidate/openubmc-developer/SKILL.md")
        reference = target.parent / "references" / "downstream-handoffs.md"
        events = "\n".join(
            [
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": f"sed -n '1,260p' {target}",
                            "aggregated_output": "",
                        }
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                            "exit_code": 0,
                            "command": f"sed -n '1,240p' {reference}",
                            "aggregated_output": "",
                        }
                    }
                ),
            ]
        )

        trace = runner.parse_event_stream(events, target)

        self.assertEqual(
            trace["references_read"], ["references/downstream-handoffs.md"]
        )

    def test_failed_reference_command_is_observed_but_not_counted_as_a_read(self) -> None:
        runner = load_runner()
        command = "cat /skills/openubmc-developer/references/lua-component.md"
        events = json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "status": "failed",
                    "exit_code": 1,
                    "command": command,
                    "aggregated_output": "permission denied",
                }
            }
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["commands"], [command])
        self.assertEqual(trace["references_read"], [])
        self.assertEqual(len(trace["command_failures"]), 1)

    def test_successful_skill_read_command_counts_when_event_output_is_suppressed(self) -> None:
        runner = load_runner()
        events = json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "status": "completed",
                    "exit_code": 0,
                    "command": (
                        "sed -n '1,200p' /skills/openubmc-developer/SKILL.md"
                    ),
                    "aggregated_output": "",
                }
            }
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["skills_read"], ["openubmc-developer"])
        self.assertEqual(
            trace["skill_paths_read"],
            ["/skills/openubmc-developer/SKILL.md"],
        )

    def test_event_parser_distinguishes_same_named_skill_paths(self) -> None:
        runner = load_runner()
        events = "\n".join(
            json.dumps(
                {
                    "item": {
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                        "command": f"cat {path}",
                        "aggregated_output": "---\nname: openubmc-developer\n---",
                    }
                }
            )
            for path in (
                "/candidate/openubmc-developer/SKILL.md",
                "/release/openubmc-developer/SKILL.md",
            )
        )

        trace = runner.parse_event_stream(events)

        self.assertEqual(trace["skills_read"], ["openubmc-developer"])
        self.assertEqual(
            trace["skill_paths_read"],
            [
                "/candidate/openubmc-developer/SKILL.md",
                "/release/openubmc-developer/SKILL.md",
            ],
        )

    def test_evaluation_prompt_pins_only_behavior_mode(self) -> None:
        runner = load_runner()
        root = Path("/candidate/openubmc-developer")

        behavior = runner.evaluation_prompt("do work", root, "behavior")
        trigger = runner.evaluation_prompt("do work", root, "trigger")

        self.assertIn("/candidate/openubmc-developer/SKILL.md", behavior)
        self.assertIn("Use $openubmc-developer from", behavior)
        self.assertIn("ignore other openubmc-developer copies", behavior)
        self.assertNotIn("/candidate/openubmc-developer/SKILL.md", trigger)
        self.assertIn("Select Skills normally from the installed catalog", trigger)
        self.assertNotIn("evaluation", behavior.lower())
        self.assertNotIn("evaluation", trigger.lower())
        self.assertNotIn("expected results", behavior.lower())
        self.assertNotIn("expected results", trigger.lower())

    def test_load_cases_validates_ids_and_workspace_paths(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "skill_name": "openubmc-developer",
                        "evals": [
                            {
                                "id": "unsafe",
                                "prompt": "analyze",
                                "workspace": {"files": {"../escape": "bad"}},
                                "expect": {"workspace_changes": []},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "workspace path"):
                runner.load_cases(path, None)

    def test_load_cases_rejects_git_metadata_overlays(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "skill_name": "openubmc-developer",
                        "evals": [
                            {
                                "id": "unsafe-git",
                                "prompt": "analyze",
                                "workspace": {
                                    "git": {
                                        "dirty_files": {".git/config": "bad"}
                                    }
                                },
                                "expect": {"workspace_changes": []},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Git metadata"):
                runner.load_cases(path, None)

    def test_load_cases_rejects_invalid_case_timeout(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "skill_name": "openubmc-developer",
                        "evals": [
                            {
                                "id": "bad-timeout",
                                "prompt": "analyze",
                                "timeout": 0,
                                "expect": {"workspace_changes": []},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "timeout"):
                runner.load_cases(path, None)

    def test_load_cases_rejects_invalid_empty_output_policy(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "skill_name": "openubmc-developer",
                        "evals": [
                            {
                                "id": "bad-evidence-policy",
                                "prompt": "analyze",
                                "expect": {
                                    "workspace_changes": [],
                                    "command_evidence": [
                                        {
                                            "command_pattern": "verify",
                                            "output_pattern": "verified",
                                            "allow_empty_output": "yes",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "allow_empty_output"):
                runner.load_cases(path, None)

    def test_load_cases_filters_ids_and_rejects_unknown_ids(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "skill_name": "openubmc-developer",
                        "evals": [
                            {
                                "id": "one",
                                "prompt": "analyze one",
                                "expect": {"workspace_changes": []},
                            },
                            {
                                "id": "two",
                                "prompt": "analyze two",
                                "expect": {"workspace_changes": []},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            selected = runner.load_cases(path, {"two"})
            self.assertEqual([case["id"] for case in selected], ["two"])
            with self.assertRaisesRegex(ValueError, "unknown eval id"):
                runner.load_cases(path, {"missing"})

    def test_load_cases_rejects_incomplete_read_allowlist(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "skill_name": "openubmc-developer",
                        "evals": [
                            {
                                "id": "invalid-allowlist",
                                "prompt": "analyze",
                                "expect": {
                                    "workspace_changes": [],
                                    "references": {
                                        "required": ["references/lua-component.md"],
                                        "allowed": [],
                                    },
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "allowed must include"):
                runner.load_cases(path, None)

    def test_materialize_workspace_copies_fixture_without_mutating_it(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = root / "fixtures"
            template = fixtures / "base"
            template.mkdir(parents=True)
            (template / "source.txt").write_text("original\n", encoding="utf-8")
            destination = root / "run"
            case = {
                "workspace": {
                    "copy_from": "base",
                    "files": {
                        "source.txt": "overlay\n",
                        "scripts/check.sh": {
                            "content": "#!/bin/sh\nexit 0\n",
                            "mode": "0755",
                        },
                    },
                }
            }

            runner.materialize_workspace(case, destination, fixtures)

            self.assertEqual(
                (destination / "source.txt").read_text(encoding="utf-8"),
                "overlay\n",
            )
            self.assertTrue((destination / "scripts/check.sh").stat().st_mode & 0o111)
            self.assertEqual(
                (template / "source.txt").read_text(encoding="utf-8"),
                "original\n",
            )

    def test_materialize_workspace_creates_real_dirty_git_state(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "run"
            case = {
                "workspace": {
                    "files": {
                        "tracked.txt": "baseline\n",
                        "clean.txt": "clean\n",
                    },
                    "git": {
                        "dirty_files": {
                            "tracked.txt": "user change\n",
                            "untracked.txt": "new work\n",
                        }
                    },
                }
            }

            runner.materialize_workspace(case, destination, root)
            completed = subprocess.run(
                ["git", "status", "--short"],
                cwd=destination,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertEqual(
            set(completed.stdout.splitlines()),
            {" M tracked.txt", "?? untracked.txt"},
        )

    def test_materialize_workspace_can_create_empty_git_baseline(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "run"
            case = {"workspace": {"git": {}}}

            runner.materialize_workspace(case, destination, root)
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=destination,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertRegex(completed.stdout.strip(), r"^[0-9a-f]{40}$")

    def test_git_control_snapshot_detects_staging(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            runner._initialize_git_workspace(workspace)
            before = runner.snapshot_git_control_state(workspace)

            (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "tracked.txt"], cwd=workspace, check=True
            )
            after = runner.snapshot_git_control_state(workspace)

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)

    def test_git_control_snapshot_detects_registered_worktrees(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "main"
            workspace.mkdir()
            (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            runner._initialize_git_workspace(workspace)
            before = runner.snapshot_git_control_state(workspace)

            linked = root / "linked"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            )
            after = runner.snapshot_git_control_state(workspace)

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)

    def test_execution_environment_prepends_workspace_tool_directory(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            tool_dir = workspace / ".eval-bin"
            tool_dir.mkdir()
            environment = runner.execution_environment(
                {"workspace": {"path_prepend": [".eval-bin"]}}, workspace
            )

        self.assertEqual(environment["PATH"].split(":", 1)[0], str(tool_dir))

    def test_input_provenance_hashes_skill_runner_and_cases(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            skill_root = Path(directory) / "skill"
            runner_path = skill_root / "scripts" / "run.py"
            cases_path = skill_root / "evals" / "evals.json"
            runner_path.parent.mkdir(parents=True)
            cases_path.parent.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
            runner_path.write_text("print('runner')\n", encoding="utf-8")
            cases_path.write_text("{}\n", encoding="utf-8")
            (skill_root / "skill.json").write_text(
                json.dumps(
                    {
                        "name": "openubmc-developer",
                        "files": [
                            "SKILL.md",
                            "skill.json",
                            "scripts/run.py",
                            "evals/evals.json",
                        ]
                    }
                ),
                encoding="utf-8",
            )
            dependency_root = Path(directory) / "build"
            dependency_root.mkdir()
            (dependency_root / "SKILL.md").write_text(
                "# Build\n", encoding="utf-8"
            )
            (dependency_root / "skill.json").write_text(
                json.dumps(
                    {
                        "name": "openubmc-build",
                        "files": ["SKILL.md", "skill.json"],
                    }
                ),
                encoding="utf-8",
            )

            skill_names = {"openubmc-developer", "openubmc-build"}
            first = runner.input_provenance(
                skill_root, runner_path, cases_path, skill_names=skill_names
            )
            cases_path.write_text('{"changed": true}\n', encoding="utf-8")
            second = runner.input_provenance(
                skill_root, runner_path, cases_path, skill_names=skill_names
            )
            (dependency_root / "SKILL.md").write_text(
                "# Build changed\n", encoding="utf-8"
            )
            third = runner.input_provenance(
                skill_root, runner_path, cases_path, skill_names=skill_names
            )

        self.assertRegex(
            first["skill_package"]["package_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual(
            first["runner"]["sha256"],
            first["skill_package"]["files"]["scripts/run.py"],
        )
        self.assertNotEqual(first["cases"]["sha256"], second["cases"]["sha256"])
        self.assertNotEqual(
            first["skill_package"]["package_sha256"],
            second["skill_package"]["package_sha256"],
        )
        self.assertNotEqual(
            second["skill_dependencies"]["openubmc-build"]["package_sha256"],
            third["skill_dependencies"]["openubmc-build"]["package_sha256"],
        )

    def test_input_provenance_fingerprints_dependency_without_manifest(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_root = root / "openubmc-developer"
            skill_root.mkdir()
            runner_path = skill_root / "runner.py"
            cases_path = skill_root / "cases.json"
            (skill_root / "SKILL.md").write_text(
                "---\nname: openubmc-developer\n---\n# Developer\n",
                encoding="utf-8",
            )
            runner_path.write_text("# runner\n", encoding="utf-8")
            cases_path.write_text("{}\n", encoding="utf-8")
            (skill_root / "skill.json").write_text(
                json.dumps(
                    {
                        "name": "openubmc-developer",
                        "files": ["SKILL.md", "skill.json"],
                    }
                ),
                encoding="utf-8",
            )

            dependency_root = root / "openubmc-build"
            (dependency_root / "references").mkdir(parents=True)
            (dependency_root / "SKILL.md").write_text(
                "---\nname: openubmc-build\n---\n# Build\n",
                encoding="utf-8",
            )
            reference = dependency_root / "references" / "build-contract.md"
            reference.write_text("first\n", encoding="utf-8")

            first = runner.input_provenance(
                skill_root,
                runner_path,
                cases_path,
                skill_names={"openubmc-developer", "openubmc-build"},
            )
            reference.write_text("second\n", encoding="utf-8")
            second = runner.input_provenance(
                skill_root,
                runner_path,
                cases_path,
                skill_names={"openubmc-developer", "openubmc-build"},
            )

        dependency = first["skill_dependencies"]["openubmc-build"]
        self.assertEqual(dependency["fingerprint_source"], "runtime-files")
        self.assertIsNone(dependency["manifest_sha256"])
        self.assertIn("references/build-contract.md", dependency["files"])
        self.assertNotEqual(
            dependency["package_sha256"],
            second["skill_dependencies"]["openubmc-build"]["package_sha256"],
        )

    def test_executable_version_timeout_is_a_nonblocking_diagnostic(self) -> None:
        runner = load_runner()
        with (
            mock.patch.object(runner.shutil, "which", return_value="/bin/codex"),
            mock.patch.object(
                runner.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["codex", "--version"], 5),
            ),
        ):
            version = runner._executable_version("codex")

        self.assertEqual(version["resolved"], "/bin/codex")
        self.assertIn("TimeoutExpired", version["error"])

    def test_main_report_records_summary_model_timestamps_and_input_integrity(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            args = mock.Mock(
                timeout=10,
                check_timeout=5,
                transport_retries=1,
                cases=ROOT / "evals" / "evals.json",
                ids="one",
                model="test-model",
                mode="behavior",
                output=output,
                codex="codex-test",
                list=False,
            )
            case = {"id": "one", "prompt": "analyze", "workspace": {}}
            result = {
                "id": "one",
                "workspace_changes": [],
                "skills_read": ["openubmc-developer"],
                "references_read": [],
                "returncode": 0,
                "timed_out": False,
                "case_pass": True,
                "failures": [],
            }
            inputs = {
                "skill_package": {"package_sha256": "a" * 64},
                "runner": {"sha256": "b" * 64},
                "cases": {"sha256": "c" * 64},
            }
            with (
                mock.patch.object(runner, "parse_args", return_value=args),
                mock.patch.object(runner, "load_cases", return_value=[case]),
                mock.patch.object(runner, "run_case", return_value=result),
                mock.patch.object(
                    runner, "input_provenance", side_effect=[inputs, inputs]
                ),
                mock.patch.object(
                    runner, "utc_timestamp", side_effect=["start", "finish"]
                ),
                mock.patch.object(
                    runner,
                    "_executable_version",
                    return_value={"stdout": "codex test-version"},
                ),
                mock.patch.object(runner.shutil, "which", return_value="/bin/codex-test"),
            ):
                exit_code = runner.main()

            report = json.loads(
                (output / "behavior-results.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["summary"], {"total": 1, "passed": 1, "failed": 0})
        self.assertEqual(report["provenance"]["started_at"], "start")
        self.assertEqual(report["provenance"]["finished_at"], "finish")
        self.assertEqual(report["provenance"]["requested_model"], "test-model")
        self.assertEqual(
            report["provenance"]["codex_version"],
            {"stdout": "codex test-version"},
        )
        self.assertEqual(
            report["provenance"]["runner_options"],
            {
                "timeout_seconds": 10,
                "check_timeout_seconds": 5,
                "transport_retries": 1,
                "evaluation_mode": "behavior",
            },
        )
        self.assertEqual(report["evaluation_mode"], "behavior")
        self.assertEqual(
            report["provenance"]["target_skill_path"],
            str((ROOT / "SKILL.md").resolve()),
        )
        self.assertTrue(report["provenance"]["inputs_unchanged"])
        self.assertEqual(report["provenance"]["inputs"], inputs)

    def test_main_writes_an_auditable_report_when_interrupted(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            args = mock.Mock(
                timeout=10,
                check_timeout=5,
                transport_retries=1,
                cases=ROOT / "evals" / "evals.json",
                ids="one",
                model="test-model",
                mode="behavior",
                output=output,
                codex="codex-test",
                list=False,
            )
            case = {"id": "one", "prompt": "analyze", "workspace": {}}
            inputs = {
                "skill_package": {"package_sha256": "a" * 64},
                "runner": {"sha256": "b" * 64},
                "cases": {"sha256": "c" * 64},
            }
            with (
                mock.patch.object(runner, "parse_args", return_value=args),
                mock.patch.object(runner, "load_cases", return_value=[case]),
                mock.patch.object(
                    runner, "run_case", side_effect=KeyboardInterrupt
                ),
                mock.patch.object(
                    runner, "input_provenance", side_effect=[inputs, inputs]
                ),
                mock.patch.object(
                    runner, "utc_timestamp", side_effect=["start", "finish"]
                ),
                mock.patch.object(runner.shutil, "which", return_value="/bin/codex-test"),
            ):
                exit_code = runner.main()

            report = json.loads(
                (output / "behavior-results.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 130)
        self.assertEqual(report["run_status"], "interrupted")
        self.assertEqual(report["runner_error"], "KeyboardInterrupt")
        self.assertEqual(report["incomplete_case_id"], "one")
        self.assertEqual(
            report["summary"],
            {"total": 1, "passed": 0, "failed": 0, "completed": 0, "not_run": 1},
        )

    def test_main_retries_recognized_transport_failure_in_a_fresh_attempt(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            args = mock.Mock(
                timeout=10,
                check_timeout=5,
                transport_retries=1,
                cases=ROOT / "evals" / "evals.json",
                ids="one",
                model="test-model",
                mode="behavior",
                output=output,
                codex="codex-test",
                list=False,
            )
            case = {"id": "one", "prompt": "analyze", "workspace": {}}
            first = {
                "id": "one",
                "attempt": 1,
                "workspace_changes": [],
                "skills_read": ["openubmc-developer"],
                "references_read": [],
                "returncode": 124,
                "timed_out": True,
                "stderr": "behavior evaluation timed out",
                "errors": [],
                "transport_events": [
                    "stream disconnected before completion: idle timeout waiting for SSE"
                ],
                "event_log": "/tmp/one.events.jsonl",
                "stderr_log": "/tmp/one.stderr.txt",
                "case_pass": False,
                "failures": ["codex_timeout"],
            }
            second = {
                "id": "one",
                "attempt": 2,
                "workspace_changes": [],
                "skills_read": ["openubmc-developer"],
                "references_read": [],
                "returncode": 0,
                "timed_out": False,
                "stderr": "",
                "errors": [],
                "event_log": "/tmp/one.attempt-2.events.jsonl",
                "stderr_log": "/tmp/one.attempt-2.stderr.txt",
                "case_pass": True,
                "failures": [],
            }
            inputs = {
                "skill_package": {"package_sha256": "a" * 64},
                "runner": {"sha256": "b" * 64},
                "cases": {"sha256": "c" * 64},
            }
            with (
                mock.patch.object(runner, "parse_args", return_value=args),
                mock.patch.object(runner, "load_cases", return_value=[case]),
                mock.patch.object(
                    runner, "run_case", side_effect=[first, second]
                ) as run_case,
                mock.patch.object(
                    runner, "input_provenance", side_effect=[inputs, inputs]
                ),
                mock.patch.object(
                    runner, "utc_timestamp", side_effect=["start", "finish"]
                ),
                mock.patch.object(runner.shutil, "which", return_value="/bin/codex-test"),
            ):
                exit_code = runner.main()

            report = json.loads(
                (output / "behavior-results.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_case.call_count, 2)
        self.assertTrue(report["cases"][0]["case_pass"])
        self.assertEqual(len(report["cases"][0]["transport_attempts"]), 2)

    def test_assessment_checks_reads_commands_files_and_post_checks(self) -> None:
        runner = load_runner()
        case = {
            "id": "implementation",
            "expect": {
                "workspace_changes": {
                    "required": ["src/main.lua"],
                    "allowed": ["src/main.lua", "tests/main_spec.lua"],
                },
                "skills": {
                    "required": ["openubmc-developer"],
                    "allowed": ["openubmc-developer"],
                    "forbidden": ["openubmc-build"],
                },
                "references": {
                    "required": ["references/lua-component.md"],
                    "allowed": ["references/lua-component.md"],
                    "forbidden": ["references/persistence-compatibility.md"],
                },
                "commands": {
                    "required_patterns": ["python3 .*verify.py"],
                    "forbidden_patterns": ["git reset"],
                },
                "files": {
                    "src/main.lua": {
                        "contains": ["return value or 0"],
                        "not_contains": ["BROKEN"],
                    }
                },
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": ["src/main.lua"],
            "skills_read": ["openubmc-developer"],
            "references_read": ["references/lua-component.md"],
            "commands": ["python3 scripts/verify.py"],
            "agent_messages": ["implemented"],
            "errors": [],
            "post_checks": [{"name": "contract", "passed": True}],
        }
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "src/main.lua"
            path.parent.mkdir()
            path.write_text("return value or 0\n", encoding="utf-8")

            assessment = runner.assess_trace(case, trace, workspace)

        self.assertTrue(assessment["case_pass"], assessment["failures"])

    def test_forbidden_command_patterns_match_invocations_not_search_terms(self) -> None:
        runner = load_runner()
        case = {
            "id": "forbidden-invocation",
            "expect": {
                "workspace_changes": [],
                "commands": {"forbidden_patterns": [r"bmcgo\s+gen"]},
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": [
                "/bin/bash -lc \"rg -n 'Device|bmcgo gen|exit=' .\""
            ],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }

        search_only = runner.assess_trace(case, trace)
        trace["commands"] = ["/bin/bash -lc 'bmcgo gen'"]
        executed = runner.assess_trace(case, trace)

        self.assertTrue(search_only["case_pass"], search_only["failures"])
        self.assertIn(
            r"forbidden_command_pattern:bmcgo\s+gen", executed["failures"]
        )

    def test_required_command_patterns_accept_shell_quoted_invocations(self) -> None:
        runner = load_runner()
        case = {
            "id": "quoted-required-invocation",
            "expect": {
                "workspace_changes": [],
                "commands": {"required_patterns": [r"git\s+status"]},
                "command_evidence": [
                    {
                        "command_pattern": r"git\s+status",
                        "output_pattern": r"master",
                    }
                ],
            },
        }
        command = "/bin/bash -lc \"'git' 'status' '--short' '--branch'\""
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": [command],
            "command_results": [
                {
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "output": "## master\n",
                }
            ],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertTrue(assessment["case_pass"], assessment["failures"])

    def test_assessment_rejects_git_control_state_changes(self) -> None:
        runner = load_runner()
        assessment = runner.assess_trace(
            {"id": "git-control", "expect": {"workspace_changes": []}},
            {
                "returncode": 0,
                "timed_out": False,
                "git_control_changed": True,
                "workspace_changes": [],
                "skills_read": [],
                "references_read": [],
                "commands": [],
                "agent_messages": [],
                "errors": [],
                "post_checks": [],
            },
        )

        self.assertIn("git_control_state_changed", assessment["failures"])

    def test_assessment_checks_final_message_against_observed_git_head(self) -> None:
        runner = load_runner()
        case = {
            "id": "build-handoff",
            "expect": {
                "workspace_changes": [],
                "git_head_disclosed": True,
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": [],
            "agent_messages": ["working", "保留工作区，HEAD 7bb4af719350"],
            "git_control_after": {
                "head": "7bb4af719350b2ff8c930bc63c1046d3e0a4ceb9"
            },
            "errors": [],
            "post_checks": [],
        }

        disclosed = runner.assess_trace(case, trace)
        self.assertTrue(disclosed["case_pass"], disclosed["failures"])

        trace["agent_messages"][-1] = "保留工作区，HEAD deadbee"
        wrong = runner.assess_trace(case, trace)
        self.assertIn("missing_git_head_disclosure", wrong["failures"])

        trace["agent_messages"][-1] = "交接完成"
        missing = runner.assess_trace(case, trace)
        self.assertIn("missing_git_head_disclosure", missing["failures"])

    def test_assessment_rejects_a_same_named_skill_from_the_wrong_path(self) -> None:
        runner = load_runner()
        case = {
            "id": "wrong-copy",
            "expect": {
                "workspace_changes": [],
                "skills": {"required": ["openubmc-developer"]},
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": ["openubmc-developer"],
            "skill_reads": [
                {
                    "name": "openubmc-developer",
                    "path": "/release/openubmc-developer/SKILL.md",
                    "resolved_path": "/release/openubmc-developer/SKILL.md",
                }
            ],
            "skill_paths_read": ["/release/openubmc-developer/SKILL.md"],
            "evaluation_mode": "behavior",
            "expected_skill_path": "/candidate/openubmc-developer/SKILL.md",
            "references_read": [],
            "commands": [],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }

        behavior = runner.assess_trace(case, trace)
        trace["evaluation_mode"] = "trigger"
        trigger = runner.assess_trace(case, trace)

        self.assertIn(
            "wrong_skill_path:/release/openubmc-developer/SKILL.md",
            behavior["failures"],
        )
        self.assertIn(
            "wrong_skill_path:/release/openubmc-developer/SKILL.md",
            trigger["failures"],
        )

    def test_assessment_rejects_reads_outside_the_semantic_allowlist(self) -> None:
        runner = load_runner()
        case = {
            "id": "narrow-reference",
            "expect": {
                "workspace_changes": [],
                "skills": {
                    "required": ["openubmc-developer"],
                    "allowed": ["openubmc-developer"],
                },
                "references": {
                    "required": ["references/lua-component.md"],
                    "allowed": ["references/lua-component.md"],
                },
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": ["openubmc-developer", "openubmc-build"],
            "references_read": [
                "references/lua-component.md",
                "references/persistence-compatibility.md",
            ],
            "commands": [],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }

        assessment = runner.assess_trace(case, trace)

        self.assertIn("unexpected_skill_read:openubmc-build", assessment["failures"])
        self.assertIn(
            "unexpected_reference_read:references/persistence-compatibility.md",
            assessment["failures"],
        )

    def test_command_evidence_requires_successful_execution_output(self) -> None:
        runner = load_runner()
        case = {
            "id": "isolated-build",
            "expect": {
                "workspace_changes": [],
                "command_evidence": [
                    {
                        "command_pattern": r"(?:^|\n)\s*\./build\.sh(?:\s|$)",
                        "output_pattern": "ISOLATED_BUILD_EXECUTED",
                    }
                ],
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": ['/bin/bash -lc "sed -n \'1,200p\' build.sh"'],
            "command_results": [
                {
                    "command": '/bin/bash -lc "sed -n \'1,200p\' build.sh"',
                    "status": "completed",
                    "exit_code": 0,
                    "output": "printf 'ISOLATED_BUILD_EXECUTED\\n'",
                }
            ],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }

        missing = runner.assess_trace(case, trace)
        self.assertIn("missing_command_evidence:0", missing["failures"])

        trace["command_results"].append(
            {
                "command": "python3 -c ./build.sh",
                "status": "completed",
                "exit_code": 0,
                "output": "ISOLATED_BUILD_EXECUTED\n",
            }
        )
        non_shell = runner.assess_trace(case, trace)
        self.assertIn("missing_command_evidence:0", non_shell["failures"])

        trace["command_results"].append(
            {
                "command": "/bin/bash -lc ./build.sh",
                "status": "completed",
                "exit_code": None,
                "output": "ISOLATED_BUILD_EXECUTED\n",
            }
        )
        unknown_exit = runner.assess_trace(case, trace)
        self.assertIn("missing_command_evidence:0", unknown_exit["failures"])

        trace["command_results"].append(
            {
                "command": "/bin/bash -lc ./build.sh",
                "status": "completed",
                "exit_code": 0,
                "output": "ISOLATED_BUILD_EXECUTED\n",
            }
        )
        observed = runner.assess_trace(case, trace)
        self.assertTrue(observed["case_pass"], observed["failures"])

    def test_command_evidence_can_allow_suppressed_transport_output(self) -> None:
        runner = load_runner()
        case = {
            "id": "suppressed-output",
            "expect": {
                "workspace_changes": [],
                "command_evidence": [
                    {
                        "command_pattern": r"python3\s+tests/verify\.py",
                        "output_pattern": "verified",
                        "allow_empty_output": True,
                    }
                ],
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": ["python3 tests/verify.py"],
            "command_results": [
                {
                    "command": "python3 tests/verify.py",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "",
                }
            ],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }

        suppressed = runner.assess_trace(case, trace)
        self.assertTrue(suppressed["case_pass"], suppressed["failures"])

        trace["command_results"][0]["output"] = "unexpected non-empty output"
        mismatched = runner.assess_trace(case, trace)
        self.assertIn("missing_command_evidence:0", mismatched["failures"])

        trace["command_results"][0]["output"] = ""
        trace["command_results"][0]["exit_code"] = 1
        failed = runner.assess_trace(case, trace)
        self.assertIn("missing_command_evidence:0", failed["failures"])

    def test_snapshot_records_symlink_identity_not_external_content(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first\n", encoding="utf-8")
            second.write_text("second\n", encoding="utf-8")
            link = workspace / "linked.txt"
            link.symlink_to(first)

            before = runner.snapshot_tree(workspace)
            first.write_text("changed outside\n", encoding="utf-8")
            after_external_change = runner.snapshot_tree(workspace)
            link.unlink()
            link.symlink_to(second)
            after_link_change = runner.snapshot_tree(workspace)

        self.assertEqual(before, after_external_change)
        self.assertEqual(
            runner.diff_snapshots(before, after_link_change),
            ["linked.txt"],
        )

    def test_file_assertions_reject_symlink_substitution(self) -> None:
        runner = load_runner()
        case = {
            "id": "symlink-file",
            "expect": {
                "workspace_changes": [],
                "files": {"result.txt": {"equals": "expected\n"}},
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": [],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            external = root / "external.txt"
            external.write_text("expected\n", encoding="utf-8")
            (workspace / "result.txt").symlink_to(external)

            assessment = runner.assess_trace(case, trace, workspace)

        self.assertIn("file_is_symlink:result.txt", assessment["failures"])

    def test_file_assertions_reject_symlinked_parent_escape(self) -> None:
        runner = load_runner()
        case = {
            "id": "symlink-parent",
            "expect": {
                "workspace_changes": [],
                "files": {"result/value.txt": {"equals": "expected\n"}},
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": [],
            "skills_read": [],
            "references_read": [],
            "commands": [],
            "agent_messages": [],
            "errors": [],
            "post_checks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            external = root / "external"
            external.mkdir()
            (external / "value.txt").write_text("expected\n", encoding="utf-8")
            (workspace / "result").symlink_to(external, target_is_directory=True)

            assessment = runner.assess_trace(case, trace, workspace)

        self.assertIn(
            "file_escapes_workspace:result/value.txt", assessment["failures"]
        )

    def test_assessment_requires_disclosure_only_when_adjacent_file_changed(self) -> None:
        runner = load_runner()
        case = {
            "id": "adjacent",
            "expect": {
                "workspace_changes": {
                    "required": ["src/main.lua"],
                    "allowed": ["src/main.lua", "src/adjacent.lua"],
                },
                "disclosures": [
                    {
                        "if_changed": ["src/adjacent.lua"],
                        "message_pattern": "相邻|额外|adjacent",
                    }
                ],
            },
        }
        trace = {
            "returncode": 0,
            "timed_out": False,
            "workspace_changes": ["src/main.lua", "src/adjacent.lua"],
            "skills_read": [],
            "references_read": [],
            "commands": [],
            "agent_messages": ["修复完成。"],
            "errors": [],
            "post_checks": [],
        }

        missing = runner.assess_trace(case, trace)
        self.assertIn("missing_disclosure:src/adjacent.lua", missing["failures"])

        trace["agent_messages"] = ["另外修复了同一流程中的相邻缺陷。"]
        disclosed = runner.assess_trace(case, trace)
        self.assertTrue(disclosed["case_pass"], disclosed["failures"])

    def test_post_checks_use_argv_without_shell(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            checks = [
                {
                    "name": "python",
                    "argv": ["python3", "-c", "print('verified')"],
                    "stdout_contains": ["verified"],
                }
            ]

            results = runner.run_post_checks(checks, workspace, 10)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["passed"], results[0])
        self.assertEqual(results[0]["workspace_changes"], [])

    def test_post_checks_fail_when_they_mutate_the_workspace(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            checks = [
                {
                    "name": "mutating-check",
                    "argv": [
                        "python3",
                        "-c",
                        "from pathlib import Path; Path('changed.txt').write_text('x')",
                    ],
                }
            ]

            result = runner.run_post_checks(checks, workspace, 10)[0]

        self.assertFalse(result["passed"])
        self.assertEqual(result["workspace_changes"], ["changed.txt"])
        self.assertIn("workspace_changed:changed.txt", result["failures"])

    def test_run_case_turns_timeout_into_observable_failure(self) -> None:
        runner = load_runner()
        case = {
            "id": "timeout",
            "prompt": "Use $openubmc-developer to inspect the source.",
            "workspace": {"files": {"src/main.lua": "return 1\n"}},
            "expect": {"workspace_changes": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            timeout = subprocess.TimeoutExpired(
                cmd=["codex"], timeout=1, output="", stderr="still running"
            )
            with mock.patch.object(runner.subprocess, "run", side_effect=timeout):
                result = runner.run_case(
                    case,
                    timeout=1,
                    check_timeout=1,
                    model=None,
                    output_dir=output,
                    fixture_root=Path(directory),
                    codex_binary="codex",
                )

        self.assertTrue(result["timed_out"])
        self.assertEqual(result["returncode"], 124)
        self.assertFalse(result["case_pass"])
        self.assertIn("codex_timeout", result["failures"])


if __name__ == "__main__":
    unittest.main()
