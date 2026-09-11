"""Tests for the native Codex Skill-routing evidence seam."""

import json
from pathlib import Path
import copy
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.skill_routing_evaluation import (
    _execution_failure_layer,
    _operational_failure_layer,
    _rollout_identity,
    apply_review_contract,
    build_codex_command,
    build_resume_command,
    compare_arm_evidence,
    document_digest,
    evaluate_route,
    file_content_inventory,
    inventory_content_digest,
    load_arm_identity,
    load_matrix,
    render_comparison_report,
    resolve_executable,
    routing_exit_code,
    scan_secret_files,
    summarize_events,
    verify_evaluator_identity,
    verify_rollout_identity,
    verify_arm_artifacts,
    verify_loose_content_inventory,
    validate_review_classification,
    verify_workspace_layout,
)


ROOT = Path(__file__).resolve().parents[2]


class SkillRoutingEvaluationTests(unittest.TestCase):
    def test_evaluator_identity_requires_one_commit_with_all_review_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "eval@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Routing Eval"],
                check=True,
            )
            runner = root / "runner.py"
            matrix = root / "matrix.json"
            review_contract = root / "review-contract.json"
            runner.write_text("print('verified')\n")
            matrix.write_text("{}\n")
            review_contract.write_text("{}\n")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "add",
                    "runner.py",
                    "matrix.json",
                    "review-contract.json",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "evaluator"], check=True
            )
            evaluator_commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
            (root / "later.txt").write_text("evidence may be committed later\n")
            subprocess.run(
                ["git", "-C", str(root), "add", "later.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "evidence"], check=True
            )

            verified = verify_evaluator_identity(
                root, evaluator_commit, runner, matrix, review_contract
            )
            runner.write_text("print('changed')\n")
            changed = verify_evaluator_identity(
                root, evaluator_commit, runner, matrix, review_contract
            )
            runner.write_text("print('verified')\n")
            review_contract.write_text('{"changed": true}\n')
            changed_contract = verify_evaluator_identity(
                root, evaluator_commit, runner, matrix, review_contract
            )

        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["commit"], evaluator_commit)
        self.assertEqual(
            set(verified["files"]), {"runner", "matrix", "review_contract"}
        )
        self.assertEqual(changed["status"], "unverified")
        self.assertIn("runner.bytes", changed["mismatches"])
        self.assertEqual(changed_contract["status"], "unverified")
        self.assertIn("review_contract.bytes", changed_contract["mismatches"])

    def test_matrix_covers_requested_intents_and_workspace_modes(self) -> None:
        matrix = load_matrix(ROOT / "evaluation/plugin-tasks/routing-matrix.json")

        intents = {case["intent"] for case in matrix["cases"]}
        self.assertEqual(
            intents,
            {
                "ambiguous",
                "build",
                "component-development",
                "credentials",
                "diagnosis",
                "knowledge-base",
                "logs",
                "multi-turn",
                "negative",
                "upgrade",
            },
        )
        modes = [case["workspace_mode"] for case in matrix["cases"]]
        self.assertGreater(modes.count("ordinary"), modes.count("source"))
        self.assertIn("source", modes)
        self.assertIn("explicit-source", modes)

    def test_review_contract_declares_supporting_and_per_turn_routes(self) -> None:
        matrix_path = ROOT / "evaluation/plugin-tasks/routing-matrix.json"
        contract_path = ROOT / "evaluation/plugin-tasks/routing-review-contract.json"
        matrix = apply_review_contract(load_matrix(matrix_path), matrix_path, contract_path)
        cases = {case["case_id"]: case for case in matrix["cases"]}

        self.assertEqual(cases["kb-en-ordinary"]["allowed_routes"], ["openubmc-debug"])
        self.assertEqual(
            cases["context-en-multi-turn"]["expected_routes_by_turn"],
            [[], ["openubmc-debug"]],
        )

    def test_mcp_route_rejects_an_undeclared_supporting_skill(self) -> None:
        case = {
            "expected_routes": [],
            "allowed_routes": ["openubmc-debug"],
            "expected_mcp": ["openubmc-kb"],
        }
        inventory = [
            {"name": "openubmc-debug", "enabled": True},
            {"name": "openubmc-upgrade", "enabled": True},
        ]
        calls = [{"server": "openubmc-kb"}]

        allowed = evaluate_route(
            case,
            {"skill_reads": ["openubmc-debug"], "mcp_calls": calls},
            inventory,
            available_mcp=["openubmc-kb"],
        )
        unrelated = evaluate_route(
            case,
            {"skill_reads": ["openubmc-upgrade"], "mcp_calls": calls},
            inventory,
            available_mcp=["openubmc-kb"],
        )

        self.assertEqual(allowed["status"], "passed")
        self.assertEqual(unrelated["classification"], "wrong-route")

    def test_loose_content_inventory_must_match_the_retained_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skills"
            skill_root.mkdir()
            (skill_root / "SKILL.md").write_text("---\nname: example\n---\n")
            retained = root / "routing-loose-content-inventory.json"
            document = file_content_inventory(skill_root)
            retained.write_text(json.dumps(document))
            expected = {
                "path": retained.name,
                "sha256": "sha256:"
                + __import__("hashlib").sha256(retained.read_bytes()).hexdigest(),
                "file_count": 1,
            }

            verified = verify_loose_content_inventory(skill_root, retained, expected)
            (skill_root / "extra.txt").write_text("drift\n")
            stale = verify_loose_content_inventory(skill_root, retained, expected)

        self.assertEqual(verified["status"], "verified")
        self.assertEqual(stale["status"], "unverified")
        self.assertIn("content", stale["mismatches"])

    def test_command_pins_real_model_effort_and_read_only_execution(self) -> None:
        first = build_codex_command(
            Path("/opt/codex"),
            model="gpt-5.6-sol",
            effort="xhigh",
            cwd=Path("/tmp/ordinary"),
            prompt="diagnose this BMC",
            plugins=True,
        )
        self.assertIn("gpt-5.6-sol", first)
        self.assertIn('model_reasoning_effort="xhigh"', first)
        self.assertIn("read-only", first)
        self.assertIn("--json", first)
        self.assertNotIn("--ephemeral", first)

        resumed = build_resume_command(
            Path("/opt/codex"),
            thread_id="thread-1",
            model="gpt-5.6-sol",
            effort="xhigh",
            prompt="continue",
            plugins=True,
        )
        self.assertIn('sandbox_mode="read-only"', resumed)

    def test_bare_codex_executable_is_resolved_through_path(self) -> None:
        with mock.patch(
            "scripts.skill_routing_evaluation.shutil.which", return_value="/opt/codex/bin/codex"
        ) as which:
            self.assertEqual(resolve_executable("codex"), Path("/opt/codex/bin/codex"))
        which.assert_called_once_with("codex")

    def test_rollout_identity_verifies_every_turn_including_resume(self) -> None:
        identity = {
            "status": "recorded",
            "codex_version": "0.153.4",
            "model_provider": "cliproxy",
            "cwd": "/tmp/ordinary",
            "turn_contexts": [
                {
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "cwd": "/tmp/ordinary",
                    "approval_policy": "never",
                    "sandbox_policy": {"type": "read-only"},
                },
                {
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "cwd": "/tmp/ordinary",
                    "approval_policy": "never",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            ],
        }
        expected = {
            "codex_version": "0.153.4",
            "model_provider": "cliproxy",
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
            "cwd": "/tmp/ordinary",
            "approval_policy": "never",
            "sandbox_policy": {"type": "read-only"},
            "turn_count": 2,
        }

        result = verify_rollout_identity(identity, expected)

        self.assertEqual(result["status"], "unverified")
        self.assertIn("turn_contexts[1].sandbox_policy", result["mismatches"])

    def test_rollout_identity_rejects_a_non_native_originator(self) -> None:
        identity = {
            "status": "recorded",
            "originator": "example-wrapper",
            "turn_contexts": [],
        }

        result = verify_rollout_identity(
            identity,
            {"originator": "codex_exec", "turn_count": 0},
        )

        self.assertEqual(result["status"], "unverified")
        self.assertIn("originator", result["mismatches"])

    def test_candidate_arm_identity_requires_bound_distribution_and_runtime(self) -> None:
        sha = "sha256:" + "a" * 64
        document = {
            "schema": "openubmc.skill-routing-arm.v1",
            "arm_id": "plugin-candidate",
            "kind": "plugin",
            "source": {"repository": "owner/repo", "commit": "a" * 40, "tree": "b" * 40},
            "inventory": {"sha256": sha, "content_digest": sha},
            "execution": {
                "codex_version": "0.153.4",
                "codex_executable_sha256": sha,
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "model_provider": "cliproxy",
                "originator": "codex_exec",
                "approval_policy": "never",
                "sandbox_policy": {"type": "read-only"},
                "plugins": True,
            },
            "environment": {
                "profile": "isolated-no-private-inputs",
                "target": "not-provided",
                "credentials": "not-provided",
                "knowledge_base": "not-configured",
            },
            "plugin": {
                "archive_sha256": sha,
                "content_digest": sha,
                "subject_digest": sha,
            },
        }
        document["digest"] = document_digest(document)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "arm.json"
            path.write_text(json.dumps(document))

            with self.assertRaisesRegex(ValueError, "runtime_digest"):
                load_arm_identity(path)

    def test_arm_artifact_verification_detects_archive_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "eval@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Eval"], check=True)
            (source / "README").write_text("source\n")
            subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "source"], check=True)
            commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            inventory_rows = [{"name": "openubmc:x", "path": "/skills/x/SKILL.md", "enabled": True}]
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory_rows))
            archive = root / "candidate.tar.gz"
            archive.write_bytes(b"candidate")
            subject = {"payload": {"content_digest": "content-1"}}
            subject["digest"] = document_digest(subject)
            subject_path = root / "subject.json"
            subject_path.write_text(json.dumps(subject))
            runtime = {"subject_digest": subject["digest"]}
            runtime["digest"] = document_digest(runtime)
            runtime_path = root / "runtime.json"
            runtime_path.write_text(json.dumps(runtime))
            sha = "sha256:" + "a" * 64
            identity = {
                "source": {"commit": commit, "tree": tree},
                "inventory": {
                    "sha256": "sha256:" + __import__("hashlib").sha256(inventory_path.read_bytes()).hexdigest(),
                    "content_digest": inventory_content_digest(inventory_rows),
                },
                "plugin": {
                    "archive_sha256": "sha256:" + __import__("hashlib").sha256(archive.read_bytes()).hexdigest(),
                    "content_digest": "content-1",
                    "subject_digest": subject["digest"],
                    "runtime_digest": runtime["digest"],
                },
            }
            archive.write_bytes(b"drifted")

            result = verify_arm_artifacts(
                identity,
                inventory_path=inventory_path,
                source_workspace=source,
                artifact_paths={
                    "archive": archive,
                    "subject": subject_path,
                    "runtime": runtime_path,
                },
            )

            self.assertEqual(result["status"], "unverified")
            self.assertIn("plugin.archive_sha256", result["mismatches"])

    def test_arm_artifact_verification_cross_binds_subject_and_runtime_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "config", "user.email", "eval@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Eval"],
                check=True,
            )
            (source / "README").write_text("source\n")
            subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "source"], check=True)
            commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            inventory_rows = [
                {
                    "name": "openubmc:x",
                    "path": "/cache/openubmc/1/skills/x/SKILL.md",
                    "enabled": True,
                    "pluginId": "openubmc@test",
                }
            ]
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory_rows))
            archive = root / "candidate.tar.gz"
            archive.write_bytes(b"candidate")
            archive_digest = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
            wrong_commit = "f" * 40
            plugin = {"marketplace": "test", "name": "openubmc", "version": "1"}
            subject = {
                "distribution": {
                    "archive": {"sha256": archive_digest},
                    "commit": wrong_commit,
                    "repository": "example/openubmc",
                },
                "payload": {
                    "content_digest": "content-1",
                    "mcp_servers": ["openubmc-kb"],
                    "skill_files": {"skills/x/SKILL.md": "file-digest"},
                    "source_commit": wrong_commit,
                },
                "plugin": plugin,
            }
            subject["digest"] = document_digest(subject)
            subject_path = root / "subject.json"
            subject_path.write_text(json.dumps(subject))
            runtime = {
                "archive_sha256": archive_digest,
                "archive_verified": True,
                "codex_executable_sha256": "c" * 64,
                "content_digest": "content-1",
                "distribution_commit": wrong_commit,
                "loaded_mcp_servers": ["openubmc-kb"],
                "loaded_skill_files": ["skills/y/SKILL.md"],
                "loaded_skills": ["openubmc:y"],
                "mcp_servers": ["openubmc-kb"],
                "native_plugin_loaded": True,
                "payload_source_commit": wrong_commit,
                "plugin": plugin,
                "skills_verified": 1,
                "subject_digest": subject["digest"],
            }
            runtime["digest"] = document_digest(runtime)
            runtime_path = root / "runtime.json"
            runtime_path.write_text(json.dumps(runtime))
            identity = {
                "kind": "plugin",
                "source": {
                    "commit": commit,
                    "tree": tree,
                    "repository": "example/openubmc",
                },
                "execution": {"codex_executable_sha256": "sha256:" + "c" * 64},
                "inventory": {
                    "sha256": "sha256:"
                    + __import__("hashlib").sha256(inventory_path.read_bytes()).hexdigest(),
                    "content_digest": inventory_content_digest(inventory_rows),
                },
                "plugin": {
                    **plugin,
                    "archive_sha256": "sha256:" + archive_digest,
                    "content_digest": "content-1",
                    "mcp_servers": ["openubmc-kb"],
                    "plugin_skill_count": 1,
                    "subject_digest": subject["digest"],
                    "runtime_digest": runtime["digest"],
                },
            }

            result = verify_arm_artifacts(
                identity,
                inventory_path=inventory_path,
                source_workspace=source,
                artifact_paths={
                    "archive": archive,
                    "subject": subject_path,
                    "runtime": runtime_path,
                },
            )

            self.assertEqual(result["status"], "unverified")
            self.assertIn("plugin.subject_distribution_commit", result["mismatches"])
            self.assertIn("plugin.subject_payload_source_commit", result["mismatches"])
            self.assertIn("plugin.runtime_distribution_commit", result["mismatches"])
            self.assertIn("plugin.runtime_payload_source_commit", result["mismatches"])
            self.assertIn("plugin.runtime_inventory_skill_names", result["mismatches"])
            self.assertIn("plugin.runtime_inventory_skill_files", result["mismatches"])

    def test_route_evaluation_reports_disabled_expected_skill_before_fallback(self) -> None:
        case = {
            "expected_routes": ["openubmc-debug"],
            "expected_mcp": [],
            "intent": "diagnosis",
        }
        inventory = [
            {"name": "openubmc-debug", "enabled": False},
            {"name": "openubmc-debugging", "enabled": True},
        ]
        observation = {"skill_reads": ["openubmc-debugging"], "mcp_calls": []}

        result = evaluate_route(case, observation, inventory)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], "skill-not-loaded")
        self.assertEqual(result["expected_availability"], {"openubmc-debug": "disabled"})
        self.assertEqual(result["fallback_routes"], ["openubmc-debugging"])

    def test_route_evaluation_rejects_a_second_owning_skill(self) -> None:
        case = {
            "expected_routes": ["openubmc-debug"],
            "expected_mcp": [],
            "intent": "diagnosis",
        }
        inventory = [
            {"name": "openubmc-debug", "enabled": True},
            {"name": "openubmc-upgrade", "enabled": True},
        ]
        observation = {
            "skill_reads": ["openubmc-debug", "openubmc-upgrade"],
            "mcp_calls": [],
        }

        result = evaluate_route(case, observation, inventory)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], "wrong-route")
        self.assertEqual(result["fallback_routes"], ["openubmc-upgrade"])

    def test_route_evaluation_distinguishes_an_unavailable_mcp(self) -> None:
        case = {
            "expected_routes": [],
            "expected_mcp": ["openubmc-kb"],
            "intent": "knowledge-base",
        }

        result = evaluate_route(
            case,
            {"skill_reads": [], "mcp_calls": []},
            [],
            available_mcp=[],
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], "mcp-not-loaded")
        self.assertEqual(
            result["expected_mcp_availability"],
            {"openubmc-kb": "unavailable"},
        )

    def test_multi_turn_route_evaluation_rejects_a_premature_first_turn(self) -> None:
        case = {
            "expected_routes": ["openubmc-debug"],
            "expected_routes_by_turn": [[], ["openubmc-debug"]],
            "expected_mcp": [],
            "intent": "multi-turn",
        }
        inventory = [{"name": "openubmc:openubmc-debug", "enabled": True}]
        turn_observations = [
            {"skill_reads": ["openubmc:openubmc-debug"], "mcp_calls": []},
            {"skill_reads": ["openubmc:openubmc-debug"], "mcp_calls": []},
        ]

        result = evaluate_route(
            case,
            {"skill_reads": ["openubmc:openubmc-debug"], "mcp_calls": []},
            inventory,
            turn_observations=turn_observations,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], "wrong-route")
        self.assertEqual(result["turns"][0]["classification"], "wrong-route")

    def test_multi_turn_route_evaluation_accepts_delayed_second_turn_route(self) -> None:
        case = {
            "expected_routes": ["openubmc-debug"],
            "expected_routes_by_turn": [[], ["openubmc-debug"]],
            "expected_mcp": [],
            "intent": "multi-turn",
        }
        inventory = [{"name": "openubmc:openubmc-debug", "enabled": True}]
        turn_observations = [
            {"skill_reads": [], "mcp_calls": []},
            {"skill_reads": ["openubmc:openubmc-debug"], "mcp_calls": []},
        ]

        result = evaluate_route(
            case,
            {"skill_reads": ["openubmc:openubmc-debug"], "mcp_calls": []},
            inventory,
            turn_observations=turn_observations,
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            [turn["classification"] for turn in result["turns"]],
            ["passed", "passed"],
        )

    def test_secret_scan_reports_variable_names_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "raw.jsonl"
            raw.write_text("token=top-secret-value\n")

            result = scan_secret_files(
                [raw],
                {
                    "CLI_PROXY_API_KEY": "top-secret-value",
                    "OPENUBMC_SSH_PASSWORD": "another-private-value",
                    "PATH": "/bin",
                },
            )

        rendered = json.dumps(result)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["matches"], [{"path": "raw.jsonl", "variable": "CLI_PROXY_API_KEY"}])
        self.assertNotIn("top-secret-value", rendered)
        self.assertNotIn("another-private-value", rendered)

    def test_clean_secret_scan_is_stable_when_unrelated_credentials_are_added(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "raw.jsonl"
            raw.write_text("no credentials here\n")

            first = scan_secret_files([raw], {"CLI_PROXY_API_KEY": "value-one"})
            second = scan_secret_files(
                [raw],
                {
                    "CLI_PROXY_API_KEY": "value-one",
                    "SPEC_REVIEW_TOKEN": "value-two",
                },
            )

        self.assertEqual(first, second)

    def test_summary_redacts_credential_values_from_all_retained_mcp_fields(self) -> None:
        secret = "credential-value-that-must-not-leak"
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "example",
                    "tool": "query",
                    "status": "failed",
                    "arguments": {"token": secret},
                    "result": {"echo": secret},
                    "error": "authorization failed for " + secret,
                },
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "do not repeat " + secret},
            },
        ]

        summary = summarize_events(
            events,
            [],
            environ={"CLI_PROXY_API_KEY": secret},
        )

        rendered = json.dumps(summary)
        self.assertNotIn(secret, rendered)
        self.assertIn("<redacted:CLI_PROXY_API_KEY>", rendered)

    def test_operational_auth_failure_is_separate_from_route_result(self) -> None:
        observation = {
            "mcp_calls": [
                {
                    "server": "openubmc-kb",
                    "tool": "query",
                    "status": "failed",
                    "arguments": {},
                    "result": {"error": {"code": "KB_CREDENTIALS_MISSING"}},
                    "error": None,
                }
            ]
        }

        layer = _operational_failure_layer(observation)

        self.assertEqual(layer["status"], "failed")
        self.assertEqual(layer["classifications"], ["environment-or-auth"])
        self.assertEqual(layer["environment_or_auth_codes"], ["KB_CREDENTIALS_MISSING"])

    def test_execution_recovery_requires_transport_errors_and_complete_turns(self) -> None:
        complete = {
            "completed_turns": 1,
            "final_answer": "done",
            "errors": ["Reconnecting... stream disconnected: Transport error: timeout"],
        }
        arbitrary_error = {**complete, "errors": ["internal model invariant failed"]}
        truncated = {**complete, "completed_turns": 0, "final_answer": ""}

        recovered = _execution_failure_layer(
            returncodes=[0],
            timed_out=False,
            invalid_json_lines=0,
            observation=complete,
            expected_turns=1,
        )
        failed = _execution_failure_layer(
            returncodes=[0],
            timed_out=False,
            invalid_json_lines=0,
            observation=arbitrary_error,
            expected_turns=1,
        )
        incomplete = _execution_failure_layer(
            returncodes=[0],
            timed_out=False,
            invalid_json_lines=0,
            observation=truncated,
            expected_turns=1,
        )

        self.assertEqual(recovered["status"], "passed")
        self.assertEqual(recovered["classifications"], ["model-transport-recovered"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("model-event-error", failed["classifications"])
        self.assertEqual(incomplete["status"], "failed")
        self.assertIn("incomplete-turns", incomplete["classifications"])
        self.assertIn("missing-final-answer", incomplete["classifications"])

    def test_operational_failure_reads_completed_domain_errors(self) -> None:
        observation = {
            "mcp_calls": [
                {
                    "server": "openubmc-target-runtime",
                    "tool": "observe",
                    "status": "completed",
                    "arguments": {},
                    "result": {
                        "structured_content": {
                            "ok": False,
                            "error": {"code": "CAPABILITY_UNAVAILABLE"},
                        }
                    },
                    "error": None,
                }
            ]
        }

        layer = _operational_failure_layer(observation)

        self.assertEqual(layer["status"], "failed")
        self.assertEqual(layer["classifications"], ["capability-missing"])
        self.assertEqual(layer["capability_codes"], ["CAPABILITY_UNAVAILABLE"])

    def test_route_failure_changes_run_exit_code(self) -> None:
        verified_identity = {"verification": {"status": "verified"}}
        passed = {
            "timed_out": False,
            "expected_turns": 1,
            "returncodes": [0],
            "identity": verified_identity,
            "observation": {"completed_turns": 1, "final_answer": "done"},
            "route": {"status": "passed"},
        }
        failed = {**passed, "route": {"status": "failed"}}

        self.assertEqual(routing_exit_code([passed], {"status": "clean"}), 0)
        self.assertEqual(routing_exit_code([passed, failed], {"status": "clean"}), 2)

    def test_rollout_prompt_identity_ignores_injected_agent_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            sessions = codex_home / "sessions/2026/09/11"
            sessions.mkdir(parents=True)
            thread_id = "thread-1"
            rollout = sessions / f"rollout-{thread_id}.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "# AGENTS.md instructions\n<environment_context>injected</environment_context>",
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "actual task"}],
                    },
                },
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

            identity = _rollout_identity(codex_home, thread_id)

        self.assertEqual(identity["user_prompts"], ["actual task"])

    def test_workspace_layout_binds_empty_non_git_cwd_and_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary = root / "ordinary"
            source = root / "external-source"
            ordinary.mkdir()
            source.mkdir()
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "routing@example.invalid"],
                ["git", "config", "user.name", "Routing Test"],
            ):
                subprocess.run(command, cwd=source, check=True)
            (source / "README.md").write_text("source\n")
            subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "source"], cwd=source, check=True)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
            ).stdout.strip()

            verified = verify_workspace_layout(ordinary, source, source, commit)
            self.assertEqual(verified["status"], "verified")
            self.assertTrue(verified["ordinary"]["empty"])
            self.assertFalse(verified["ordinary"]["inside_git"])

            (ordinary / "unexpected.txt").write_text("not empty\n")
            rejected = verify_workspace_layout(ordinary, source, source, commit)
            self.assertEqual(rejected["status"], "unverified")
            self.assertIn("ordinary.empty", rejected["mismatches"])

    def test_summary_uses_actual_skill_reads_and_mcp_calls(self) -> None:
        inventory = [
            {
                "name": "openubmc:openubmc-debug",
                "path": "/cache/openubmc/skills/openubmc-debug/SKILL.md",
                "enabled": True,
                "pluginId": "openubmc@test",
            }
        ]
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "sed -n '1,240p' /cache/openubmc/skills/openubmc-debug/SKILL.md",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "---\nname: openubmc-debug\ndescription: Diagnose openUBMC\n---\n",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "openubmc-target-runtime",
                    "tool": "observe",
                    "status": "completed",
                    "arguments": {"kind": "status"},
                    "result": {"isError": True},
                    "error": None,
                },
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "target unavailable"},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        ]

        summary = summarize_events(events, inventory)

        self.assertEqual(summary["thread_id"], "thread-1")
        self.assertEqual(summary["skill_reads"], ["openubmc:openubmc-debug"])
        self.assertEqual(
            summary["skill_read_evidence"],
            [
                {
                    "name": "openubmc:openubmc-debug",
                    "path": "/cache/openubmc/skills/openubmc-debug/SKILL.md",
                    "command": "sed -n '1,240p' /cache/openubmc/skills/openubmc-debug/SKILL.md",
                    "output_sha256": "sha256:2a7efe7d79fa063e8c48c8afb8669c2c972ddf5745a610be1b40f094f7e21cf8",
                }
            ],
        )
        self.assertEqual(
            summary["mcp_calls"],
            [
                {
                    "server": "openubmc-target-runtime",
                    "tool": "observe",
                    "status": "completed",
                    "arguments": {"kind": "status"},
                    "result": {"isError": True},
                    "error": None,
                }
            ],
        )
        self.assertEqual(summary["usage"]["total_tokens"], 15)
        self.assertEqual(summary["final_answer"], "target unavailable")

    def test_summary_rejects_failed_or_unproven_skill_read_commands(self) -> None:
        path = "/cache/openubmc/skills/openubmc-debug/SKILL.md"
        inventory = [{"name": "openubmc:openubmc-debug", "path": path, "enabled": True}]
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": f"cat {path}",
                    "status": "failed",
                    "exit_code": 1,
                    "aggregated_output": "",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": f"echo {path}",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": path + "\n",
                },
            },
        ]

        self.assertEqual(summarize_events(events, inventory)["skill_reads"], [])

    def test_summary_bounds_large_mcp_results_with_a_content_digest(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "openubmc-kb",
                    "tool": "query",
                    "status": "completed",
                    "arguments": {},
                    "result": "x" * 20000,
                    "error": None,
                },
            }
        ]

        result = summarize_events(events, [])["mcp_calls"][0]["result"]

        self.assertTrue(result["truncated"])
        self.assertEqual(result["original_bytes"], 20002)
        self.assertEqual(
            result["sha256"],
            "sha256:e03d9e85eec7bdc57d99d7347dc8df60e467ba0bcf8d242db601ce4c6c01798a",
        )
        self.assertLessEqual(len(result["preview"]), 4096)

    def test_review_classification_keeps_failure_layers_distinct(self) -> None:
        for value in (
            "passed",
            "skill-not-loaded",
            "mcp-not-loaded",
            "skill-not-triggered",
            "wrong-route",
            "capability-missing",
            "environment-or-auth",
            "unclassified",
        ):
            self.assertEqual(validate_review_classification(value), value)
        with self.assertRaisesRegex(ValueError, "classification"):
            validate_review_classification("failed")

    def test_invalid_matrix_rejects_single_turn_claimed_as_multi_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matrix.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "openubmc.skill-routing-matrix.v1",
                        "cases": [
                            {
                                "case_id": "bad",
                                "intent": "multi-turn",
                                "language": "zh",
                                "workspace_mode": "ordinary",
                                "expected_routes": ["openubmc-debug"],
                                "turns": ["one"],
                            }
                        ],
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "multi-turn"):
                load_matrix(path)

    def test_multi_turn_route_requires_context_before_domain_skill_read(self) -> None:
        case = {
            "intent": "multi-turn",
            "expected_routes": ["openubmc-debug"],
        }
        inventory = [{"name": "openubmc-debug", "enabled": True}]

        passed = evaluate_route(
            case,
            {"skill_reads": ["openubmc-debug"], "mcp_calls": []},
            inventory,
            turn_observations=[
                {"skill_reads": [], "mcp_calls": []},
                {"skill_reads": ["openubmc-debug"], "mcp_calls": []},
            ],
        )
        premature = evaluate_route(
            case,
            {"skill_reads": ["openubmc-debug"], "mcp_calls": []},
            inventory,
            turn_observations=[
                {"skill_reads": ["openubmc-debug"], "mcp_calls": []},
                {"skill_reads": ["openubmc-debug"], "mcp_calls": []},
            ],
        )

        self.assertEqual(
            [turn["classification"] for turn in passed["turns"]],
            ["passed", "passed"],
        )
        self.assertEqual(premature["turns"][0]["classification"], "wrong-route")
        self.assertEqual(premature["status"], "failed")

    def test_pair_comparison_rejects_a_different_model(self) -> None:
        evidence_root = ROOT / "evaluation/plugin-tasks/routing-evidence"
        baseline = json.loads((evidence_root / "routing-evidence-loose.json").read_text())
        candidate = copy.deepcopy(
            json.loads((evidence_root / "routing-evidence-plugin.json").read_text())
        )
        candidate["arm_identity"]["execution"]["model"] = "different-model"
        candidate["digest"] = document_digest(candidate)

        with self.assertRaisesRegex(ValueError, "paired execution"):
            compare_arm_evidence(baseline, candidate)

    def test_pair_comparison_rejects_unverified_or_failed_evidence(self) -> None:
        evidence_root = ROOT / "evaluation/plugin-tasks/routing-evidence"
        baseline = json.loads((evidence_root / "routing-evidence-loose.json").read_text())
        candidate = json.loads((evidence_root / "routing-evidence-plugin.json").read_text())
        unverified = copy.deepcopy(baseline)
        unverified["integrity"]["status"] = "unverified"
        execution_failed = copy.deepcopy(baseline)
        execution_failed["samples"][0]["failure_layers"]["execution"]["status"] = "failed"

        with self.assertRaisesRegex(ValueError, "baseline evidence integrity"):
            compare_arm_evidence(unverified, candidate)
        with self.assertRaisesRegex(ValueError, "baseline evidence execution"):
            compare_arm_evidence(execution_failed, candidate)

    def test_rebuilt_pair_has_verified_identity_and_expected_route_counts(self) -> None:
        evidence_root = ROOT / "evaluation/plugin-tasks/routing-evidence"
        baseline = json.loads((evidence_root / "routing-evidence-loose.json").read_text())
        candidate = json.loads((evidence_root / "routing-evidence-plugin.json").read_text())
        comparison = json.loads((evidence_root / "routing-comparison.json").read_text())

        self.assertEqual(baseline["integrity"]["status"], "verified")
        self.assertEqual(candidate["integrity"]["status"], "verified")
        self.assertEqual(baseline["summary"]["routing_passed"], 2)
        self.assertEqual(candidate["summary"]["routing_passed"], 12)
        self.assertEqual(comparison["summary"]["routing_pass_delta"], 10)
        self.assertEqual(comparison["summary"]["discoverability_improved"], 9)
        self.assertEqual(comparison["summary"]["not_comparable_treatment"], 1)
        comparison_cases = {sample["case_id"]: sample for sample in comparison["cases"]}
        self.assertEqual(
            comparison_cases["kb-en-ordinary"]["change"],
            "not-comparable-treatment",
        )
        baseline_cases = {sample["case_id"]: sample for sample in baseline["samples"]}
        self.assertEqual(baseline_cases["negative-zh-ordinary"]["route"]["status"], "passed")
        self.assertEqual(baseline_cases["ambiguous-zh-ordinary"]["route"]["status"], "passed")
        recovered = baseline_cases["build-en-source-cwd"]["failure_layers"]["execution"]
        self.assertEqual(recovered["status"], "passed")
        self.assertEqual(recovered["classifications"], ["model-transport-recovered"])
        multi_turn = next(
            sample for sample in candidate["samples"] if sample["case_id"] == "context-en-multi-turn"
        )
        self.assertEqual(
            [turn["classification"] for turn in multi_turn["route"]["turns"]],
            ["passed", "passed"],
        )
        self.assertEqual(baseline["raw_evidence"]["boundary"], "local-only")
        self.assertFalse(baseline["raw_evidence"]["embedded"])

        for name in ("routing-arm-loose.json", "routing-arm-plugin.json"):
            identity = load_arm_identity(evidence_root / name)
            self.assertEqual(identity["source"]["commit"], "3a898818b5ea5bd5f810ecd3c22de6617ae617da")
            self.assertEqual(identity["source"]["tree"], "eab8fda257f1d9802cf5fad82f10223e1f8820e1")

    def test_checked_in_report_is_rebuilt_from_the_comparison(self) -> None:
        evidence_root = ROOT / "evaluation/plugin-tasks/routing-evidence"
        comparison = json.loads((evidence_root / "routing-comparison.json").read_text())

        self.assertEqual(
            render_comparison_report(comparison),
            (evidence_root / "routing-report.md").read_text(),
        )

    def test_checked_in_evaluator_identity_matches_its_declared_commit(self) -> None:
        evidence_root = ROOT / "evaluation/plugin-tasks/routing-evidence"
        baseline = json.loads((evidence_root / "routing-evidence-loose.json").read_text())
        candidate = json.loads((evidence_root / "routing-evidence-plugin.json").read_text())
        comparison = json.loads((evidence_root / "routing-comparison.json").read_text())
        expected = baseline["evaluator"]
        observed = verify_evaluator_identity(
            ROOT,
            expected["commit"],
            ROOT / "scripts/skill_routing_evaluation.py",
            ROOT / "evaluation/plugin-tasks/routing-matrix.json",
            ROOT / "evaluation/plugin-tasks/routing-review-contract.json",
        )

        self.assertEqual(observed, expected)
        self.assertEqual(candidate["evaluator"], expected)
        self.assertEqual(comparison["paired_identity"]["evaluator"], expected)


if __name__ == "__main__":
    unittest.main()
