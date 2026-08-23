from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "agent_gateway_ab.py"
SPEC = importlib.util.spec_from_file_location("agent_gateway_ab", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


EXPECTED_QUERIES = [
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Name",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Protocol",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive ResourceId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive SlotNumber",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Presence",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive TemperatureCelsius",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo Type",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo SocketId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.DriveStatus Health",
]


def signing_keys(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    private_key = root / "qualification-key"
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(private_key),
        ],
        check=True,
    )
    return private_key, Path(f"{private_key}.pub")


def initialize_git_repository(root: Path) -> None:
    module.subprocess.run(["git", "init"], cwd=root, check=True)
    module.subprocess.run(
        ["git", "config", "user.email", "benchmark@example.invalid"],
        cwd=root,
        check=True,
    )
    module.subprocess.run(
        ["git", "config", "user.name", "Benchmark Test"],
        cwd=root,
        check=True,
    )
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    module.subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    module.subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True)


def signed_run_evidence(
    root: Path,
    value: dict[str, object],
    *,
    private_key: Path,
    public_key: Path,
) -> dict[str, object]:
    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(public_key), "-E", "sha256"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.split()[1]
    runs = value["runs"]
    assert isinstance(runs, list)
    for index, run in enumerate(runs, 1):
        assert isinstance(run, dict)
        payload = root / f"run-{index}.json"
        payload.write_text(
            json.dumps(
                run,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-q",
                "-f",
                str(private_key),
                "-n",
                "openubmc-agent-gateway-ab",
                str(payload),
            ],
            check=True,
        )
        run["attestation"] = {
            "schema": module.RUN_ATTESTATION_SCHEMA,
            "identity": "openubmc-agent-workflow-qualification",
            "namespace": "openubmc-agent-gateway-ab",
            "key_fingerprint": fingerprint,
            "signature": base64.b64encode(
                Path(f"{payload}.sig").read_bytes()
            ).decode("ascii"),
        }
    return value


def passing_run_fields():
    return {
        "exit_code": 0,
        "semantic_acceptance": {"passed": True},
        "scope_acceptance": True,
        "scope_validation": {"passed": True},
    }


def passing_metrics(schedule):
    return module.metrics_from_run_evidence(passing_execute_run_evidence(schedule))


def verify_passing_summary(
    *,
    candidate_commit: str,
    baseline_commit: str,
    run_candidate_commit: str | None = None,
):
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        schedule = module.balanced_schedule(10, seed=7)
        run_evidence = passing_execute_run_evidence(
            schedule,
            candidate_commit=run_candidate_commit or candidate_commit,
            baseline_commit=baseline_commit,
        )
        private_key, public_key = signing_keys(root)
        signed_run_evidence(
            root,
            run_evidence,
            private_key=private_key,
            public_key=public_key,
        )
        metrics = module.metrics_from_run_evidence(run_evidence)
        metrics_path = root / "all_metrics.json"
        schedule_path = root / "schedule.json"
        run_evidence_path = write_run_evidence(root, run_evidence)
        metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
        schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
        analysis = module.analyze(metrics)
        analysis["release_evidence"] = module.release_evidence(
            scenario="execute-source-only",
            requested_pairs=10,
            candidate_source_commit=candidate_commit,
            baseline_source_commit=baseline_commit,
            model=module.QUALIFICATION_MODEL,
            codex_config=module.QUALIFICATION_CODEX_CONFIG,
            metrics_path=metrics_path,
            schedule_path=schedule_path,
            run_evidence_path=run_evidence_path,
            analysis=analysis,
            environment={"python": "3.12", "node": "v22"},
        )
        summary_path = root / "summary.json"
        summary_path.write_text(json.dumps(analysis), encoding="utf-8")
        return module.verify_summary(
            summary_path,
            expected_source_commit=candidate_commit,
            expected_baseline_commit=module.DEFAULT_BASELINE_REF,
            attestation_public_key=public_key,
        )


def candidate_observe_event(*, queries=None, complete: bool = True):
    selected_queries = EXPECTED_QUERIES if queries is None else queries
    receipt_id = "observation-test"
    return {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": "observe",
            "arguments": {
                "target": "10.121.136.200",
                "freshness": {"mode": "live", "max_age_seconds": 0},
                "selectors": [
                    {
                        "id": "capabilities",
                        "kind": "capability",
                        "names": ["SSH", "Telnet", "MDBCTL", "BUSCTL"],
                    },
                    {"id": "drive", "kind": "mdb", "queries": selected_queries},
                ],
            },
            "result": {
                "structured_content": {
                    "receipt_id": receipt_id,
                    "status": "complete" if complete else "incomplete",
                    "coverage": {
                        "requested": 13,
                        "available": 13 if complete else 12,
                        "unavailable": 0,
                        "not_checked": 0 if complete else 1,
                        "complete": complete,
                    },
                    "results": {
                        "capabilities": {
                            "kind": "capability",
                            "values": [
                                {"name": name, "status": "available"}
                                for name in ("ssh", "telnet", "mdbctl", "busctl")
                            ],
                        },
                        "drive": {
                            "kind": "mdb",
                            "values": [
                                {"query_index": index, "status": "available", "value": index}
                                for index in range(9)
                            ],
                        },
                    },
                    "claims": [
                        {
                            "selector_id": selector_id,
                            "status": "grounded",
                            "receipt_id": receipt_id,
                        }
                        for selector_id in ("capabilities", "drive")
                    ],
                }
            },
        },
    }


def candidate_execute_event(kind: str, state: str, *, elapsed: float):
    structured = {
        "run_id": "run-qualified",
        "state": state,
        "gate": None,
        "outcome": None,
    }
    arguments = {"kind": kind}
    if kind == "start":
        arguments.update(
            {
                "target": "10.121.136.200",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            }
        )
        structured["gate"] = {
            "gate_id": "gate-developer",
            "gate_version": 1,
            "schema_digest": "a" * 64,
            "owner": "openubmc-developer",
        }
    else:
        arguments.update(
            {
                "run_id": "run-qualified",
                "gate_id": "gate-developer",
                "gate_version": 1,
                "schema_digest": "a" * 64,
                "response": {
                    "status": "completed",
                    "summary": "qualification source-only receipt completed",
                    "payload": {
                        "source_revision": "qualification-source",
                        "authored_files": ["src/qualification.lua"],
                        "verification_plan": ["run qualification tests"],
                    },
                },
            }
        )
        structured["outcome"] = {"status": "completed"}
    return {
        "type": "item.completed",
        "observed_elapsed_seconds": elapsed,
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": "execute",
            "arguments": arguments,
            "result": {"structured_content": structured},
        },
    }


def baseline_execute_event(tool: str, arguments, structured, *, elapsed: float):
    return {
        "type": "item.completed",
        "observed_elapsed_seconds": elapsed,
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": tool,
            "arguments": arguments,
            "result": {"structured_content": structured},
        },
    }


def baseline_phase_contract(**changes):
    contract = {
        "receipt_schema": "openubmc.target-runtime.v1/developer-change-receipt-v1",
        "case_id": "case-qualified",
        "expected_revision": 2,
        "idempotency_key": "qualification-development",
        "phase_type": "developer.change",
        "producer_identity": "openubmc-developer",
    }
    contract.update(changes)
    return contract


def baseline_start_arguments():
    return {
        "ip": "10.121.136.200",
        "intent": "diagnose-and-fix",
        "delivery_strategy": "source-only",
        "final_purpose": "qualify Runtime source-only execution",
    }


def baseline_metric_from_events(events):
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        events_path = root / "events.jsonl"
        events_path.write_text(
            "\n".join(json.dumps(item) for item in events) + "\n",
            encoding="utf-8",
        )
        final_path = root / "final.md"
        final_path.write_text(
            "source-only Runtime Outcome completed", encoding="utf-8"
        )
        return module.metric_from_run(
            arm="A",
            pair=1,
            order=1,
            events_path=events_path,
            final_path=final_path,
            exit_code=0,
            duration_seconds=31,
            scenario="execute-source-only",
        )


def passing_execute_run_evidence(
    schedule,
    *,
    candidate_commit: str = "a" * 40,
    baseline_commit: str = module.DEFAULT_BASELINE_REF,
):
    runs = []
    for pair, first, second in schedule:
        for order, arm in enumerate((first, second), 1):
            execution_id = str(
                uuid.UUID(int=(pair * 2) + (0 if arm == "A" else 1))
            )
            if arm == "B":
                events = [
                    {"type": "thread.started", "thread_id": execution_id},
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "start"},
                    },
                    candidate_execute_event("start", "waiting_response", elapsed=0.5),
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "respond"},
                    },
                    candidate_execute_event("respond", "completed", elapsed=1.5),
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 80,
                            "cached_input_tokens": 20,
                            "output_tokens": 8,
                        },
                    },
                ]
                duration = 2.0
            else:
                phase_contract = {
                    "case_id": "case-qualified",
                    "expected_revision": 2,
                    "idempotency_key": "qualification-development",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                }
                events = [
                    {"type": "thread.started", "thread_id": execution_id},
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "start"},
                    },
                    baseline_execute_event(
                        "workflow.advance",
                        {
                            "ip": "10.121.136.200",
                            "intent": "diagnose-and-fix",
                            "delivery_strategy": "source-only",
                            "final_purpose": "qualify Runtime source-only execution",
                        },
                        {
                            "status": "waiting_phase_record",
                            "required_skill": "openubmc-developer",
                            "handoff_arguments": {
                                "phase_record_contract": phase_contract,
                            },
                            "agent_envelope": {
                                "case_id": "case-qualified",
                                "revision": 5,
                            },
                        },
                        elapsed=1.0,
                    ),
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "record"},
                    },
                    baseline_execute_event(
                        "phase_record",
                        {
                            **phase_contract,
                            "expected_revision": 5,
                            "status": "completed",
                            "source_revision": "qualification-source",
                            "summary": "qualification source-only receipt completed",
                            "authored_files": ["src/qualification.lua"],
                            "verification_plan": ["run qualification tests"],
                        },
                        {"status": "completed", "case_id": "case-qualified"},
                        elapsed=2.0,
                    ),
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "continue"},
                    },
                    baseline_execute_event(
                        "workflow.next",
                        {"case_id": "case-qualified"},
                        {
                            "status": "completed",
                            "completed": True,
                            "case_id": "case-qualified",
                        },
                        elapsed=3.0,
                    ),
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 10,
                        },
                    },
                ]
                duration = 4.0
            events.append(
                {
                    "type": "runner.completed",
                    "exit_code": 0,
                    "duration_seconds": duration,
                }
            )
            runs.append(
                {
                    "scenario": "execute-source-only",
                    "arm": arm,
                    "pair": pair,
                    "order": order,
                    "source_commit": (
                        candidate_commit if arm == "B" else baseline_commit
                    ),
                    "execution_id": execution_id,
                    "events": events,
                    "final": "source-only Runtime Outcome completed",
                }
            )
    return {
        "schema": module.RUN_EVIDENCE_SCHEMA,
        "source": {
            "candidate_commit": candidate_commit,
            "baseline_commit": baseline_commit,
        },
        "runs": runs,
    }


def write_run_evidence(root: Path, value=None) -> Path:
    path = root / "run_evidence.json"
    document = value or {"schema": module.RUN_EVIDENCE_SCHEMA, "runs": []}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class AgentGatewayAbTests(unittest.TestCase):
    def test_pinned_source_check_rejects_candidate_ref_drift(self) -> None:
        repo = Path("/repo")
        candidate = Path("/candidate")
        baseline = Path("/baseline")

        def commit(root, ref):
            if root == repo and ref == "HEAD":
                return "c" * 40
            return "a" * 40 if root == candidate else "b" * 40

        with patch.object(module, "_require_clean_source"), patch.object(
            module, "_git_commit", side_effect=commit
        ), self.assertRaisesRegex(RuntimeError, "candidate source moved"):
            module._require_pinned_sources(
                repo=repo,
                candidate_root=candidate,
                baseline_root=baseline,
                candidate_commit="a" * 40,
                baseline_commit="b" * 40,
                baseline_ref="baseline-ref",
            )

    def test_run_benchmark_rejects_a_dirty_candidate_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initialize_git_repository(root)
            (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            args = module.argparse.Namespace(repo=root)

            with patch.object(module, "_prepare_worktree") as prepare, self.assertRaisesRegex(
                RuntimeError, "clean candidate repository"
            ):
                module.run_benchmark(args)

        prepare.assert_not_called()

    def test_run_benchmark_rejects_a_dirty_reused_candidate_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            initialize_git_repository(repo)
            source_commit = module._git_commit(repo, "HEAD")
            work_root = root / "work"
            baseline_root = work_root / "variants" / f"baseline-{source_commit[:12]}"
            candidate_root = work_root / "variants" / f"candidate-{source_commit[:12]}"
            module._prepare_worktree(repo, baseline_root, source_commit)
            module._prepare_worktree(repo, candidate_root, source_commit)
            (candidate_root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            private_key, public_key = signing_keys(root / "keys")
            credentials = root / "credentials.env"
            credentials.write_text("OPENUBMC_USERNAME=test\n", encoding="utf-8")
            args = module.argparse.Namespace(
                repo=repo,
                model=module.QUALIFICATION_MODEL,
                codex_config=list(module.QUALIFICATION_CODEX_CONFIG),
                attestation_private_key=private_key,
                attestation_public_key=public_key,
                work_root=work_root,
                baseline_ref="HEAD",
                output=None,
                pairs=1,
                seed=1,
                credentials=credentials,
                only_arm="B",
                scenario="execute-source-only",
                codex="codex",
                codex_cwd=repo,
                pause_seconds=0,
            )

            with patch.object(module, "prepare_arm_home"), patch.object(
                module, "_run", side_effect=AssertionError("benchmark executed")
            ) as execute, self.assertRaisesRegex(
                RuntimeError, "clean benchmark worktree"
            ):
                module.run_benchmark(args)

        execute.assert_not_called()

    def test_run_benchmark_rejects_a_noncanonical_model_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initialize_git_repository(root)
            args = module.argparse.Namespace(
                repo=root,
                model="different-model",
                codex_config=list(module.QUALIFICATION_CODEX_CONFIG),
            )

            with patch.object(module, "_prepare_worktree") as prepare, self.assertRaisesRegex(
                RuntimeError, "qualification model"
            ):
                module.run_benchmark(args)

        prepare.assert_not_called()

    def test_run_benchmark_rejects_an_untrusted_attestation_key_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as key_raw:
            root = Path(raw)
            initialize_git_repository(root)
            private_key, _ = signing_keys(Path(key_raw) / "trusted")
            _, different_public_key = signing_keys(Path(key_raw) / "different")
            args = module.argparse.Namespace(
                repo=root,
                model=module.QUALIFICATION_MODEL,
                codex_config=list(module.QUALIFICATION_CODEX_CONFIG),
                attestation_private_key=private_key,
                attestation_public_key=different_public_key,
            )

            with patch.object(module, "_prepare_worktree") as prepare, self.assertRaisesRegex(
                RuntimeError, "does not match"
            ):
                module.run_benchmark(args)

        prepare.assert_not_called()

    def test_prepare_arm_home_links_selected_skill_for_supported_clients(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            skill_sources = {
                name: source_root / name
                for name in ("openubmc-debug", "openubmc-developer")
            }
            for skill_source in skill_sources.values():
                skill_source.mkdir(parents=True)
            home = root / "home"
            home.mkdir()

            module.prepare_arm_home(home, source_root)

            for client_root in (".agents", ".codex"):
                for name, skill_source in skill_sources.items():
                    link = home / client_root / "skills" / name
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), skill_source.resolve())

    def test_schedule_is_balanced_and_deterministic(self) -> None:
        first = module.balanced_schedule(10, seed=7)
        second = module.balanced_schedule(10, seed=7)
        self.assertEqual(first, second)
        orders = [(a, b) for _pair, a, b in first]
        self.assertEqual(orders.count(("A", "B")), 5)
        self.assertEqual(orders.count(("B", "A")), 5)

    def test_candidate_execute_prompt_uses_the_current_gate_response_shape(self) -> None:
        prompt = module._prompt(
            Path("/tmp/openubmc-debug/SKILL.md"),
            scenario="execute-source-only",
            arm="B",
        )

        self.assertIn("读取 start 工具结果的 structured_content", prompt)
        self.assertIn(
            '"kind":"respond","run_id":"<structured_content.run_id>",'
            '"gate_id":"<structured_content.gate.gate_id>",'
            '"gate_version":<structured_content.gate.gate_version>,'
            '"schema_digest":"<structured_content.gate.schema_digest>",'
            '"response":{"status":"completed",',
            prompt,
        )
        self.assertIn(
            "kind、run_id、gate_id、gate_version、schema_digest、response "
            "都是 respond 参数的顶层字段",
            prompt,
        )
        self.assertIn("response 内只含 status、summary、payload", prompt)
        self.assertNotIn("response 只含上述固定 receipt", prompt)

    def test_skill_disclosure_uses_the_same_agent_profile_and_prompt_semantics(self) -> None:
        baseline = Path("/tmp/baseline/openubmc-debug/SKILL.md")
        candidate = Path("/tmp/candidate/openubmc-debug/SKILL.md")

        configs = module.run_configs(
            "skill-disclosure",
            baseline.parent.parent,
            candidate.parent.parent,
        )
        first = module._prompt(baseline, scenario="skill-disclosure", arm="A")
        second = module._prompt(candidate, scenario="skill-disclosure", arm="B")

        self.assertEqual(configs["A"].interface_profile, "agent")
        self.assertEqual(configs["B"].interface_profile, "agent")
        self.assertEqual(first, second)
        self.assertIn("$openubmc-debug", first)
        self.assertNotIn(str(baseline), first)
        self.assertNotIn(str(candidate), second)
        self.assertIn("按需读取直接链接的 references", first)
        self.assertIn("openubmc-target-runtime.observe", first)
        self.assertIn("不要列出 MCP resources/templates", first)
        self.assertNotIn("assurance", first)
        self.assertIn(
            '"freshness":{"max_age_seconds":0,"mode":"live"}', first
        )
        self.assertIn(
            '"selectors":[{"id":"capabilities","kind":"capability",'
            '"names":["SSH","Telnet","MDBCTL","BUSCTL"]},'
            '{"id":"drive","kind":"mdb","queries":[',
            first,
        )

    def test_skill_disclosure_metrics_apply_candidate_scope_to_both_arms(self) -> None:
        events = [
            candidate_observe_event(),
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ]
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )

        for arm in ("A", "B"):
            record = module.RunEvidenceRecord.capture(
                arm=arm,
                pair=1,
                order=1 if arm == "A" else 2,
                scenario="skill-disclosure",
                events=events,
                final=final,
                exit_code=0,
                duration_seconds=1,
            )
            self.assertTrue(record.metric()["valid"], arm)

    def test_candidate_scope_rejects_unrelated_mcp_discovery(self) -> None:
        tools = [
            {
                "type": "mcp_tool_call",
                "server": "codex",
                "tool": "list_mcp_resources",
                "arguments": {},
                "result": {"structured_content": {}},
            },
            candidate_observe_event()["item"],
        ]

        validation = module.candidate_scope_acceptance(tools)

        self.assertFalse(validation["passed"])
        self.assertIn("unrelated MCP tools", validation["errors"])

    def test_candidate_scope_rejects_legacy_assurance_input(self) -> None:
        observe = candidate_observe_event()["item"]
        observe["arguments"]["assurance"] = "auto"

        validation = module.candidate_scope_acceptance([observe])

        self.assertFalse(validation["passed"])
        self.assertIn("legacy assurance input", validation["errors"])

    def test_documented_qualification_command_locks_model_and_codex_config(self) -> None:
        documentation = (
            Path(__file__).resolve().parents[2] / "docs" / "agent-semantic-gateway.md"
        ).read_text(encoding="utf-8")
        run_command = documentation.partition(
            "python scripts/agent_gateway_ab.py run \\",
        )[2].partition("\n\npython scripts/agent_gateway_ab.py verify")[0]
        tokens = shlex.split(run_command.replace("\\\n", " "))
        models = [
            tokens[index + 1]
            for index, token in enumerate(tokens[:-1])
            if token == "--model"
        ]
        codex_config = [
            tokens[index + 1]
            for index, token in enumerate(tokens[:-1])
            if token == "--codex-config"
        ]

        self.assertEqual(models, [module.QUALIFICATION_MODEL])
        self.assertEqual(tuple(codex_config), module.QUALIFICATION_CODEX_CONFIG)

    def test_candidate_execute_acceptance_binds_response_to_the_start_gate(self) -> None:
        start = candidate_execute_event(
            "start", "waiting_response", elapsed=1
        )["item"]
        respond = candidate_execute_event("respond", "completed", elapsed=2)["item"]
        respond["arguments"]["gate_id"] = "gate-from-another-run"

        accepted = module.candidate_execute_acceptance([start, respond])

        self.assertFalse(accepted["passed"], accepted)
        self.assertIn(
            "Gate response must preserve gate_id",
            accepted["errors"],
        )

    def test_candidate_execute_acceptance_requires_the_fixed_source_receipt(self) -> None:
        start = candidate_execute_event(
            "start", "waiting_response", elapsed=1
        )["item"]
        respond = candidate_execute_event("respond", "completed", elapsed=2)["item"]
        respond["arguments"]["response"]["summary"] = "different work"

        accepted = module.candidate_execute_acceptance([start, respond])

        self.assertFalse(accepted["passed"], accepted)
        self.assertIn(
            "Gate response must match the fixed source receipt",
            accepted["errors"],
        )

    def test_semantic_acceptance_requires_fields_and_cautious_conclusion(self) -> None:
        text = (
            "SSH Telnet MDBCTL BUSCTL；Name Disk0，Protocol 3，ResourceId 0，"
            "SlotNumber 0，Presence 1，TemperatureCelsius 29，Type SATA/SAS，"
            "SocketId 0，Health 0。不能单独证明 ResourceId=0 异常。"
        )
        self.assertTrue(module.semantic_acceptance(text)["passed"])
        self.assertFalse(module.semantic_acceptance(text.replace("Health", ""))["passed"])

    def test_metric_parser_requires_candidate_to_use_one_observe(self) -> None:
        events = [
            candidate_observe_event(),
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                },
            },
        ]
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text(final, encoding="utf-8")
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=2,
                scenario="observation",
            )
        self.assertTrue(metric["valid"], metric)
        self.assertEqual(metric["noncached_input_plus_output"], 90)
        self.assertEqual(metric["model_turns"], 1)
        self.assertEqual(metric["time_to_next_actionable_turn_seconds"], 2)

    def test_metric_parser_rejects_wrong_scope_or_incomplete_receipt(self) -> None:
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )
        cases = (
            candidate_observe_event(queries=EXPECTED_QUERIES[:-1]),
            candidate_observe_event(complete=False),
        )
        for index, event in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                events_path = root / "events.jsonl"
                events_path.write_text(
                    "\n".join(
                        json.dumps(item)
                        for item in (
                            event,
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 10,
                                },
                            },
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                final_path = root / "final.md"
                final_path.write_text(final, encoding="utf-8")
                metric = module.metric_from_run(
                    arm="B",
                    pair=1,
                    order=1,
                    events_path=events_path,
                    final_path=final_path,
                    exit_code=0,
                    duration_seconds=2,
                    scenario="observation",
                )
            self.assertFalse(metric["valid"])
            self.assertFalse(metric["scope_acceptance"])

    def test_execute_metric_requires_one_gate_roundtrip_without_polling(self) -> None:
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "start"}},
            candidate_execute_event("start", "waiting_response", elapsed=1.25),
            {"type": "item.completed", "item": {"type": "agent_message", "text": "respond"}},
            candidate_execute_event("respond", "completed", elapsed=2.5),
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text(
                "source-only Runtime Outcome completed，未修改目标。",
                encoding="utf-8",
            )
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=3,
                scenario="execute-source-only",
            )

        self.assertTrue(metric["valid"], metric)
        self.assertEqual(metric["model_turns"], 2)
        self.assertEqual(metric["gate_roundtrips"], 1)
        self.assertEqual(metric["resume_calls"], 0)
        self.assertEqual(metric["time_to_next_actionable_turn_seconds"], 1.25)

    def test_execute_metric_rejects_polling_or_duplicate_gate_response(self) -> None:
        events = [
            candidate_execute_event("start", "waiting_response", elapsed=1),
            candidate_execute_event("respond", "completed", elapsed=2),
            candidate_execute_event("respond", "completed", elapsed=3),
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text("source-only Runtime Outcome completed", encoding="utf-8")
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=3,
                scenario="execute-source-only",
            )

        self.assertFalse(metric["valid"])
        self.assertFalse(metric["scope_acceptance"])

    def test_baseline_execute_metric_requires_compatibility_terminal_workflow(self) -> None:
        phase_contract = baseline_phase_contract()
        events = [
            baseline_execute_event(
                "workflow.advance",
                baseline_start_arguments(),
                {
                    "status": "waiting_phase_record",
                    "required_skill": "openubmc-developer",
                    "handoff_arguments": {
                        "phase_record_contract": phase_contract,
                    },
                    "agent_envelope": {
                        "case_id": "case-qualified",
                        "revision": 4,
                    },
                },
                elapsed=5,
            ),
            baseline_execute_event(
                "workflow.advance",
                baseline_start_arguments(),
                {
                    "status": "waiting_phase_record",
                    "required_skill": "openubmc-developer",
                    "handoff_arguments": {
                        "phase_record_contract": phase_contract,
                    },
                    "agent_envelope": {
                        "case_id": "case-qualified",
                        "revision": 5,
                    },
                },
                elapsed=10,
            ),
            baseline_execute_event(
                "phase_record",
                {
                    **phase_contract,
                    "expected_revision": 5,
                    "status": "completed",
                    "source_revision": "qualification-source",
                    "summary": "qualification source-only receipt completed",
                    "authored_files": ["src/qualification.lua"],
                    "verification_plan": ["run qualification tests"],
                },
                {"status": "completed", "case_id": "case-qualified"},
                elapsed=20,
            ),
            baseline_execute_event(
                "workflow.next",
                {"case_id": "case-qualified"},
                {"status": "completed", "completed": True, "case_id": "case-qualified"},
                elapsed=30,
            ),
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ]
        metric = baseline_metric_from_events(events)

        self.assertTrue(metric["valid"])
        self.assertEqual(metric["gate_roundtrips"], 1)
        self.assertEqual(metric["mcp_events"], 4)
        self.assertEqual(metric["time_to_next_actionable_turn_seconds"], 5)

    def test_baseline_execute_metric_rejects_repeated_start_contract_drift(self) -> None:
        base_contract = baseline_phase_contract()
        scenarios = (
            (
                "different Case",
                "case-other",
                base_contract,
                "baseline repeated starts must preserve the same Case",
            ),
            (
                "different phase contract",
                "case-qualified",
                baseline_phase_contract(idempotency_key="other-development"),
                "baseline repeated starts must preserve phase contract idempotency_key",
            ),
            (
                "different receipt schema",
                "case-qualified",
                baseline_phase_contract(receipt_schema="other-receipt-v1"),
                "baseline repeated starts must preserve phase contract receipt_schema",
            ),
        )

        for name, repeated_case, repeated_contract, expected_error in scenarios:
            with self.subTest(name=name):
                events = [
                    baseline_execute_event(
                        "workflow.advance",
                        baseline_start_arguments(),
                        {
                            "status": "waiting_phase_record",
                            "required_skill": "openubmc-developer",
                            "handoff_arguments": {
                                "phase_record_contract": base_contract,
                            },
                            "agent_envelope": {
                                "case_id": "case-qualified",
                                "revision": 4,
                            },
                        },
                        elapsed=5,
                    ),
                    baseline_execute_event(
                        "workflow.advance",
                        baseline_start_arguments(),
                        {
                            "status": "waiting_phase_record",
                            "required_skill": "openubmc-developer",
                            "handoff_arguments": {
                                "phase_record_contract": repeated_contract,
                            },
                            "agent_envelope": {
                                "case_id": repeated_case,
                                "revision": 5,
                            },
                        },
                        elapsed=10,
                    ),
                    baseline_execute_event(
                        "phase_record",
                        {
                            **repeated_contract,
                            "expected_revision": 5,
                            "status": "completed",
                            "source_revision": "qualification-source",
                            "summary": "qualification source-only receipt completed",
                            "authored_files": ["src/qualification.lua"],
                            "verification_plan": ["run qualification tests"],
                        },
                        {"status": "completed", "case_id": repeated_case},
                        elapsed=20,
                    ),
                    baseline_execute_event(
                        "workflow.next",
                        {"case_id": repeated_case},
                        {
                            "status": "completed",
                            "completed": True,
                            "case_id": repeated_case,
                        },
                        elapsed=30,
                    ),
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                ]
                metric = baseline_metric_from_events(events)

                self.assertFalse(metric["valid"])
                self.assertIn(
                    expected_error,
                    metric["scope_validation"]["errors"],
                )

    def test_baseline_execute_metric_binds_contract_and_terminal_result_to_case(self) -> None:
        base_contract = baseline_phase_contract()
        scenarios = (
            (
                "contract Case divergence",
                baseline_phase_contract(case_id="case-other"),
                "case-qualified",
                "case-qualified",
                "baseline start phase contract must match the Case",
            ),
            (
                "phase result Case divergence",
                base_contract,
                "case-other",
                "case-qualified",
                "baseline phase_record result must use the same Case",
            ),
            (
                "terminal result Case divergence",
                base_contract,
                "case-qualified",
                "case-other",
                "baseline continuation result must use the same Case",
            ),
        )
        for (
            name,
            phase_contract,
            phase_result_case,
            terminal_case,
            expected_error,
        ) in scenarios:
            with self.subTest(name=name):
                events = [
                    baseline_execute_event(
                        "workflow.advance",
                        baseline_start_arguments(),
                        {
                            "status": "waiting_phase_record",
                            "required_skill": "openubmc-developer",
                            "handoff_arguments": {
                                "phase_record_contract": phase_contract,
                            },
                            "agent_envelope": {
                                "case_id": "case-qualified",
                                "revision": 5,
                            },
                        },
                        elapsed=10,
                    ),
                    baseline_execute_event(
                        "phase_record",
                        {
                            **phase_contract,
                            "expected_revision": 5,
                            "status": "completed",
                            "source_revision": "qualification-source",
                            "summary": "qualification source-only receipt completed",
                            "authored_files": ["src/qualification.lua"],
                            "verification_plan": ["run qualification tests"],
                        },
                        {"status": "completed", "case_id": phase_result_case},
                        elapsed=20,
                    ),
                    baseline_execute_event(
                        "workflow.next",
                        {"case_id": "case-qualified"},
                        {
                            "status": "completed",
                            "completed": True,
                            "case_id": terminal_case,
                        },
                        elapsed=30,
                    ),
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                ]
                metric = baseline_metric_from_events(events)

                self.assertFalse(metric["valid"])
                self.assertIn(
                    expected_error,
                    metric["scope_validation"]["errors"],
                )

    def test_baseline_execute_metric_rejects_nested_envelope_case_drift(self) -> None:
        phase_contract = baseline_phase_contract()
        scenarios = (
            (
                "start envelope",
                "case-other",
                "case-qualified",
                "case-qualified",
                "baseline start result Case must match its agent envelope",
            ),
            (
                "phase envelope",
                "case-qualified",
                "case-other",
                "case-qualified",
                "baseline phase_record result Case must match its agent envelope",
            ),
            (
                "terminal envelope",
                "case-qualified",
                "case-qualified",
                "case-other",
                "baseline continuation result Case must match its agent envelope",
            ),
        )

        for (
            name,
            start_envelope_case,
            phase_envelope_case,
            terminal_envelope_case,
            expected_error,
        ) in scenarios:
            with self.subTest(name=name):
                events = [
                    baseline_execute_event(
                        "workflow.advance",
                        baseline_start_arguments(),
                        {
                            "status": "waiting_phase_record",
                            "required_skill": "openubmc-developer",
                            "case_id": "case-qualified",
                            "handoff_arguments": {
                                "phase_record_contract": phase_contract,
                            },
                            "agent_envelope": {
                                "case_id": start_envelope_case,
                                "revision": 5,
                            },
                        },
                        elapsed=10,
                    ),
                    baseline_execute_event(
                        "phase_record",
                        {
                            **phase_contract,
                            "expected_revision": 5,
                            "status": "completed",
                            "source_revision": "qualification-source",
                            "summary": "qualification source-only receipt completed",
                            "authored_files": ["src/qualification.lua"],
                            "verification_plan": ["run qualification tests"],
                        },
                        {
                            "status": "completed",
                            "case_id": "case-qualified",
                            "agent_envelope": {"case_id": phase_envelope_case},
                        },
                        elapsed=20,
                    ),
                    baseline_execute_event(
                        "workflow.next",
                        {"case_id": "case-qualified"},
                        {
                            "status": "completed",
                            "completed": True,
                            "case_id": "case-qualified",
                            "agent_envelope": {"case_id": terminal_envelope_case},
                        },
                        elapsed=30,
                    ),
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                ]
                metric = baseline_metric_from_events(events)

                self.assertFalse(metric["valid"])
                self.assertIn(
                    expected_error,
                    metric["scope_validation"]["errors"],
                )

    def test_analyzer_passes_ten_good_pairs_and_expands_uncertain_result(self) -> None:
        passing = []
        for pair in range(1, 11):
            passing.extend(
                (
                    {
                        "arm": "A",
                        "pair": pair,
                        "valid": True,
                        **passing_run_fields(),
                        "total_tokens": 100,
                        "noncached_input_plus_output": 100,
                        "duration_seconds": 100,
                        "tool_output_bytes": 100,
                        "model_turns": 4,
                        "time_to_next_actionable_turn_seconds": 100,
                    },
                    {
                        "arm": "B",
                        "pair": pair,
                        "valid": True,
                        **passing_run_fields(),
                        "total_tokens": 105,
                        "noncached_input_plus_output": 105,
                        "duration_seconds": 105,
                        "tool_output_bytes": 105,
                        "model_turns": 4,
                        "time_to_next_actionable_turn_seconds": 105,
                    },
                )
            )
        self.assertEqual(module.analyze(passing)["decision"], "passed")

        uncertain = [dict(item) for item in passing]
        for item in uncertain:
            if item["arm"] == "B":
                item["duration_seconds"] = 130
        result = module.analyze(uncertain)
        self.assertEqual(result["decision"], "collect_more")
        self.assertEqual(result["next_pair_target"], 20)

    def test_analyzer_recomputes_validity_from_raw_run_fields(self) -> None:
        schedule = module.balanced_schedule(10, seed=7)
        for field, value in (
            ("exit_code", 1),
            ("semantic_acceptance", {"passed": False}),
            ("scope_acceptance", False),
            ("scope_validation", {"passed": False}),
        ):
            with self.subTest(field=field):
                metrics = passing_metrics(schedule)
                metrics[0][field] = value
                metrics[0]["valid"] = True
                tampered_arm = metrics[0]["arm"]

                result = module.analyze(metrics)

                self.assertEqual(result["decision"], "collect_more")
                self.assertEqual(result["valid_pairs"], 9)
                self.assertFalse(
                    result["invalid_pairs"][0]["valid"][tampered_arm]
                )
                self.assertTrue(
                    result["invalid_pairs"][0]["claimed_valid"][tampered_arm]
                )

    def test_analyzer_marks_legacy_metrics_incomplete_instead_of_crashing(self) -> None:
        legacy = []
        for pair in range(1, 11):
            for arm in ("A", "B"):
                legacy.append(
                    {
                        "arm": arm,
                        "pair": pair,
                        "valid": True,
                        "total_tokens": 100,
                        "noncached_input_plus_output": 100,
                        "duration_seconds": 100,
                        "tool_output_bytes": 100,
                    }
                )

        result = module.analyze(legacy)

        self.assertEqual(result["decision"], "collect_more")
        self.assertEqual(result["valid_pairs"], 0)
        self.assertIn(
            "model_turns",
            result["invalid_pairs"][0]["missing_or_nonpositive_metrics"]["A"],
        )

    def test_release_evidence_records_source_environment_thresholds_and_digests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics = root / "all_metrics.json"
            schedule = root / "schedule.json"
            metrics.write_text("[]\n", encoding="utf-8")
            schedule.write_text("[]\n", encoding="utf-8")
            evidence = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics,
                schedule_path=schedule,
                run_evidence_path=write_run_evidence(root),
                analysis={"valid_pairs": 10, "invalid_pairs": []},
                environment={"python": "3.12", "node": "v22"},
            )

        self.assertEqual(evidence["samples"]["valid_pairs"], 10)
        self.assertEqual(evidence["source"]["candidate_commit"], "a" * 40)
        self.assertIn("geometric_mean_ratio_max", evidence["thresholds"])
        self.assertRegex(evidence["environment_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(evidence["evidence_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_release_evidence_records_the_fixed_benchmark_contract(self) -> None:
        self.assertEqual(
            module.QUALIFICATION_CODEX_CONFIG,
            (
                "features.shell_tool=false",
                'model_provider="cliproxy"',
                'model_providers.cliproxy.name="CLIProxyAPI"',
                'model_providers.cliproxy.base_url="http://82.156.104.157/v1"',
                'model_providers.cliproxy.env_key="CLI_PROXY_API_KEY"',
                'model_providers.cliproxy.wire_api="responses"',
                "model_providers.cliproxy.supports_websockets=false",
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics = root / "all_metrics.json"
            schedule = root / "schedule.json"
            metrics.write_text("[]\n", encoding="utf-8")
            schedule.write_text("[]\n", encoding="utf-8")

            evidence = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics,
                schedule_path=schedule,
                run_evidence_path=write_run_evidence(root),
                analysis={"valid_pairs": 10, "invalid_pairs": []},
                environment={"python": "3.12", "node": "v22"},
            )

        self.assertEqual(
            evidence["benchmark"],
            {
                "target": module.BENCHMARK_TARGET,
                "prompt_digest": module.QUALIFICATION_PROMPT_DIGEST,
                "codex_config": list(module.QUALIFICATION_CODEX_CONFIG),
            },
        )

    def test_skill_disclosure_records_its_own_prompt_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics = root / "all_metrics.json"
            schedule = root / "schedule.json"
            metrics.write_text("[]\n", encoding="utf-8")
            schedule.write_text("[]\n", encoding="utf-8")

            evidence = module.release_evidence(
                scenario="skill-disclosure",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics,
                schedule_path=schedule,
                run_evidence_path=write_run_evidence(root),
                analysis={"valid_pairs": 10, "invalid_pairs": []},
                environment={"python": "3.12", "node": "v22"},
            )

        self.assertEqual(
            evidence["benchmark"]["prompt_digest"],
            "sha256:00e1a37bce6a65c5f6782ebde771a05b17f8b684e56ef8751bb9f99a9196a3e5",
        )
        self.assertNotEqual(
            evidence["benchmark"]["prompt_digest"],
            module.QUALIFICATION_PROMPT_DIGEST,
        )

    def test_documentation_covers_the_skill_disclosure_scenario(self) -> None:
        documentation = (
            Path(__file__).resolve().parents[2] / "docs" / "agent-semantic-gateway.md"
        ).read_text(encoding="utf-8")

        self.assertIn("--scenario skill-disclosure", documentation)
        self.assertIn("same Agent profile", documentation)
        self.assertIn("valid pairs", documentation)

    def test_verify_cli_uses_the_selected_baseline_ref(self) -> None:
        with patch.object(
            module, "_git_commit", side_effect=lambda _repo, ref: ref
        ), patch.object(
            module,
            "verify_summary",
            return_value={"promotable": True},
        ) as verify, patch("builtins.print"):
            result = module.main(
                [
                    "verify",
                    "summary.json",
                    "--source-ref",
                    "candidate",
                    "--baseline-ref",
                    "github/main",
                    "--scenario",
                    "skill-disclosure",
                    "--attestation-public-key",
                    "key.pub",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            verify.call_args.kwargs["expected_baseline_commit"], "github/main"
        )

    def test_verify_summary_rejects_claims_not_derived_from_raw_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text("[]\n", encoding="utf-8")
            schedule_path.write_text("[]\n", encoding="utf-8")
            analysis = {
                "schema": module.SCHEMA,
                "valid_pairs": 10,
                "invalid_pairs": [],
                "decision": "passed",
                "thresholds": dict(module.THRESHOLDS),
                "metrics": {
                    metric: {"passed": True} for metric in module.METRICS
                },
            }
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(root),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("run evidence" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_rejects_metrics_that_do_not_match_the_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            metrics = passing_metrics(schedule)
            tampered_schedule = [list(item) for item in schedule]
            tampered_schedule[0][1], tampered_schedule[0][2] = (
                tampered_schedule[0][2],
                tampered_schedule[0][1],
            )
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(tampered_schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(
                    root, passing_execute_run_evidence(schedule)
                ),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("schedule" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_accepts_analysis_recomputed_from_a_balanced_run(self) -> None:
        verified = verify_passing_summary(
            candidate_commit="a" * 40,
            baseline_commit=module.DEFAULT_BASELINE_REF,
        )

        self.assertTrue(verified["promotable"], verified)
        self.assertRegex(verified["summary_sha256"], r"^[0-9a-f]{64}$")

    def test_verify_summary_rejects_run_evidence_rebound_to_another_candidate(self) -> None:
        verified = verify_passing_summary(
            candidate_commit="b" * 40,
            baseline_commit=module.DEFAULT_BASELINE_REF,
            run_candidate_commit="a" * 40,
        )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("run source commit" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_rejects_rewritten_run_source_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate_commit = "b" * 40
            schedule = module.balanced_schedule(10, seed=7)
            run_evidence = passing_execute_run_evidence(
                schedule,
                candidate_commit="a" * 40,
                baseline_commit=module.DEFAULT_BASELINE_REF,
            )
            private_key, public_key = signing_keys(root)
            signed_run_evidence(
                root,
                run_evidence,
                private_key=private_key,
                public_key=public_key,
            )
            run_evidence["source"]["candidate_commit"] = candidate_commit
            for run in run_evidence["runs"]:
                if run["arm"] == "B":
                    run["source_commit"] = candidate_commit
            metrics = module.metrics_from_run_evidence(run_evidence)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            run_evidence_path = write_run_evidence(root, run_evidence)
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit=candidate_commit,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=run_evidence_path,
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path,
                expected_source_commit=candidate_commit,
                expected_baseline_commit=module.DEFAULT_BASELINE_REF,
                attestation_public_key=public_key,
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("attestation" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_rejects_duplicate_run_execution_identities(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate_commit = "a" * 40
            schedule = module.balanced_schedule(10, seed=7)
            run_evidence = passing_execute_run_evidence(
                schedule,
                candidate_commit=candidate_commit,
                baseline_commit=module.DEFAULT_BASELINE_REF,
            )
            run_evidence["runs"][1]["execution_id"] = run_evidence["runs"][0][
                "execution_id"
            ]
            private_key, public_key = signing_keys(root)
            signed_run_evidence(
                root,
                run_evidence,
                private_key=private_key,
                public_key=public_key,
            )
            metrics = module.metrics_from_run_evidence(run_evidence)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            run_evidence_path = write_run_evidence(root, run_evidence)
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit=candidate_commit,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=run_evidence_path,
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path,
                expected_source_commit=candidate_commit,
                expected_baseline_commit=module.DEFAULT_BASELINE_REF,
                attestation_public_key=public_key,
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("execution identity is duplicated" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_rejects_performance_fields_not_derived_from_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            run_evidence = passing_execute_run_evidence(schedule)
            metrics = module.metrics_from_run_evidence(run_evidence)
            for metric in metrics:
                if metric["arm"] == "B":
                    for name in module.METRICS:
                        metric[name] = 1
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            run_evidence_path = write_run_evidence(root, run_evidence)
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=run_evidence_path,
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertIn(
            "AB raw metrics are not derived from the run evidence",
            verified["errors"],
        )

    def test_verify_summary_rejects_a_noncanonical_baseline_commit(self) -> None:
        verified = verify_passing_summary(
            candidate_commit="a" * 40,
            baseline_commit="a" * 40,
        )

        self.assertFalse(verified["promotable"], verified)
        self.assertIn(
            "AB baseline source commit does not match the qualification contract",
            verified["errors"],
        )

    def test_verify_summary_rejects_the_candidate_as_its_own_baseline(self) -> None:
        verified = verify_passing_summary(
            candidate_commit=module.DEFAULT_BASELINE_REF,
            baseline_commit=module.DEFAULT_BASELINE_REF,
        )

        self.assertFalse(verified["promotable"], verified)
        self.assertIn(
            "AB candidate source commit must differ from the baseline commit",
            verified["errors"],
        )

    def test_verify_summary_rejects_a_different_benchmark_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            metrics = passing_metrics(schedule)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model="different-model",
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(
                    root, passing_execute_run_evidence(schedule)
                ),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertIn(
            "AB release evidence model does not match the qualification contract",
            verified["errors"],
        )

    def test_verify_summary_fails_closed_for_malformed_metric_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            metrics = passing_metrics(schedule)
            metrics[0]["arm"] = []
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(
                    root, passing_execute_run_evidence(schedule)
                ),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("raw metrics are not derived" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_recomputes_the_environment_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            metrics = passing_metrics(schedule)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            evidence = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(
                    root, passing_execute_run_evidence(schedule)
                ),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            evidence["environment_fingerprint"] = "sha256:" + "0" * 64
            evidence_without_digest = dict(evidence)
            evidence_without_digest.pop("evidence_digest")
            evidence["evidence_digest"] = module._fingerprint(
                evidence_without_digest
            )
            analysis["release_evidence"] = evidence
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("environment fingerprint" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_fails_closed_when_raw_metrics_cannot_be_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            valid_metrics = passing_metrics(schedule)
            malformed_metrics = [dict(item) for item in valid_metrics]
            malformed_metrics[0]["pair"] = {}
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text(json.dumps(malformed_metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            analysis = module.analyze(valid_metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(
                    root, passing_execute_run_evidence(schedule)
                ),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("raw metrics are not derived" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_fails_closed_for_a_malformed_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schedule = module.balanced_schedule(10, seed=7)
            metrics = passing_metrics(schedule)
            malformed_schedule = [list(item) for item in schedule]
            malformed_schedule[0][1] = []
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            schedule_path.write_text(json.dumps(malformed_schedule), encoding="utf-8")
            analysis = module.analyze(metrics)
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(
                    root, passing_execute_run_evidence(schedule)
                ),
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertFalse(verified["promotable"], verified)
        self.assertTrue(
            any("one A arm" in error for error in verified["errors"]),
            verified,
        )

    def test_verify_summary_rejects_source_mismatch_and_artifact_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text("[]\n", encoding="utf-8")
            schedule_path.write_text("[]\n", encoding="utf-8")
            analysis = {
                "schema": module.SCHEMA,
                "valid_pairs": 10,
                "invalid_pairs": [],
                "decision": "passed",
                "thresholds": dict(module.THRESHOLDS),
                "metrics": {
                    metric: {"passed": True} for metric in module.METRICS
                },
            }
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit=module.DEFAULT_BASELINE_REF,
                model=module.QUALIFICATION_MODEL,
                codex_config=module.QUALIFICATION_CODEX_CONFIG,
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                run_evidence_path=write_run_evidence(root),
                analysis=analysis,
                environment={"python": "3.12"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")
            metrics_path.write_text("tampered\n", encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="c" * 40
            )

        self.assertFalse(verified["promotable"])
        self.assertTrue(any("candidate source commit" in error for error in verified["errors"]))
        self.assertTrue(any("all_metrics digest" in error for error in verified["errors"]))


if __name__ == "__main__":
    unittest.main()
