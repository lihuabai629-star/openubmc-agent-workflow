"""Agent MCP normalization must preserve typed scope and Runtime authority."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    JsonRpcMcpEndpoint,
    ObservationQuery,
    RuntimeMcpService,
    decode_run_command,
)
from openubmc_target_runtime.agent_input import (  # noqa: E402
    AgentInputAdapter,
    normalize_agent_arguments,
)
from tests.test_agent_gateway import (  # noqa: E402
    SemanticBackend,
    accepted_diagnosis_payload,
)


class AgentInputNormalizationTests(unittest.TestCase):
    def test_documented_legacy_observe_is_the_same_typed_query(self) -> None:
        canonical = {
            "target": "192.0.2.10",
            "selectors": [{"kind": "capability", "names": ["ssh"]}],
            "freshness": {"mode": "live", "max_age_seconds": 0},
            "deadline": 120,
        }
        legacy = {
            "bmc_ip": "192.0.2.10",
            "selector": json.dumps(canonical["selectors"][0]),
            "freshness": '{"mode":"live","max_age_seconds":"0"}',
            "deadline": "120",
        }
        self.assertEqual(normalize_agent_arguments("observe", canonical), canonical)
        normalized = normalize_agent_arguments("observe", legacy)
        self.assertEqual(normalized, canonical)
        self.assertEqual(
            ObservationQuery.from_query(normalized),
            ObservationQuery.from_query(canonical),
        )

    def test_documented_legacy_execute_is_the_same_typed_command(self) -> None:
        canonical = {
            "kind": "start",
            "target": "192.0.2.10",
            "intent": "diagnosis-only",
            "delivery_strategy": "source-only",
            "entry_operation": "debug_run",
            "entry_arguments": {"disk_id": "Disk23"},
            "deadline": 120,
        }
        legacy = {
            "actionKind": "START",
            "target_ip": "192.0.2.10",
            "intent": "diagnosis-only",
            "deliveryStrategy": "source-only",
            "entryOperation": "debug_run",
            "entryArguments": '{"disk_id":"Disk23"}',
            "deadline": "120",
        }
        self.assertEqual(normalize_agent_arguments("execute", canonical), canonical)
        normalized = normalize_agent_arguments("execute", legacy)
        self.assertEqual(normalized, canonical)
        self.assertEqual(
            decode_run_command(normalized, operation_id="legacy-start"),
            decode_run_command(canonical, operation_id="legacy-start"),
        )

    def test_conflicting_aliases_fail_and_unknown_fields_remain_strict(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicting values for target"):
            normalize_agent_arguments(
                "observe",
                {"target": "192.0.2.10", "target_ip": "192.0.2.11"},
            )
        with self.assertRaisesRegex(ValueError, "conflicting values for kind"):
            normalize_agent_arguments(
                "execute", {"kind": "start", "action_type": "resume"}
            )
        with self.assertRaisesRegex(ValueError, "conflicting values for gate_version"):
            normalize_agent_arguments(
                "execute", {"gate_version": True, "gateVersion": "1"}
            )
        self.assertEqual(
            normalize_agent_arguments(
                "execute", {"kind": "start", "authorization": "approved"}
            ),
            {"kind": "start", "authorization": "approved"},
        )

    def test_current_turn_carries_only_omitted_identity(self) -> None:
        adapter = AgentInputAdapter()
        adapter.remember("task-a", {
            "run_id": "run-one",
            "state": "waiting_response",
            "gate": {
                "kind": "phase",
                "gate_id": "gate-one",
                "gate_version": 2,
                "schema_digest": "sha256:" + "a" * 64,
            },
        })
        omitted = adapter.normalize(
            "execute", {"kind": "respond", "response": {"status": "failed"}},
            task_id="task-a",
        )
        self.assertEqual(omitted["run_id"], "run-one")
        self.assertEqual(omitted["gate_id"], "gate-one")
        self.assertEqual(omitted["gate_version"], 2)
        self.assertEqual(omitted["schema_digest"], "sha256:" + "a" * 64)
        self.assertNotIn("submission_id", omitted)
        self.assertEqual(
            adapter.normalize(
                "execute", {"kind": "respond", "response": {}},
                task_id="other-task",
            ),
            {"kind": "respond", "response": {}},
        )
        for changed in (
            {"run_id": "run-other"},
            {"gate_id": "gate-stale"},
            {"gate_version": 3},
            {"schema_digest": "sha256:" + "b" * 64},
        ):
            with self.subTest(changed=changed):
                action = adapter.normalize(
                    "execute",
                    {"kind": "respond", "response": {}, **changed},
                    task_id="task-a",
                )
                self.assertEqual({key: action[key] for key in changed}, changed)
                self.assertFalse(
                    all(field in action for field in (
                        "gate_id", "gate_version", "schema_digest"
                    ))
                )
        adapter.forget("task-a")
        self.assertNotIn("run_id", adapter.normalize(
            "execute", {"kind": "resume"}, task_id="task-a"
        ))


class DirectMcpNormalizationTests(unittest.TestCase):
    def _endpoint(self, task_id: str) -> tuple[SemanticBackend, RuntimeMcpService, JsonRpcMcpEndpoint]:
        backend = SemanticBackend()
        service = RuntimeMcpService(backend)
        self.addCleanup(service.close)
        return backend, service, JsonRpcMcpEndpoint(service, session_task_id=task_id)

    @staticmethod
    def _call(endpoint: JsonRpcMcpEndpoint, name: str, arguments: dict[str, object], request_id: int = 1) -> dict[str, object]:
        response = endpoint.handle({
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        assert response is not None
        return response["result"]

    def test_direct_mcp_legacy_observe_has_canonical_result_and_one_dispatch(self) -> None:
        canonical = {
            "target": "192.0.2.10",
            "selectors": [{"kind": "capability", "names": ["ssh"]}],
        }
        legacy = {
            "target_ip": "192.0.2.10",
            "selectors": '[{"kind":"capability","names":["ssh"]}]',
        }
        first_backend, _, first_endpoint = self._endpoint("canonical-observe")
        second_backend, _, second_endpoint = self._endpoint("legacy-observe")
        first = self._call(first_endpoint, "observe", canonical)
        second = self._call(second_endpoint, "observe", legacy)
        self.assertFalse(first["isError"])
        self.assertFalse(second["isError"])
        self.assertEqual(first["structuredContent"]["coverage"], second["structuredContent"]["coverage"])
        self.assertEqual(first["structuredContent"]["results"], second["structuredContent"]["results"])
        self.assertEqual(len(first_backend.calls), 1)
        self.assertEqual(len(second_backend.calls), 1)
        self.assertEqual(first_backend.calls[0][1]["selectors"], second_backend.calls[0][1]["selectors"])

    def test_direct_mcp_turn_binding_and_preflight_do_not_repeat_effect(self) -> None:
        backend, service, endpoint = self._endpoint("turn-identity")
        waiting = self._call(endpoint, "execute", {
            "action_type": "start", "bmc_ip": "192.0.2.10",
            "intent": "diagnose-and-fix", "deliveryStrategy": "source-only",
        })
        self.assertFalse(waiting["isError"])
        turn = waiting["structuredContent"]
        self.assertEqual(turn["state"], "waiting_response")
        self.assertEqual(turn["gate"]["name"], "diagnosis.acceptance")
        evidence_ids = [item["evidence_id"] for item in turn["diagnostic_receipt"]["evidence"]]
        response = {
            "status": "completed", "summary": "source diagnosis verified",
            "payload": accepted_diagnosis_payload(evidence_ids),
        }
        before = len(backend.calls)
        malformed = self._call(endpoint, "execute", {
            "kind": "respond", "gateId": "bad gate id",
            "response": json.dumps(response),
        }, request_id=2)
        self.assertTrue(malformed["isError"])
        self.assertEqual(len(backend.calls), before)
        self.assertIsNone(malformed["structuredContent"]["next_action"])
        accepted = self._call(endpoint, "execute", {
            "kind": "respond", "response": json.dumps(response),
        }, request_id=3)
        self.assertFalse(accepted["isError"])
        self.assertEqual(accepted["structuredContent"]["run_id"], turn["run_id"])
        self.assertEqual(accepted["structuredContent"]["gate"]["name"], "developer.change")
        self.assertEqual(len(backend.calls), before)
        service.complete_task("turn-identity")
        missing = self._call(endpoint, "execute", {"kind": "resume"}, request_id=4)
        self.assertTrue(missing["isError"])
        self.assertIsNone(missing["structuredContent"]["next_action"])

    def test_recoverable_preflight_has_one_complete_canonical_retry(self) -> None:
        backend, _, endpoint = self._endpoint("one-call-retry")
        query = {
            "target_ip": "192.0.2.10",
            "selectors": '[{"kind":"capability","names":["ssh"]}]',
            "deadline": "later",
        }
        invalid = self._call(endpoint, "observe", query)
        self.assertTrue(invalid["isError"])
        self.assertEqual(backend.calls, [])
        candidate = invalid["structuredContent"]["next_action"]
        self.assertIsInstance(candidate, dict)
        self.assertEqual(candidate["target"], "192.0.2.10")
        self.assertEqual(candidate["selectors"], [{"kind": "capability", "names": ["ssh"]}])
        self.assertEqual(candidate["deadline"], 180)
        self.assertNotIn("target_ip", candidate)
        ObservationQuery.from_query(candidate)
        retried = self._call(endpoint, "observe", candidate, request_id=2)
        self.assertFalse(retried["isError"])
        self.assertEqual(len(backend.calls), 1)

    def test_preflight_example_includes_current_gate_without_new_response(self) -> None:
        backend, _, endpoint = self._endpoint("gate-example")
        started = self._call(endpoint, "execute", {
            "kind": "start", "target": "192.0.2.10",
            "intent": "diagnose-and-fix", "delivery_strategy": "source-only",
        })
        turn = started["structuredContent"]
        evidence_ids = [item["evidence_id"] for item in turn["diagnostic_receipt"]["evidence"]]
        response = {
            "status": "completed", "summary": "source diagnosis verified",
            "payload": accepted_diagnosis_payload(evidence_ids),
        }
        before = len(backend.calls)
        invalid = self._call(endpoint, "execute", {
            "actionKind": "RESPOND", "response": json.dumps(response),
            "deadline": "later",
        }, request_id=2)
        self.assertTrue(invalid["isError"])
        self.assertEqual(len(backend.calls), before)
        candidate = invalid["structuredContent"]["next_action"]
        self.assertEqual(candidate["run_id"], turn["run_id"])
        self.assertEqual(candidate["gate_id"], turn["gate"]["gate_id"])
        self.assertEqual(candidate["gate_version"], turn["gate"]["gate_version"])
        self.assertEqual(candidate["schema_digest"], turn["gate"]["schema_digest"])
        self.assertEqual(candidate["response"], response)
        decode_run_command(candidate, operation_id="canonical-example")

    def test_ambiguous_target_and_authorization_fields_never_dispatch(self) -> None:
        backend, _, endpoint = self._endpoint("strict-input")
        for request_id, arguments in enumerate((
            {
                "kind": "start", "target": "192.0.2.10",
                "target_ip": "192.0.2.11", "intent": "diagnosis-only",
            },
            {
                "kind": "start", "target": "192.0.2.10",
                "intent": "diagnosis-only", "authorization": "approved",
            },
            {
                "kind": "control", "run_id": "unknown-run",
                "command": "reconcile", "effect_id": "unknown-effect",
            },
        ), start=1):
            with self.subTest(arguments=arguments):
                result = self._call(endpoint, "execute", arguments, request_id=request_id)
                self.assertTrue(result["isError"])
                self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
