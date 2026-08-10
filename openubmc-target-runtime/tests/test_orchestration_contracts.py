from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from openubmc_target_runtime import (
    CredentialSelector,
    CancellationToken,
    DeveloperEditIntent,
    DeliveryStrategy,
    DomainOutcome,
    MutationDomainResult,
    OpenUBMCTaskRun,
    OperationContext,
    OrchestratedMcpBackend,
    ResolvedSshCredentials,
    TargetPolicy,
    TargetSpec,
    TaskIntent,
    TaskIntentKind,
    TaskOrchestrationContext,
    TaskTargetBinding,
    TaskWorkflowOrchestrator,
    RuntimeMcpService,
)


FIXTURES = Path(__file__).parent / "fixtures" / "orchestration_intents.json"


class RecordingMcpBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def open_task(task_id: str):
        return {"task_id": task_id}

    @staticmethod
    def close_task(_task) -> None:
        return None

    @staticmethod
    def maintain_task(_task) -> int:
        return 0

    @staticmethod
    def task_status(_task) -> dict[str, object]:
        return {}

    def _capture(self, name: str, arguments) -> dict[str, object]:
        self.calls.append((name, dict(arguments)))
        return {"ok": True}

    def debug_run(self, _task, arguments, _context):
        return self._capture("debug_run", arguments)

    def debug_collect(self, _task, arguments, _context):
        return self._capture("debug_collect", arguments)

    def log_bundle_collect(self, _task, arguments, _context):
        return self._capture("log_bundle_collect", arguments)

    def live_patch_run(self, _task, arguments, _context):
        return self._capture("live_patch_run", arguments)

    def upgrade_run(self, _task, arguments, _context):
        return self._capture("upgrade_run", arguments)


class OrchestrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ssh_selector = CredentialSelector.for_ssh(
            user="Administrator",
            user_env="",
            password_env="OPENUBMC_SSH_PASSWORD",
            identity_file="",
            environ={},
        )
        self.redfish_selector = CredentialSelector.for_redfish(
            user="Administrator",
            user_env="",
            password_env="REDFISH_PASSWORD",
            environ={},
        )
        self.reference = self.target("reference", "reference", "192.0.2.10")
        self.candidate = self.target("candidate", "candidate", "192.0.2.11")

    def target(self, target_id: str, role: str, host: str) -> TaskTargetBinding:
        selectors = (self.ssh_selector, self.redfish_selector)
        return TaskTargetBinding(
            target_id=target_id,
            role=role,
            target=TargetSpec.for_credential_selectors(
                host=host,
                credential_selectors=selectors,
                policy=TargetPolicy(read_only=False, ssh_host_key_policy="strict"),
            ),
            credential_selectors=selectors,
        )

    def intent(
        self,
        value: str,
        *,
        entry_domain: str = "debug",
        delivery_strategy: str | None = None,
    ) -> TaskIntent:
        return TaskIntent.create(
            original_intent=value,
            final_purpose="find the cause and leave the requested target verified",
            entry_domain=entry_domain,
            targets=(self.reference, self.candidate),
            delivery_strategy=delivery_strategy,
        )

    def test_intent_fixtures_define_the_public_workflow_steps(self) -> None:
        document = json.loads(FIXTURES.read_text(encoding="utf-8"))
        self.assertEqual(
            document["schema_version"],
            "openubmc-target-runtime.orchestration-fixtures.v1",
        )
        for case in document["cases"]:
            with self.subTest(case=case["id"]):
                task_intent = self.intent(
                    case["intent"],
                    entry_domain=case["entry_domain"],
                    delivery_strategy=case.get("delivery_strategy"),
                )
                self.assertEqual(
                    [f"{step.domain}:{step.phase}" for step in task_intent.steps],
                    case["expected_steps"],
                )

    def test_intent_aliases_normalize_to_a_typed_kind(self) -> None:
        intent = self.intent("debug_only")

        self.assertIs(intent.original_intent, TaskIntentKind.DIAGNOSIS_ONLY)
        self.assertEqual(intent.to_public_dict()["original_intent"], "diagnosis-only")

    def test_diagnose_and_fix_delivery_strategy_controls_mutation_authorization(
        self,
    ) -> None:
        default_route = self.intent("diagnose-and-fix")
        source_only = self.intent(
            "diagnose-and-fix", delivery_strategy="source-only"
        )
        live_patch = self.intent(
            "diagnose-and-fix", delivery_strategy="live-patch"
        )
        build_upgrade = self.intent(
            "diagnose-and-fix", delivery_strategy="build-upgrade"
        )

        self.assertIs(
            source_only.delivery_strategy,
            DeliveryStrategy.SOURCE_ONLY,
        )
        self.assertIs(
            default_route.delivery_strategy,
            DeliveryStrategy.SOURCE_ONLY,
        )
        self.assertEqual(
            [step.key for step in default_route.steps],
            ["debug:diagnosis", "developer:edit"],
        )
        self.assertEqual(source_only.authorization.allowed_actions, frozenset())
        live_patch.authorization.require("live_patch")
        build_upgrade.authorization.require("upgrade")
        self.assertEqual(
            build_upgrade.to_public_dict()["delivery_strategy"],
            "build-upgrade",
        )

    def test_mutation_backend_result_has_one_typed_epoch_adapter(self) -> None:
        result = MutationDomainResult.from_backend(
            {
                "ok": True,
                "epoch_after": 3,
                "evidence_ids": ["patch-evidence", ""],
            }
        )

        self.assertEqual(result.epoch_after, 3)
        self.assertEqual(result.evidence_ids, ("patch-evidence",))
        self.assertEqual(result.value["ok"], True)
        with self.assertRaisesRegex(ValueError, "positive epoch_after"):
            MutationDomainResult.from_backend({"epoch_after": True})

    def test_diagnosis_only_stops_without_mutation_even_when_handler_exists(self) -> None:
        context = TaskOrchestrationContext(
            task_id="diagnosis-task", intent=self.intent("diagnosis-only")
        )
        calls: list[str] = []

        def debug(execution):
            calls.append(execution.phase)
            return DomainOutcome.succeeded(
                {"conclusion": "read-only diagnosis"},
                evidence_ids=("evidence-debug",),
            )

        def live_patch(_execution):
            calls.append("unexpected-mutation")
            return DomainOutcome.modified(
                {"applied": True},
                modified_target_epochs={"candidate": 1},
            )

        result = TaskWorkflowOrchestrator(context).run(
            {"debug": debug, "live_patch": live_patch}
        )

        self.assertTrue(result.completed)
        self.assertEqual(calls, ["diagnosis"])
        self.assertEqual(result.executions[0].status, "succeeded")
        self.assertEqual(context.intent.authorization.allowed_actions, frozenset())
        self.assertEqual(context.intent.authorization.parse_count, 1)

    def test_diagnose_and_fix_carries_typed_edit_and_fresh_epoch_context(self) -> None:
        context = TaskOrchestrationContext(
            task_id="fix-task",
            intent=self.intent(
                "diagnose-and-fix",
                delivery_strategy="live-patch",
            ),
        )
        seen_operation_ids: list[str] = []
        seen_authorizations = []
        edit = DeveloperEditIntent(
            component_roots=("/src/storage",),
            authored_files=("src/handler.lua",),
            change_summary="handle the diagnosed missing-state transition",
            runtime_artifact="/tmp/handler.lua",
            restart_scope="skynet",
            verification_checks=("GetState returns Ready",),
        )

        def debug(execution):
            seen_operation_ids.append(execution.operation_id)
            self.assertEqual(
                [target.role for target in execution.targets],
                ["reference", "candidate"],
            )
            return DomainOutcome.succeeded(
                {"owner": "storage"}, evidence_ids=("evidence-diagnosis",)
            )

        def developer(execution):
            seen_operation_ids.append(execution.operation_id)
            self.assertEqual(execution.previous[-1].evidence_ids, ("evidence-diagnosis",))
            return DomainOutcome.succeeded(
                {"changed": True}, edit_intent=edit
            )

        def live_patch(execution):
            seen_operation_ids.append(execution.operation_id)
            seen_authorizations.append(execution.authorization)
            self.assertIs(execution.edit_intent, edit)
            execution.authorization.require("live_patch")
            return DomainOutcome.modified(
                {"applied": True},
                evidence_ids=("evidence-patch",),
                modified_target_epochs={"candidate": 1},
            )

        def verify(execution):
            seen_operation_ids.append(execution.operation_id)
            self.assertEqual(execution.minimum_target_epochs, {"candidate": 1})
            self.assertEqual(
                {target.target_id for target in execution.targets},
                {"reference", "candidate"},
            )
            return DomainOutcome.verified(
                {"comparison": "candidate now matches reference"},
                evidence_ids=("evidence-fresh",),
                observed_target_epochs={"candidate": 1, "reference": 0},
            )

        result = TaskWorkflowOrchestrator(context).run(
            {
                "debug": debug,
                "developer": developer,
                "live_patch": live_patch,
                "debug:fresh_verification": verify,
            }
        )

        self.assertTrue(result.completed)
        self.assertEqual(
            [execution.status for execution in result.executions],
            ["succeeded", "succeeded", "modified", "verified"],
        )
        self.assertEqual(len(seen_operation_ids), len(set(seen_operation_ids)))
        self.assertTrue(all(value.startswith("op-") for value in seen_operation_ids))
        self.assertEqual(seen_authorizations, [context.intent.authorization])
        self.assertEqual(len(result.handoffs), 3)
        self.assertEqual(result.handoffs[-1].evidence_ids, ("evidence-patch",))
        self.assertIs(result.handoffs[1].edit_intent, edit)
        self.assertNotIn(
            "credentials",
            json.dumps(edit.to_public_dict(), sort_keys=True).lower(),
        )

    def test_domain_failure_marks_remaining_owning_skill_as_not_executed(self) -> None:
        context = TaskOrchestrationContext(
            task_id="partial-task",
            intent=self.intent(
                "diagnose-and-fix",
                delivery_strategy="live-patch",
            ),
        )
        edit = DeveloperEditIntent(
            component_roots=("/src/storage",),
            authored_files=("src/handler.lua",),
            change_summary="bounded fix",
        )

        result = TaskWorkflowOrchestrator(context).run(
            {
                "debug": lambda _execution: DomainOutcome.succeeded(
                    {"diagnosed": True}, evidence_ids=("evidence-1",)
                ),
                "developer": lambda _execution: DomainOutcome.succeeded(
                    {"edited": True}, edit_intent=edit
                ),
                "live_patch": lambda _execution: (_ for _ in ()).throw(
                    RuntimeError("target mutation was not executed")
                ),
            }
        )

        self.assertFalse(result.completed)
        self.assertTrue(result.partial)
        self.assertEqual(result.phase_states["live_patch:mutation"], "failed")
        self.assertEqual(
            result.phase_states["debug:fresh_verification"], "not_executed"
        )
        self.assertEqual(result.next_action, "live_patch:mutation")

    def test_failed_verification_retains_modified_and_not_executed_state(self) -> None:
        context = TaskOrchestrationContext(
            task_id="upgrade-task",
            intent=self.intent("upgrade-and-verify", entry_domain="upgrade"),
        )

        def upgrade(execution):
            execution.authorization.require("upgrade")
            return DomainOutcome.modified(
                {"installed_version": "2.0"},
                evidence_ids=("evidence-upgrade",),
                modified_target_epochs={"candidate": 2},
            )

        def verify(_execution):
            raise RuntimeError("fresh Debug verification failed")

        result = TaskWorkflowOrchestrator(context).run(
            {"upgrade": upgrade, "debug:fresh_verification": verify}
        )

        self.assertFalse(result.completed)
        self.assertTrue(result.partial)
        self.assertEqual(
            [execution.status for execution in result.executions],
            ["modified", "failed"],
        )
        self.assertEqual(result.executions[0].modified_target_epochs, {"candidate": 2})
        self.assertIn("fresh Debug verification failed", result.executions[1].error)
        self.assertEqual(result.next_action, "debug:fresh_verification")

    def test_openubmc_task_run_exposes_bound_orchestration_without_rebinding(self) -> None:
        intent = self.intent("diagnosis-only")
        task_run = OpenUBMCTaskRun(
            task_id="bound-task",
            credential_resolver=self.resolver(),
        )
        first = task_run.bind_task_intent(intent)
        second = task_run.bind_task_intent(intent)
        self.assertIs(first, second)
        self.assertEqual(
            task_run.runtime_status()["orchestration"]["intent"]["parse_count"], 1
        )
        with self.assertRaises(ValueError):
            task_run.bind_task_intent(self.intent("diagnose-and-fix"))

    def test_mcp_domain_backends_inherit_target_and_keep_resources_isolated(self) -> None:
        class Backend:
            def __init__(self, tool: str) -> None:
                self.tool = tool
                self.opened: list[dict[str, object]] = []

            def open_task(self, task_id: str):
                resource = {"task_id": task_id, "lease": object(), "calls": []}
                self.opened.append(resource)
                return resource

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(task) -> dict[str, object]:
                return {"call_count": len(task["calls"])}

            def debug_run(self, task, arguments, _context):
                task["calls"].append(dict(arguments))
                return {"domain": "debug", "ip": arguments["ip"]}

            def log_bundle_collect(self, task, arguments, _context):
                task["calls"].append(dict(arguments))
                return {
                    "domain": "log_analyzer",
                    "ip": arguments["ip"],
                    "ssh_port": arguments["ssh_port"],
                }

        debug = Backend("debug_run")
        logs = Backend("log_bundle_collect")
        backend = OrchestratedMcpBackend(
            {
                "debug_run": debug,
                "log_bundle_collect": logs,
            }
        )
        task = backend.open_task("mcp-orchestration")
        operation_context = object()

        first = backend.debug_run(
            task,
            {
                "intent": "diagnosis-only",
                "final_purpose": "diagnose current state and collect a bundle if needed",
                "ip": "192.0.2.11",
                "target_id": "candidate",
                "target_role": "candidate",
                "ssh_user": "Administrator",
                "ssh_password": "must-not-enter-context",
                "ssh_password_env": "OPENUBMC_SSH_PASSWORD",
            },
            operation_context,
        )
        second = backend.log_bundle_collect(
            task,
            {"problem": "same target bundle"},
            operation_context,
        )

        self.assertEqual(first["ip"], "192.0.2.11")
        self.assertEqual(second["ip"], "192.0.2.11")
        self.assertIsNot(debug.opened[0]["lease"], logs.opened[0]["lease"])
        status = backend.task_status(task)
        self.assertEqual(status["orchestration"]["intent"]["parse_count"], 1)
        self.assertNotIn("must-not-enter-context", json.dumps(status, sort_keys=True))
        self.assertEqual(
            set(status["domain_resources"]), {"debug_run", "log_bundle_collect"}
        )
        third = backend.log_bundle_collect(
            task,
            {"ip": "192.0.2.99", "problem": "different target"},
            operation_context,
        )

        self.assertEqual(third["ip"], "192.0.2.99")
        rebound_status = backend.task_status(task)
        self.assertEqual(
            rebound_status["orchestration"]["intent"]["targets"][0]["target"]["host"],
            "192.0.2.99",
        )
        self.assertEqual(
            rebound_status["orchestration"]["intent"]["final_purpose"],
            "diagnose current state and collect a bundle if needed",
        )
        self.assertEqual(
            rebound_status["orchestration"]["intent"]["targets"][0]["target_id"],
            "candidate",
        )
        self.assertEqual(len(rebound_status["orchestration_history"]), 1)

        fourth = backend.log_bundle_collect(
            task,
            {"ssh_port": 2222, "problem": "same host on a new SSH port"},
            operation_context,
        )

        self.assertEqual(fourth["ip"], "192.0.2.99")
        self.assertEqual(fourth["ssh_port"], 2222)
        updated_status = backend.task_status(task)
        self.assertEqual(
            updated_status["orchestration"]["intent"]["targets"][0]["target"][
                "ports"
            ]["ssh"],
            2222,
        )
        self.assertEqual(len(updated_status["orchestration_history"]), 2)

        backend.debug_run(
            task,
            {"ssh_password_env": "OPENUBMC_OTHER_SSH_PASSWORD"},
            operation_context,
        )
        credential_status = backend.task_status(task)
        self.assertEqual(credential_status["credential_parse_count"], 2)

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_omitted_target_incrementally_inherits_connection_scope(
        self,
        _load_selected_credentials_file,
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"debug_run": domain})
        task = backend.open_task("incremental-target-binding")
        operation_context = object()
        backend.debug_run(
            task,
            {
                "intent": "diagnosis-only",
                "final_purpose": "continue diagnosing one target",
                "ip": "192.0.2.11",
                "target_id": "candidate",
                "target_role": "candidate",
                "ssh_port": 2201,
                "telnet_port": 2301,
                "redfish_port": 8443,
                "ssh_user_env": "OLD_SSH_USER",
                "ssh_password_env": "OLD_SSH_PASSWORD",
                "ssh_identity_file": "/tmp/old-identity",
                "telnet_user_env": "OLD_TELNET_USER",
                "telnet_password_env": "OLD_TELNET_PASSWORD",
                "redfish_user_env": "OLD_REDFISH_USER",
                "redfish_password_env": "OLD_REDFISH_PASSWORD",
                "ssh_host_key_policy": "accept-new",
                "ssh_known_hosts_file": "/tmp/old-known-hosts",
                "allow_insecure_host_key": True,
                "allow_insecure_tls": True,
            },
            operation_context,
        )
        backend.debug_run(
            task,
            {"ssh_port": 2222, "problem": "same target, adjusted SSH port"},
            operation_context,
        )

        call = domain.calls[-1][1]
        self.assertEqual(call["ip"], "192.0.2.11")
        self.assertEqual(call["ssh_port"], 2222)
        self.assertEqual(call["telnet_port"], 2301)
        self.assertEqual(call["ssh_user_env"], "OLD_SSH_USER")
        self.assertEqual(call["ssh_password_env"], "OLD_SSH_PASSWORD")
        self.assertEqual(call["ssh_identity_file"], "/tmp/old-identity")
        self.assertEqual(call["telnet_user_env"], "OLD_TELNET_USER")
        self.assertEqual(call["telnet_password_env"], "OLD_TELNET_PASSWORD")
        self.assertNotIn("redfish_port", call)
        self.assertNotIn("redfish_user_env", call)
        self.assertNotIn("redfish_password_env", call)
        status = backend.task_status(task)
        target = status["orchestration"]["intent"]["targets"][0]["target"]
        self.assertEqual(
            target["ports"], {"ssh": 2222, "telnet": 2301, "redfish": 8443}
        )
        self.assertEqual(target["policy"]["ssh_host_key_policy"], "accept-new")

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_direct_credentials_follow_same_process_continuation(
        self,
        _load_selected_credentials_file,
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"debug_run": domain})
        task = backend.open_task("direct-credential-continuation")
        backend.debug_run(
            task,
            {
                "ip": "192.0.2.12",
                "ssh_user": "root",
                "ssh_password": "direct-ssh-password",
                "telnet_user": "root",
                "telnet_password": "direct-telnet-password",
            },
            object(),
        )
        backend.debug_run(task, {"problem": "continue"}, object())

        continued = domain.calls[-1][1]
        self.assertEqual(continued["ssh_password"], "direct-ssh-password")
        self.assertEqual(continued["telnet_password"], "direct-telnet-password")
        self.assertNotIn(
            "direct-ssh-password",
            json.dumps(backend.task_status(task), sort_keys=True),
        )

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_explicit_target_rebind_resets_ports_and_reuses_credentials(
        self,
        _load_selected_credentials_file,
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"debug_run": domain})
        task = backend.open_task("fresh-target-binding")
        operation_context = object()
        backend.debug_run(
            task,
            {
                "intent": "diagnosis-only",
                "final_purpose": "switch targets without repeating the purpose",
                "ip": "192.0.2.11",
                "target_id": "candidate",
                "target_role": "candidate",
                "ssh_port": 2201,
                "telnet_port": 2301,
                "redfish_port": 8443,
                "ssh_user_env": "OLD_SSH_USER",
                "ssh_password_env": "OLD_SSH_PASSWORD",
                "ssh_identity_file": "/tmp/old-identity",
                "telnet_user_env": "OLD_TELNET_USER",
                "telnet_password_env": "OLD_TELNET_PASSWORD",
                "redfish_user_env": "OLD_REDFISH_USER",
                "redfish_password_env": "OLD_REDFISH_PASSWORD",
                "ssh_host_key_policy": "accept-new",
                "ssh_known_hosts_file": "/tmp/old-known-hosts",
                "allow_insecure_host_key": True,
                "allow_insecure_tls": True,
            },
            operation_context,
        )
        backend.debug_run(
            task,
            {"ip": "192.0.2.99", "problem": "explicitly switch target"},
            operation_context,
        )

        call = domain.calls[-1][1]
        self.assertEqual(call["ip"], "192.0.2.99")
        self.assertEqual(call["ssh_port"], 22)
        self.assertEqual(call["telnet_port"], 23)
        self.assertEqual(call["ssh_user_env"], "OLD_SSH_USER")
        self.assertEqual(call["ssh_password_env"], "OLD_SSH_PASSWORD")
        self.assertEqual(call["ssh_identity_file"], "/tmp/old-identity")
        self.assertEqual(call["telnet_user_env"], "OLD_TELNET_USER")
        self.assertEqual(call["telnet_password_env"], "OLD_TELNET_PASSWORD")
        self.assertEqual(call["ssh_host_key_policy"], "accept-new")
        self.assertEqual(call["ssh_known_hosts_file"], "/tmp/old-known-hosts")
        self.assertTrue(call["allow_insecure_host_key"])
        unrelated_keys = {
            "ssh_user",
            "telnet_user",
            "redfish_port",
            "redfish_user",
            "redfish_user_env",
            "redfish_password_env",
            "allow_insecure_tls",
        }
        for key in unrelated_keys:
            self.assertNotIn(key, call)
        status = backend.task_status(task)
        intent = status["orchestration"]["intent"]
        target = intent["targets"][0]
        self.assertEqual(intent["final_purpose"], "switch targets without repeating the purpose")
        self.assertEqual(target["target_id"], "candidate")
        self.assertEqual(target["role"], "candidate")
        self.assertEqual(
            target["target"]["ports"],
            {"ssh": 22, "telnet": 23, "redfish": 443},
        )
        self.assertEqual(
            target["target"]["policy"]["ssh_host_key_policy"], "accept-new"
        )
        self.assertEqual(len(status["orchestration_history"]), 1)

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_explicit_target_set_reuses_common_credentials_and_resets_ports(
        self,
        _load_selected_credentials_file,
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"debug_run": domain})
        task = backend.open_task("fresh-target-set-binding")
        operation_context = object()
        backend.debug_run(
            task,
            {
                "intent": "diagnosis-only",
                "ip": "192.0.2.11",
                "ssh_port": 2201,
                "telnet_port": 2301,
                "redfish_port": 8443,
                "ssh_user_env": "OLD_SSH_USER",
                "telnet_user_env": "OLD_TELNET_USER",
                "redfish_user_env": "OLD_REDFISH_USER",
            },
            operation_context,
        )
        backend.debug_run(
            task,
            {
                "redfish_password": "isolated-test-secret",
                "password": "isolated-test-secret",
                "targets": [
                    {
                        "ip": "192.0.2.10",
                        "target_id": "reference",
                        "role": "reference",
                        "redfish_password": "isolated-test-secret",
                    },
                    {
                        "ip": "192.0.2.11",
                        "target_id": "candidate",
                        "role": "candidate",
                        "redfish_password": "isolated-test-secret",
                    },
                ]
            },
            operation_context,
        )

        projected = domain.calls[-1][1]
        targets = projected["targets"]
        self.assertNotIn("redfish_password", projected)
        self.assertNotIn("password", projected)
        self.assertEqual(len(targets), 2)
        for target in targets:
            self.assertEqual(target["ssh_port"], 22)
            self.assertEqual(target["telnet_port"], 23)
            self.assertEqual(target["ssh_user_env"], "OLD_SSH_USER")
            self.assertEqual(target["telnet_user_env"], "OLD_TELNET_USER")
            for key in (
                "redfish_port",
                "redfish_user_env",
                "redfish_password",
            ):
                self.assertNotIn(key, target)
        status_targets = backend.task_status(task)["orchestration"]["intent"][
            "targets"
        ]
        self.assertEqual(
            [target["target"]["ports"] for target in status_targets],
            [
                {"ssh": 22, "telnet": 23, "redfish": 443},
                {"ssh": 22, "telnet": 23, "redfish": 443},
            ],
        )

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_projects_connection_arguments_by_domain(
        self,
        _load_selected_credentials_file,
    ) -> None:
        connection_arguments = {
            "ip": "192.0.2.11",
            "ssh_port": 2201,
            "telnet_port": 2301,
            "redfish_port": 8443,
            "ssh_user": "ssh-user",
            "ssh_user_env": "SSH_USER_ENV",
            "ssh_password_env": "SSH_PASSWORD_ENV",
            "ssh_identity_file": "/tmp/identity",
            "ssh_password": "isolated-ssh-secret",
            "telnet_user": "telnet-user",
            "telnet_user_env": "TELNET_USER_ENV",
            "telnet_password_env": "TELNET_PASSWORD_ENV",
            "telnet_password": "isolated-telnet-secret",
            "redfish_user": "redfish-user",
            "redfish_user_env": "REDFISH_USER_ENV",
            "redfish_password_env": "REDFISH_PASSWORD_ENV",
            "redfish_password": "isolated-redfish-secret",
            "ssh_host_key_policy": "accept-new",
            "ssh_known_hosts_file": "/tmp/known-hosts",
            "allow_insecure_host_key": True,
            "allow_insecure_tls": True,
            "password": "isolated-generic-secret",
        }
        ssh = {
            "ssh_port",
            "ssh_user",
            "ssh_user_env",
            "ssh_password_env",
            "ssh_identity_file",
        }
        telnet = {
            "telnet_port",
            "telnet_user",
            "telnet_user_env",
            "telnet_password_env",
        }
        redfish = {
            "redfish_port",
            "redfish_user",
            "redfish_user_env",
            "redfish_password_env",
        }
        expected = {
            "debug_run": (
                {
                    "ip",
                    "ssh_password",
                    "telnet_password",
                    "ssh_host_key_policy",
                    "ssh_known_hosts_file",
                    "allow_insecure_host_key",
                }
                | ssh
                | telnet
            ),
            "debug_collect": (
                {
                    "ip",
                    "ssh_password",
                    "telnet_password",
                    "ssh_host_key_policy",
                    "ssh_known_hosts_file",
                    "allow_insecure_host_key",
                }
                | ssh
                | telnet
            ),
            "log_bundle_collect": (
                {"ip", "ssh_password", "redfish_password"} | ssh | redfish
            ),
            "live_patch_run": (
                {
                    "ip",
                    "ssh_password",
                    "telnet_password",
                    "ssh_host_key_policy",
                    "ssh_known_hosts_file",
                }
                | ssh
                | telnet
            ),
            "upgrade_run": {
                "ip",
                "redfish_password",
                "allow_insecure_tls",
            }
            | redfish,
        }
        connection_keys = set(connection_arguments)
        tool_intents = {
            "live_patch_run": "live-patch",
            "upgrade_run": "upgrade-and-verify",
        }

        for index, (tool_name, expected_keys) in enumerate(expected.items()):
            with self.subTest(tool=tool_name):
                domain = RecordingMcpBackend()
                backend = OrchestratedMcpBackend({tool_name: domain})
                task = backend.open_task(f"domain-projection-{index}")
                getattr(backend, tool_name)(
                    task,
                    {
                        "intent": tool_intents.get(tool_name, "diagnosis-only"),
                        **connection_arguments,
                    },
                    object(),
                )
                projected = domain.calls[-1][1]
                self.assertEqual(set(projected) & connection_keys, expected_keys)

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_debug_target_id_selects_one_bound_target(
        self, _load_selected_credentials_file
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"debug_run": domain})
        task = backend.open_task("selected-debug-target")

        backend.debug_run(
            task,
            {
                "intent": "diagnosis-only",
                "target_id": "candidate",
                "targets": [
                    {
                        "ip": "192.0.2.10",
                        "target_id": "reference",
                        "role": "reference",
                    },
                    {
                        "ip": "192.0.2.11",
                        "target_id": "candidate",
                        "role": "candidate",
                    },
                ],
            },
            object(),
        )

        projected = domain.calls[-1][1]
        self.assertEqual(projected["ip"], "192.0.2.11")
        self.assertNotIn("targets", projected)

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_debug_non_string_target_id_keeps_comparison_semantics(
        self, _load_selected_credentials_file
    ) -> None:
        for index, target_id in enumerate((None, 7)):
            with self.subTest(target_id=target_id):
                domain = RecordingMcpBackend()
                backend = OrchestratedMcpBackend({"debug_run": domain})
                task = backend.open_task(f"non-string-debug-target-{index}")

                backend.debug_run(
                    task,
                    {
                        "intent": "diagnosis-only",
                        "target_id": target_id,
                        "targets": [
                            {
                                "ip": "192.0.2.10",
                                "target_id": "reference",
                                "role": "reference",
                            },
                            {
                                "ip": "192.0.2.11",
                                "target_id": "candidate",
                                "role": "candidate",
                            },
                        ],
                    },
                    object(),
                )

                projected = domain.calls[-1][1]
                self.assertNotIn("ip", projected)
                self.assertEqual(
                    [target["ip"] for target in projected["targets"]],
                    ["192.0.2.10", "192.0.2.11"],
                )

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_mutation_rejects_ambiguous_candidate_targets(
        self, _load_selected_credentials_file
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"upgrade_run": domain})
        task = backend.open_task("ambiguous-mutation-target")

        with self.assertRaises(ValueError) as raised:
            backend.upgrade_run(
                task,
                {
                    "targets": [
                        {
                            "ip": "192.0.2.10",
                            "target_id": "reference",
                            "role": "reference",
                        },
                        {
                            "ip": "192.0.2.12",
                            "target_id": "candidate-b",
                            "role": "candidate",
                        },
                        {
                            "ip": "192.0.2.11",
                            "target_id": "candidate-a",
                            "role": "candidate",
                        },
                    ],
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "2.0",
                },
                object(),
            )

        self.assertEqual(
            str(raised.exception),
            "mutation target is ambiguous; specify target_id from candidate "
            "targets: candidate-a, candidate-b",
        )
        self.assertEqual(domain.calls, [])

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_mutation_implicitly_uses_the_only_candidate_target(
        self, _load_selected_credentials_file
    ) -> None:
        domain = RecordingMcpBackend()
        backend = OrchestratedMcpBackend({"upgrade_run": domain})
        task = backend.open_task("single-mutation-candidate")

        backend.upgrade_run(
            task,
            {
                "targets": [
                    {
                        "ip": "192.0.2.10",
                        "target_id": "reference",
                        "role": "reference",
                    },
                    {
                        "ip": "192.0.2.11",
                        "target_id": "candidate",
                        "role": "candidate",
                    },
                ],
                "artifact_path": "/tmp/openubmc.hpm",
                "artifact_sha256": "a" * 64,
                "product_version": "2.0",
            },
            object(),
        )

        projected = domain.calls[-1][1]
        self.assertEqual(projected["ip"], "192.0.2.11")
        self.assertNotIn("targets", projected)

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_mutation_handoff_verifies_the_explicit_target(
        self, _load_selected_credentials_file
    ) -> None:
        class Backend(RecordingMcpBackend):
            def upgrade_run(self, _task, arguments, _context):
                self._capture("upgrade_run", arguments)
                return {"ok": True, "epoch_after": 2}

            def debug_run(self, _task, arguments, _context):
                self._capture("debug_run", arguments)
                return {"ok": True, "target_epoch": 2}

        domain = Backend()
        backend = OrchestratedMcpBackend(
            {"upgrade_run": domain, "debug_run": domain}
        )
        task = backend.open_task("selected-mutation-handoff")

        result = backend.upgrade_run(
            task,
            {
                "target_id": "candidate-b",
                "targets": [
                    {
                        "ip": "192.0.2.10",
                        "target_id": "reference",
                        "role": "reference",
                    },
                    {
                        "ip": "192.0.2.11",
                        "target_id": "candidate-a",
                        "role": "candidate",
                    },
                    {
                        "ip": "192.0.2.12",
                        "target_id": "candidate-b",
                        "role": "candidate",
                    },
                ],
                "artifact_path": "/tmp/openubmc.hpm",
                "artifact_sha256": "a" * 64,
                "product_version": "2.0",
                "workflow": {"verification": {"profile": "freshness"}},
            },
            self.operation_context("selected-mutation-handoff"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(
            [arguments["ip"] for _name, arguments in domain.calls],
            ["192.0.2.12", "192.0.2.12"],
        )
        self.assertTrue(
            all("targets" not in arguments for _name, arguments in domain.calls)
        )

    @mock.patch(
        "openubmc_target_runtime.mcp.load_selected_credentials_file",
        return_value={},
    )
    def test_mcp_fresh_verification_cannot_rebind_the_mutation_target(
        self, _load_selected_credentials_file
    ) -> None:
        class Backend(RecordingMcpBackend):
            def upgrade_run(self, _task, arguments, _context):
                self._capture("upgrade_run", arguments)
                return {"ok": True, "epoch_after": 2}

            def debug_run(self, _task, arguments, _context):
                self._capture("debug_run", arguments)
                return {"ok": True, "target_epoch": 2}

        domain = Backend()
        backend = OrchestratedMcpBackend(
            {"upgrade_run": domain, "debug_run": domain}
        )
        task = backend.open_task("verification-target-rebinding")

        result = backend.upgrade_run(
            task,
            {
                "target_id": "candidate-b",
                "targets": [
                    {
                        "ip": "192.0.2.10",
                        "target_id": "reference",
                        "role": "reference",
                    },
                    {
                        "ip": "192.0.2.11",
                        "target_id": "candidate-a",
                        "role": "candidate",
                    },
                    {
                        "ip": "192.0.2.12",
                        "target_id": "candidate-b",
                        "role": "candidate",
                    },
                ],
                "artifact_path": "/tmp/openubmc.hpm",
                "artifact_sha256": "a" * 64,
                "product_version": "2.0",
                "workflow": {
                    "verification": {
                        "profile": "freshness",
                        "ip": "203.0.113.99",
                        "target_id": "reference",
                        "target_role": "reference",
                        "role": "reference",
                        "targets": [
                            {
                                "ip": "203.0.113.98",
                                "target_id": "reference",
                                "role": "reference",
                            },
                            {
                                "ip": "203.0.113.99",
                                "target_id": "candidate-b",
                                "role": "candidate",
                            },
                        ],
                    }
                },
            },
            self.operation_context("verification-target-rebinding"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(
            [arguments["ip"] for _name, arguments in domain.calls],
            ["192.0.2.12", "192.0.2.12"],
        )
        self.assertTrue(
            all("targets" not in arguments for _name, arguments in domain.calls)
        )

    def test_mcp_selected_target_uses_its_bound_connection_arguments(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            def debug_run(self, _task, arguments, _context):
                self.calls.append(("debug", dict(arguments)))
                return {"ok": True}

            def upgrade_run(self, _task, arguments, _context):
                self.calls.append(("upgrade", dict(arguments)))
                return {"ok": True}

        domain = Backend()
        backend = OrchestratedMcpBackend(
            {"debug_run": domain, "upgrade_run": domain}
        )
        task = backend.open_task("multi-target-rebind")
        operation_context = object()

        backend.debug_run(
            task,
            {
                "intent": "diagnose-and-fix",
                "ip": "192.0.2.1",
                "target_id": "candidate",
            },
            operation_context,
        )
        backend.debug_run(
            task,
            {
                "targets": [
                    {
                        "ip": "192.0.2.10",
                        "target_id": "reference",
                        "role": "reference",
                        "ssh_port": 2201,
                        "redfish_port": 8442,
                        "redfish_user_env": "REFERENCE_REDFISH_USER",
                    },
                    {
                        "ip": "192.0.2.11",
                        "target_id": "candidate",
                        "role": "candidate",
                        "ssh_port": 2202,
                        "redfish_port": 8443,
                        "redfish_user_env": "CANDIDATE_REDFISH_USER",
                    },
                ]
            },
            operation_context,
        )
        backend.upgrade_run(
            task,
            {"target_id": "candidate", "artifact_path": "/tmp/openubmc.hpm"},
            operation_context,
        )

        upgrade_arguments = domain.calls[-1][1]
        self.assertEqual(upgrade_arguments["ip"], "192.0.2.11")
        self.assertNotIn("ssh_port", upgrade_arguments)
        self.assertEqual(upgrade_arguments["redfish_port"], 8443)
        self.assertEqual(
            upgrade_arguments["redfish_user_env"],
            "CANDIDATE_REDFISH_USER",
        )
        self.assertEqual(
            upgrade_arguments["_task_delivery_strategy"],
            "build-upgrade",
        )

    def test_mcp_lists_mutation_domains_only_when_typed_backends_exist(self) -> None:
        class Backend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def live_patch_run(_task, _arguments, _context):
                return {"ok": True}

            @staticmethod
            def upgrade_run(_task, _arguments, _context):
                return {"ok": True}

        domain = Backend()
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {"live_patch_run": domain, "upgrade_run": domain}
            )
        )
        try:
            definitions = service.tool_definitions()
            self.assertEqual(
                [tool["name"] for tool in definitions],
                [
                    "live_patch_run",
                    "upgrade_run",
                    "case_read",
                    "evidence_read",
                    "case_close",
                    "case_forget",
                    "phase_record",
                    "workflow.advance",
                    "runtime_status",
                ],
            )
            live_patch = definitions[0]["inputSchema"]
            self.assertEqual(
                live_patch["properties"]["action"]["enum"],
                ["apply", "rollback"],
            )
            self.assertIn("backup_path", live_patch["properties"])
            self.assertIn("remove_created", live_patch["properties"])
            self.assertIn(
                "expected_current_sha256",
                live_patch["properties"],
            )
            self.assertEqual(
                live_patch["properties"]["delivery_strategy"]["enum"],
                ["source-only", "live-patch", "build-upgrade"],
            )
            self.assertEqual(
                live_patch["required"],
                ["remote_path"],
            )
            self.assertEqual(
                live_patch["allOf"],
                [
                    {
                        "if": {
                            "properties": {"action": {"const": "rollback"}},
                            "required": ["action"],
                        },
                        "then": {
                            "oneOf": [
                                {"required": ["backup_path"]},
                                {
                                    "properties": {
                                        "remove_created": {"const": True}
                                    },
                                    "required": [
                                        "remove_created",
                                        "expected_current_sha256",
                                    ],
                                },
                            ]
                        },
                        "else": {"required": ["local_path"]},
                    }
                ],
            )
            upgrade = definitions[1]["inputSchema"]
            self.assertEqual(
                upgrade["properties"]["upload_timeout"]["default"],
                600,
            )
        finally:
            service.close()

    def test_mcp_passes_the_once_bound_intent_to_mutation_backends(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.arguments: list[dict[str, object]] = []

            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            def live_patch_run(self, _task, arguments, _context):
                self.arguments.append(dict(arguments))
                return {"ok": True}

        domain = Backend()
        backend = OrchestratedMcpBackend({"live_patch_run": domain})
        task = backend.open_task("mutation-intent")

        backend.live_patch_run(
            task,
            {
                "intent": "live-patch",
                "final_purpose": "repair the authored runtime file",
                "ip": "192.0.2.11",
                "local_path": "/tmp/unit.lua",
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
            },
            object(),
        )

        self.assertEqual(domain.arguments[0]["_task_intent"], "live-patch")
        self.assertEqual(domain.arguments[0]["ip"], "192.0.2.11")
        self.assertNotIn("intent", domain.arguments[0])

    def test_later_direct_mutation_selects_route_and_uses_stable_operation_id(
        self,
    ) -> None:
        class Backend:
            def __init__(self) -> None:
                self.operation_ids: list[str] = []
                self.mutation_arguments: list[dict[str, object]] = []

            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                return {"ok": True}

            def live_patch_run(self, _task, arguments, context):
                self.operation_ids.append(context.operation_id)
                self.mutation_arguments.append(dict(arguments))
                return {"ok": True}

        domain = Backend()
        backend = OrchestratedMcpBackend(
            {"debug_run": domain, "live_patch_run": domain}
        )
        task = backend.open_task("later-live-patch")
        backend.debug_run(
            task,
            {
                "intent": "diagnose-and-fix",
                "final_purpose": "repair and verify the runtime behavior",
                "ip": "192.0.2.11",
            },
            self.operation_context("later-live-patch"),
        )
        mutation = {
            "local_path": "/tmp/unit.lua",
            "remote_path": "/opt/bmc/apps/demo/unit.lua",
            "restart_scope": "none",
        }
        for _ in range(2):
            backend.live_patch_run(
                task,
                mutation,
                self.operation_context("later-live-patch"),
            )

        self.assertEqual(len(set(domain.operation_ids)), 1)
        self.assertTrue(domain.operation_ids[0].startswith("op-mutation-"))
        self.assertTrue(
            all(
                arguments["_task_delivery_strategy"] == "live-patch"
                for arguments in domain.mutation_arguments
            )
        )
        status = backend.task_status(task)
        self.assertEqual(
            status["orchestration"]["intent"]["delivery_strategy"],
            "live-patch",
        )
        self.assertEqual(
            status["orchestration"]["intent"]["final_purpose"],
            "repair and verify the runtime behavior",
        )

    def operation_context(self, task_id: str = "workflow-task") -> OperationContext:
        return OperationContext(
            task_id=task_id,
            operation_id="outer-operation",
            deadline_at=time.monotonic() + 30,
            cancellation=CancellationToken(),
            _clock=time.monotonic,
        )

    def test_upgrade_entry_automatically_runs_fresh_debug_without_user_ids(self) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []
        debug_runs = 0

        class UpgradeBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def upgrade_run(_task, arguments, context):
                calls.append(("upgrade", context.operation_id, dict(arguments)))
                return {"ok": True, "epoch_after": 2}

        class DebugBackend(UpgradeBackend):
            @staticmethod
            def debug_run(_task, arguments, context):
                nonlocal debug_runs
                debug_runs += 1
                calls.append(("debug", context.operation_id, dict(arguments)))
                return {
                    "ok": True,
                    "result": {
                        "runtime": {
                            "status": {
                                "evidence_ledger": {
                                    "records": [
                                        {
                                            "evidence_id": (
                                                f"fresh-debug-{debug_runs}"
                                            )
                                        }
                                    ]
                                },
                                "targets": [
                                    {
                                        "target": {"host": "192.0.2.11"},
                                        "epochs": {"target_epoch": 2},
                                    }
                                ],
                            }
                        }
                    },
                }

        backend = OrchestratedMcpBackend(
            {
                "upgrade_run": UpgradeBackend(),
                "debug_run": DebugBackend(),
            }
        )
        task = backend.open_task("upgrade-workflow")
        arguments = {
            "intent": "upgrade-and-verify",
            "ip": "192.0.2.11",
            "target_id": "candidate",
            "target_role": "candidate",
            "artifact_path": "/tmp/openubmc.hpm",
            "artifact_sha256": "a" * 64,
            "product_version": "2.0",
            "workflow": {"verification": {"keyword": "firmware ready"}},
        }

        first = backend.upgrade_run(
            task, arguments, self.operation_context("upgrade-workflow")
        )
        second = backend.upgrade_run(
            task, arguments, self.operation_context("upgrade-workflow")
        )

        self.assertTrue(first["completed"])
        self.assertTrue(second["completed"])
        self.assertNotEqual(first, second)
        self.assertEqual(
            [execution["status"] for execution in first["executions"]],
            ["modified", "verified"],
        )
        self.assertEqual(
            [name for name, _operation, _arguments in calls],
            ["upgrade", "debug", "debug"],
        )
        self.assertEqual(
            first["executions"][1]["evidence_ids"], ["fresh-debug-1"]
        )
        self.assertEqual(
            second["executions"][1]["evidence_ids"], ["fresh-debug-2"]
        )
        self.assertTrue(
            all(call[2]["_minimum_target_epoch"] == 2 for call in calls[1:])
        )
        operation_ids = [operation for _name, operation, _arguments in calls]
        self.assertTrue(all(value.startswith("op-") for value in operation_ids))
        self.assertTrue(operation_ids[0].startswith("op-mutation-"))

    def test_repeated_debug_workflow_reruns_diagnosis_and_verification_without_reapplying_mutation(
        self,
    ) -> None:
        debug_evidence: list[str] = []
        mutation_operation_ids: list[str] = []

        class DomainBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                evidence_id = f"debug-evidence-{len(debug_evidence) + 1}"
                debug_evidence.append(evidence_id)
                return {
                    "ok": True,
                    "target_epoch": 2,
                    "evidence_ids": [evidence_id],
                }

            @staticmethod
            def live_patch_run(_task, _arguments, context):
                mutation_operation_ids.append(context.operation_id)
                return {
                    "ok": True,
                    "epoch_after": 2,
                    "evidence_ids": ["mutation-evidence"],
                }

        domain = DomainBackend()
        backend = OrchestratedMcpBackend(
            {"debug_run": domain, "live_patch_run": domain}
        )
        task = backend.open_task("fresh-repeated-debug-workflow")
        arguments = {
            "intent": "diagnose-and-fix",
            "ip": "192.0.2.11",
            "target_id": "candidate",
            "target_role": "candidate",
            "workflow": {
                "developer": {
                    "component_roots": ["/src/storage"],
                    "authored_files": ["src/handler.lua"],
                    "change_summary": "repair the diagnosed transition",
                    "runtime_artifact": "/tmp/handler.lua",
                    "restart_scope": "skynet",
                },
                "live_patch": {
                    "local_path": "/tmp/handler.lua",
                    "remote_path": "/opt/bmc/apps/storage/handler.lua",
                    "restart_scope": "skynet",
                },
                "verification": {"keyword": "GetState"},
            },
        }

        first = backend.debug_run(
            task,
            arguments,
            self.operation_context("fresh-repeated-debug-workflow"),
        )
        second = backend.debug_run(
            task,
            arguments,
            self.operation_context("fresh-repeated-debug-workflow"),
        )

        self.assertEqual(
            debug_evidence,
            [
                "debug-evidence-1",
                "debug-evidence-2",
                "debug-evidence-3",
                "debug-evidence-4",
            ],
        )
        self.assertEqual(len(mutation_operation_ids), 1)
        first_evidence = [
            execution["evidence_ids"]
            for execution in first["executions"]
            if execution["domain"] == "debug"
        ]
        second_evidence = [
            execution["evidence_ids"]
            for execution in second["executions"]
            if execution["domain"] == "debug"
        ]
        self.assertEqual(
            first_evidence,
            [["debug-evidence-1"], ["debug-evidence-2"]],
        )
        self.assertEqual(
            second_evidence,
            [["debug-evidence-3"], ["debug-evidence-4"]],
        )
        first_mutation = next(
            execution
            for execution in first["executions"]
            if execution["phase"] == "mutation"
        )
        second_mutation = next(
            execution
            for execution in second["executions"]
            if execution["phase"] == "mutation"
        )
        self.assertEqual(
            first_mutation["operation_id"],
            second_mutation["operation_id"],
        )
        status = backend.task_status(task)
        self.assertEqual(
            status["workflow_cache"],
            {
                "enabled": False,
                "entry_count": 0,
                "bytes": 0,
                "max_entries": 0,
                "max_result_bytes": 0,
                "max_bytes": 0,
            },
        )
        self.assertEqual(
            status["workflow_history"],
            {"entry_count": 2, "max_entries": 16},
        )
        self.assertEqual(len(status["automatic_workflows"]), 2)
        self.assertEqual(status["cached_mutation_count"], 1)
        self.assertNotIn("debug-evidence", json.dumps(status, sort_keys=True))

    def test_diagnose_and_fix_entry_runs_typed_handoffs_and_parses_credentials_once(self) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []

        class DomainBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, arguments, context):
                calls.append(("debug", context.operation_id, dict(arguments)))
                return {
                    "ok": True,
                    "target_epoch": 3,
                    "evidence_ids": ["debug-evidence"],
                }

            @staticmethod
            def live_patch_run(_task, arguments, context):
                calls.append(("live_patch", context.operation_id, dict(arguments)))
                return {
                    "ok": True,
                    "epoch_after": 3,
                    "evidence_ids": ["patch-evidence"],
                }

        domain = DomainBackend()
        backend = OrchestratedMcpBackend(
            {"debug_run": domain, "live_patch_run": domain}
        )
        task = backend.open_task("diagnose-fix-workflow")
        arguments = {
            "intent": "diagnose-and-fix",
            "ip": "192.0.2.11",
            "target_id": "candidate",
            "target_role": "candidate",
            "workflow": {
                "developer": {
                    "component_roots": ["/src/storage"],
                    "authored_files": ["src/handler.lua"],
                    "change_summary": "repair the diagnosed transition",
                    "runtime_artifact": "/tmp/handler.lua",
                    "restart_scope": "skynet",
                    "verification_checks": ["GetState returns Ready"],
                },
                "live_patch": {
                    "local_path": "/tmp/handler.lua",
                    "remote_path": "/opt/bmc/apps/storage/handler.lua",
                    "restart_scope": "skynet",
                },
                "verification": {"keyword": "GetState"},
            },
        }
        credentials = {
            "OPENUBMC_SSH_USER": "Administrator",
            "OPENUBMC_SSH_PASSWORD": "one-task-secret",
        }

        with mock.patch(
            "openubmc_target_runtime.mcp.load_selected_credentials_file",
            return_value=credentials,
        ) as load_credentials:
            result = backend.debug_run(
                task,
                arguments,
                self.operation_context("diagnose-fix-workflow"),
            )

        self.assertTrue(result["completed"])
        self.assertEqual(
            [execution["status"] for execution in result["executions"]],
            ["succeeded", "succeeded", "modified", "verified"],
        )
        self.assertEqual(
            [name for name, _operation, _arguments in calls],
            ["debug", "live_patch", "debug"],
        )
        self.assertEqual(load_credentials.call_count, 1)
        self.assertTrue(
            all(call_arguments["_credential_values"] == credentials for *_rest, call_arguments in calls)
        )
        live_patch_arguments = next(
            call_arguments
            for name, _operation, call_arguments in calls
            if name == "live_patch"
        )
        self.assertEqual(live_patch_arguments["_task_intent"], "diagnose-and-fix")
        verification_arguments = calls[-1][2]
        self.assertEqual(verification_arguments["_minimum_target_epoch"], 3)
        self.assertNotIn("operation_id", json.dumps(arguments, sort_keys=True))
        status = backend.task_status(task)
        self.assertEqual(status["credential_parse_count"], 1)
        self.assertNotIn("one-task-secret", json.dumps(status, sort_keys=True))

    def test_source_only_diagnose_and_fix_stops_after_developer_handoff(self) -> None:
        calls: list[str] = []

        class DebugBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                calls.append("debug")
                return {"ok": True, "evidence_ids": ["diagnosis"]}

        backend = OrchestratedMcpBackend({"debug_run": DebugBackend()})
        task = backend.open_task("source-only-workflow")
        result = backend.debug_run(
            task,
            {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "ip": "192.0.2.11",
                "workflow": {
                    "developer": {
                        "component_roots": ["/src/storage"],
                        "authored_files": ["src/handler.lua"],
                        "change_summary": "repair the diagnosed transition",
                    }
                },
            },
            self.operation_context("source-only-workflow"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(calls, ["debug"])
        self.assertEqual(
            [execution["key"] for execution in result["executions"]],
            ["debug:diagnosis", "developer:edit"],
        )

    def test_workflow_history_distinguishes_the_current_bound_target(self) -> None:
        calls: list[str] = []

        class DebugBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, arguments, _context):
                calls.append(str(arguments["ip"]))
                return {"ok": True}

        backend = OrchestratedMcpBackend({"debug_run": DebugBackend()})
        task = backend.open_task("target-aware-workflow-cache")
        context = self.operation_context("target-aware-workflow-cache")
        backend.debug_run(
            task,
            {"intent": "diagnose-and-fix", "ip": "192.0.2.10"},
            context,
        )
        workflow = {
            "intent": "diagnose-and-fix",
            "workflow": {
                "developer": {
                    "component_roots": ["/src/storage"],
                    "authored_files": ["src/handler.lua"],
                    "change_summary": "repair the diagnosed transition",
                }
            },
        }
        backend.debug_run(task, workflow, context)
        backend.debug_run(task, {"ip": "192.0.2.11"}, context)
        backend.debug_run(task, workflow, context)

        self.assertEqual(
            calls,
            ["192.0.2.10", "192.0.2.10", "192.0.2.11", "192.0.2.11"],
        )
        status = backend.task_status(task)
        self.assertEqual(status["workflow_history"]["entry_count"], 2)
        self.assertEqual(len(status["automatic_workflows"]), 2)
        self.assertNotEqual(
            status["automatic_workflows"][0]["request_fingerprint"],
            status["automatic_workflows"][1]["request_fingerprint"],
        )

    def test_workflow_history_keeps_only_summary_for_oversized_results(self) -> None:
        calls = 0

        class DebugBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                nonlocal calls
                calls += 1
                return {"ok": True, "payload": "x" * (4 * 1024 * 1024)}

        backend = OrchestratedMcpBackend({"debug_run": DebugBackend()})
        task = backend.open_task("oversized-workflow-result")
        arguments = {
            "intent": "diagnose-and-fix",
            "ip": "192.0.2.11",
            "workflow": {
                "developer": {
                    "component_roots": ["/src/storage"],
                    "authored_files": ["src/handler.lua"],
                    "change_summary": "repair the diagnosed transition",
                }
            },
        }
        for _ in range(2):
            result = backend.debug_run(
                task,
                arguments,
                self.operation_context("oversized-workflow-result"),
            )
            self.assertTrue(result["completed"])

        self.assertEqual(calls, 2)
        status = backend.task_status(task)
        self.assertEqual(status["workflow_history"]["entry_count"], 2)
        self.assertFalse(status["workflow_cache"]["enabled"])
        self.assertEqual(status["workflow_cache"]["entry_count"], 0)
        self.assertEqual(status["workflow_cache"]["bytes"], 0)
        serialized_status = json.dumps(status, sort_keys=True)
        self.assertNotIn("payload", serialized_status)
        self.assertLess(len(serialized_status), 32 * 1024)

    def test_workflow_history_is_bounded_for_repeated_identical_runs(self) -> None:
        calls = 0

        class DebugBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                nonlocal calls
                calls += 1
                return {"ok": True, "payload": f"runtime-evidence-{calls}"}

        backend = OrchestratedMcpBackend({"debug_run": DebugBackend()})
        task = backend.open_task("bounded-workflow-history")
        arguments = {
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
            "ip": "192.0.2.11",
            "workflow": {
                "developer": {
                    "component_roots": ["/src/storage"],
                    "authored_files": ["src/handler.lua"],
                    "change_summary": "repair the diagnosed transition",
                }
            },
        }

        for _ in range(18):
            result = backend.debug_run(
                task,
                arguments,
                self.operation_context("bounded-workflow-history"),
            )
            self.assertTrue(result["completed"])

        self.assertEqual(calls, 18)
        status = backend.task_status(task)
        self.assertEqual(
            status["workflow_history"],
            {"entry_count": 16, "max_entries": 16},
        )
        self.assertEqual(len(status["automatic_workflows"]), 16)
        self.assertNotIn("runtime-evidence", json.dumps(status, sort_keys=True))

    def test_build_upgrade_strategy_carries_build_artifact_into_upgrade(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class DomainBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, arguments, _context):
                calls.append(("debug", dict(arguments)))
                return {
                    "ok": True,
                    "target_epoch": 4 if arguments.get("profile") == "freshness" else 0,
                    "evidence_ids": ["debug-evidence"],
                }

            @staticmethod
            def upgrade_run(_task, arguments, _context):
                calls.append(("upgrade", dict(arguments)))
                return {
                    "ok": True,
                    "epoch_after": 4,
                    "evidence_ids": ["upgrade-evidence"],
                }

        domain = DomainBackend()
        backend = OrchestratedMcpBackend(
            {"debug_run": domain, "upgrade_run": domain}
        )
        task = backend.open_task("build-upgrade-workflow")
        result = backend.debug_run(
            task,
            {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "ip": "192.0.2.11",
                "target_id": "candidate",
                "target_role": "candidate",
                "workflow": {
                    "developer": {
                        "component_roots": ["/src/storage"],
                        "authored_files": ["src/handler.c"],
                        "change_summary": "repair the compiled transition",
                    },
                    "build": {
                        "artifact_path": "/tmp/openubmc.hpm",
                        "artifact_sha256": "b" * 64,
                        "product_version": "2.1",
                        "evidence_ids": ["build-evidence"],
                    },
                    "upgrade": {},
                    "verification": {"keyword": "GetState"},
                },
            },
            self.operation_context("build-upgrade-workflow"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(
            [execution["key"] for execution in result["executions"]],
            [
                "debug:diagnosis",
                "developer:edit",
                "build:package",
                "upgrade:mutation",
                "debug:fresh_verification",
            ],
        )
        upgrade_arguments = next(arguments for name, arguments in calls if name == "upgrade")
        self.assertEqual(upgrade_arguments["artifact_path"], "/tmp/openubmc.hpm")
        self.assertEqual(upgrade_arguments["artifact_sha256"], "b" * 64)
        self.assertEqual(upgrade_arguments["product_version"], "2.1")
        self.assertEqual(upgrade_arguments["_task_intent"], "diagnose-and-fix")
        self.assertEqual(
            upgrade_arguments["_task_delivery_strategy"], "build-upgrade"
        )

    def test_changed_verification_reuses_mutation_and_runs_fresh_debug(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class DomainBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def upgrade_run(_task, arguments, _context):
                calls.append(("upgrade", dict(arguments)))
                return {"ok": True, "epoch_after": 2}

            @staticmethod
            def debug_run(_task, arguments, _context):
                calls.append(("debug", dict(arguments)))
                return {
                    "ok": True,
                    "target_epoch": 2,
                    "payload": "workflow-result-body-must-not-enter-status",
                }

        domain = DomainBackend()
        backend = OrchestratedMcpBackend(
            {"upgrade_run": domain, "debug_run": domain}
        )
        task = backend.open_task("iterative-upgrade-workflow")
        base = {
            "intent": "upgrade-and-verify",
            "ip": "192.0.2.11",
            "artifact_path": "/tmp/openubmc.hpm",
            "artifact_sha256": "c" * 64,
            "product_version": "2.0",
        }
        first = backend.upgrade_run(
            task,
            {**base, "workflow": {"verification": {"keyword": "ready"}}},
            self.operation_context("iterative-upgrade-workflow"),
        )
        second = backend.upgrade_run(
            task,
            {**base, "workflow": {"verification": {"keyword": "healthy"}}},
            self.operation_context("iterative-upgrade-workflow"),
        )

        self.assertTrue(first["completed"])
        self.assertTrue(second["completed"])
        self.assertEqual([name for name, _arguments in calls].count("upgrade"), 1)
        self.assertEqual([name for name, _arguments in calls].count("debug"), 2)
        status = backend.task_status(task)
        self.assertEqual(len(status["automatic_workflows"]), 2)
        self.assertEqual(status["workflow_history"]["entry_count"], 2)
        self.assertFalse(status["workflow_cache"]["enabled"])
        self.assertEqual(status["workflow_cache"]["entry_count"], 0)
        self.assertNotIn(
            "workflow-result-body-must-not-enter-status",
            json.dumps(status, sort_keys=True),
        )

    def test_mutation_operation_id_is_stable_across_task_recreation(
        self,
    ) -> None:
        mutation_operation_ids: list[str] = []

        class DomainBackend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def upgrade_run(_task, _arguments, context):
                mutation_operation_ids.append(context.operation_id)
                return {"ok": True, "epoch_after": 2}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                return {"ok": True, "target_epoch": 2}

        domain = DomainBackend()
        backend = OrchestratedMcpBackend(
            {"upgrade_run": domain, "debug_run": domain}
        )
        base = {
            "intent": "upgrade-and-verify",
            "ip": "192.0.2.11",
            "artifact_path": "/tmp/openubmc.hpm",
            "artifact_sha256": "d" * 64,
            "product_version": "2.0",
        }
        results = []
        for keyword in ("ready", "healthy"):
            task = backend.open_task("stable-upgrade-operation")
            results.append(
                backend.upgrade_run(
                    task,
                    {**base, "workflow": {"verification": {"keyword": keyword}}},
                    self.operation_context("stable-upgrade-operation"),
                )
            )
            backend.close_task(task)

        self.assertEqual(len(mutation_operation_ids), 2)
        self.assertEqual(len(set(mutation_operation_ids)), 1)
        self.assertTrue(mutation_operation_ids[0].startswith("op-mutation-"))
        for result in results:
            mutation_execution = next(
                execution
                for execution in result["executions"]
                if execution["phase"] == "mutation"
            )
            self.assertEqual(
                mutation_execution["operation_id"],
                mutation_operation_ids[0],
            )

    def test_automatic_workflow_rejects_stale_fresh_verification_epoch(self) -> None:
        class Backend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def upgrade_run(_task, _arguments, _context):
                return {"ok": True, "epoch_after": 4}

            @staticmethod
            def debug_run(_task, _arguments, _context):
                return {"ok": True, "target_epoch": 3}

        domain = Backend()
        backend = OrchestratedMcpBackend(
            {"upgrade_run": domain, "debug_run": domain}
        )
        task = backend.open_task("stale-verification-workflow")

        result = backend.upgrade_run(
            task,
            {
                "intent": "upgrade-and-verify",
                "ip": "192.0.2.11",
                "target_id": "candidate",
                "target_role": "candidate",
                "artifact_path": "/tmp/openubmc.hpm",
                "workflow": {"verification": {"keyword": "firmware ready"}},
            },
            self.operation_context("stale-verification-workflow"),
        )

        self.assertFalse(result["completed"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["executions"][-1]["status"], "failed")
        self.assertIn("stale target epoch", result["executions"][-1]["error"])

    def test_task_level_admission_blocks_debug_reads_during_live_patch(self) -> None:
        mutation_started = threading.Event()
        release_mutation = threading.Event()
        read_started = threading.Event()
        failures: list[BaseException] = []

        class Backend:
            @staticmethod
            def open_task(task_id: str):
                return {"task_id": task_id}

            @staticmethod
            def close_task(_task) -> None:
                return None

            @staticmethod
            def maintain_task(_task) -> int:
                return 0

            @staticmethod
            def task_status(_task) -> dict[str, object]:
                return {}

            @staticmethod
            def debug_run(_task, arguments, _context):
                if not arguments.get("warmup"):
                    read_started.set()
                return {"ok": True}

            @staticmethod
            def live_patch_run(_task, _arguments, _context):
                mutation_started.set()
                if not release_mutation.wait(2):
                    raise TimeoutError("test mutation was not released")
                return {"ok": True, "epoch_after": 1}

        domain = Backend()
        backend = OrchestratedMcpBackend(
            {"debug_run": domain, "live_patch_run": domain}
        )
        task = backend.open_task("cross-domain-admission")
        backend.debug_run(
            task,
            {
                "intent": "diagnose-and-fix",
                "final_purpose": "repair and verify the target",
                "ip": "192.0.2.11",
                "warmup": True,
            },
            self.operation_context("cross-domain-admission"),
        )

        def run_mutation() -> None:
            try:
                backend.live_patch_run(
                    task,
                    {"ip": "192.0.2.11"},
                    self.operation_context("cross-domain-admission"),
                )
            except BaseException as error:
                failures.append(error)

        def run_read() -> None:
            try:
                backend.debug_run(
                    task,
                    {"ip": "192.0.2.11"},
                    self.operation_context("cross-domain-admission"),
                )
            except BaseException as error:
                failures.append(error)

        mutation_thread = threading.Thread(target=run_mutation)
        read_thread = threading.Thread(target=run_read)
        mutation_thread.start()
        self.assertTrue(mutation_started.wait(1))
        read_thread.start()
        time.sleep(0.1)
        self.assertFalse(read_started.is_set())
        release_mutation.set()
        mutation_thread.join(2)
        read_thread.join(2)

        self.assertEqual(failures, [])
        self.assertTrue(read_started.is_set())

    def resolver(self):
        from openubmc_target_runtime import CredentialResolver

        return CredentialResolver(
            ssh_loader=lambda _selector: ResolvedSshCredentials(
                user="Administrator", password="secret"
            )
        )


if __name__ == "__main__":
    unittest.main()
