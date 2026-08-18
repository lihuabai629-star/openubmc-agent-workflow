"""Small dependency-free MCP surface for task-scoped openUBMC Debug runs."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Protocol, TypeVar
import uuid

from .contracts import (
    RUNTIME_API_VERSION,
    CredentialSelector,
    TargetPolicy,
    TargetSpec,
    _fingerprint,
)
from .catalog import OperationCatalog, OperationDescriptor
from .capability import (
    CallableDomainAdapter,
    CapabilityDescriptor,
    CapabilityRegistry,
    RuntimeSDK,
    RuntimeSDKContext,
)
from .context_runtime import (
    AGENT_ENVELOPE_MAX_BYTES,
    BlobRepository,
    CONTEXT_WORKFLOW_STEP_ARGUMENT,
    ContextRuntime,
    ContextToolResult,
    RuntimeRepository,
)
from .credential_file import load_selected_credentials_file
from .lifecycle import OperationContext, TaskRunRegistry
from .mutation import (
    MutationAuthorizedExceptions,
    TaskAuthorizationPolicy,
    TargetLeaseCoordinator,
    mutation_journal_operation_status,
)
from .task_context import TaskContextStore
from .orchestration import (
    DeliveryStrategy,
    DeveloperEditIntent,
    DomainExecutionContext,
    DomainOutcome,
    MutationDomainResult,
    TaskIntent,
    TaskIntentKind,
    TaskOrchestrationContext,
    TaskTargetBinding,
    TaskWorkflowOrchestrator,
    WorkflowStep,
)


TaskT = TypeVar("TaskT")
MCP_PROTOCOL_VERSION = "2025-06-18"


class DebugMcpBackend(Protocol[TaskT]):
    def open_task(self, task_id: str) -> TaskT: ...

    def close_task(self, task: TaskT) -> None: ...

    def maintain_task(self, task: TaskT) -> object: ...

    def task_status(self, task: TaskT) -> dict[str, object]: ...

    def debug_run(
        self,
        task: TaskT,
        arguments: Mapping[str, object],
        context: OperationContext,
    ) -> dict[str, object]: ...

    def debug_collect(
        self,
        task: TaskT,
        arguments: Mapping[str, object],
        context: OperationContext,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class _OperationBinding:
    name: str
    domain: str = ""
    lifecycle: str = "invoke"
    handler_name: str | None = None
    mutation: bool = False
    workflow_entry: bool = False
    credential_values: bool = False


_OPERATION_BINDINGS = (
    _OperationBinding(
        "debug_run",
        domain="debug",
        handler_name="debug_run",
        workflow_entry=True,
        credential_values=True,
    ),
    _OperationBinding(
        "debug_collect",
        domain="debug",
        handler_name="debug_collect",
        credential_values=True,
    ),
    _OperationBinding(
        "log_bundle_collect",
        domain="log_analyzer",
        handler_name="log_bundle_collect",
        workflow_entry=True,
        credential_values=True,
    ),
    _OperationBinding(
        "live_patch_run",
        domain="live_patch",
        handler_name="live_patch_run",
        mutation=True,
        workflow_entry=True,
        credential_values=True,
    ),
    _OperationBinding(
        "upgrade_run",
        domain="upgrade",
        handler_name="upgrade_run",
        mutation=True,
        workflow_entry=True,
        credential_values=True,
    ),
    _OperationBinding("case_read", lifecycle="read"),
    _OperationBinding("evidence_read", lifecycle="read"),
    _OperationBinding("case_close", lifecycle="close"),
    _OperationBinding("case_forget", lifecycle="close"),
    _OperationBinding("phase_record"),
    _OperationBinding("workflow.advance"),
    _OperationBinding("workflow.next"),
    _OperationBinding("runtime_status", lifecycle="status"),
)
_OPERATION_BINDING_BY_NAME = {
    binding.name: binding for binding in _OPERATION_BINDINGS
}
_TOOL_DOMAINS = {
    binding.name: binding.domain
    for binding in _OPERATION_BINDINGS
    if binding.domain
}
_ORCHESTRATION_ARGUMENTS = frozenset(
    {
        "intent",
        "final_purpose",
        "entry_domain",
        "delivery_strategy",
        "authorized_exceptions",
        "target_id",
        "target_role",
    }
)
_WORKFLOW_ARGUMENT = "workflow"
_TASK_AUTHORIZATION_POLICY_ARGUMENT = "_task_authorization_policy"
_INTERNAL_TASK_ARGUMENTS = frozenset(
    {
        _TASK_AUTHORIZATION_POLICY_ARGUMENT,
        "_task_intent",
        "_task_delivery_strategy",
        "_task_authorized_exceptions",
        "_credential_values",
    }
)
_DOMAIN_TO_TOOL = {
    binding.domain: binding.name
    for binding in _OPERATION_BINDINGS
    if binding.domain and binding.workflow_entry
}
_CREDENTIAL_VALUE_TOOLS = frozenset(
    binding.name for binding in _OPERATION_BINDINGS if binding.credential_values
)
_MUTATION_TOOLS = frozenset(
    binding.name for binding in _OPERATION_BINDINGS if binding.mutation
)
_CAPABILITY_CONTRACTS = {
    "debug_run": (
        "openubmc.debug.diagnose",
        "openubmc-debug",
        300.0,
        ("diagnosis", "runtime-observation"),
    ),
    "debug_collect": (
        "openubmc.debug.verify",
        "openubmc-debug",
        180.0,
        ("runtime-verification", "acceptance-outcome"),
    ),
    "log_bundle_collect": (
        "openubmc.logs.bundle",
        "openubmc-log-analyzer",
        600.0,
        ("diagnostic-bundle", "collection-outcome"),
    ),
    "live_patch_run": (
        "openubmc.delivery.live-patch",
        "openubmc-live-patch",
        600.0,
        ("mutation-journal", "deployment-verification"),
    ),
    "upgrade_run": (
        "openubmc.delivery.upgrade",
        "openubmc-upgrade",
        1800.0,
        ("mutation-journal", "deployment-identity"),
    ),
}
_CONTEXT_OPERATION_LIFECYCLES = {
    "case_read": "read",
    "evidence_read": "read",
    "case_close": "close",
    "case_forget": "close",
    "phase_record": "invoke",
    "workflow.advance": "invoke",
    "workflow.next": "invoke",
    "runtime_status": "status",
}
_EXTERNAL_WORKFLOW_DOMAINS = frozenset({"developer", "build"})
_MAX_ORCHESTRATION_HISTORY = 16
_MAX_WORKFLOW_SUMMARIES = 16
_MAX_MUTATION_OUTCOMES = 32
_PERSISTENCE_TOUCH_INTERVAL_SECONDS = 300.0
_SECRET_ARGUMENTS = frozenset(
    {"ssh_password", "telnet_password", "redfish_password", "password"}
)
_SHARED_ARGUMENTS = frozenset(
    {
        "ip",
        "ssh_port",
        "telnet_port",
        "redfish_port",
        "ssh_user",
        "ssh_user_env",
        "ssh_password_env",
        "ssh_identity_file",
        "telnet_user",
        "telnet_user_env",
        "telnet_password_env",
        "redfish_user",
        "redfish_user_env",
        "redfish_password_env",
        "ssh_host_key_policy",
        "ssh_known_hosts_file",
        "allow_insecure_host_key",
        "allow_insecure_tls",
    }
)
_REUSABLE_CONNECTION_ARGUMENTS = frozenset(
    {
        "ssh_user",
        "ssh_password",
        "telnet_user",
        "telnet_password",
        "redfish_user",
        "redfish_password",
    }
)
_TARGET_ADDRESS_ARGUMENTS = frozenset({"ip"})
_SSH_ARGUMENTS = frozenset(
    {
        "ssh_port",
        "ssh_user",
        "ssh_user_env",
        "ssh_password_env",
        "ssh_identity_file",
    }
)
_TELNET_ARGUMENTS = frozenset(
    {
        "telnet_port",
        "telnet_user",
        "telnet_user_env",
        "telnet_password_env",
    }
)
_REDFISH_ARGUMENTS = frozenset(
    {
        "redfish_port",
        "redfish_user",
        "redfish_user_env",
        "redfish_password_env",
    }
)
_SSH_SECRET_ARGUMENTS = frozenset({"ssh_password"})
_TELNET_SECRET_ARGUMENTS = frozenset({"telnet_password"})
_REDFISH_SECRET_ARGUMENTS = frozenset({"redfish_password"})
_DOMAIN_CONNECTION_ARGUMENTS = {
    "debug": (
        _TARGET_ADDRESS_ARGUMENTS
        | _SSH_ARGUMENTS
        | _TELNET_ARGUMENTS
        | frozenset(
            {
                "ssh_host_key_policy",
                "ssh_known_hosts_file",
                "allow_insecure_host_key",
            }
        )
    ),
    "log_analyzer": (
        _TARGET_ADDRESS_ARGUMENTS
        | _SSH_ARGUMENTS
        | _REDFISH_ARGUMENTS
        | _SSH_SECRET_ARGUMENTS
        | _REDFISH_SECRET_ARGUMENTS
    ),
    "live_patch": (
        _TARGET_ADDRESS_ARGUMENTS
        | _SSH_ARGUMENTS
        | _TELNET_ARGUMENTS
        | _SSH_SECRET_ARGUMENTS
        | _TELNET_SECRET_ARGUMENTS
        | frozenset({"ssh_host_key_policy", "ssh_known_hosts_file"})
    ),
    "upgrade": (
        _TARGET_ADDRESS_ARGUMENTS
        | _REDFISH_ARGUMENTS
        | frozenset({"allow_insecure_tls"})
    ),
}
_DOMAIN_ARGUMENTS_TO_STRIP = {
    "debug": frozenset({"problem"}),
}
_WORKFLOW_SECTION_PROTECTED_ARGUMENTS = (
    _ORCHESTRATION_ARGUMENTS
    | _SHARED_ARGUMENTS
    | _SECRET_ARGUMENTS
    | frozenset(
        {
            "targets",
            "role",
            CONTEXT_WORKFLOW_STEP_ARGUMENT,
        }
    )
)


class _OrchestratedMcpTask:
    def __init__(
        self,
        task_id: str,
        tool_backends: Mapping[str, object],
        *,
        state_store: TaskContextStore | None = None,
    ) -> None:
        self.task_id = task_id
        self.tool_backends = dict(tool_backends)
        self._state_store = state_store
        self.orchestration: TaskOrchestrationContext | None = None
        self._orchestration_history: list[TaskOrchestrationContext] = []
        self._shared_arguments: dict[str, object] = {}
        self._target_arguments: dict[str, dict[str, object]] = {}
        self._resources: dict[int, object] = {}
        self._resource_tools: dict[str, object] = {}
        self._credential_values: dict[str, str] | None = None
        self._credential_parse_count = 0
        self._workflow_summaries: list[dict[str, object]] = []
        self._mutation_outcomes: OrderedDict[str, DomainOutcome[object]] = OrderedDict()
        self._mutation_journal_identities: OrderedDict[str, dict[str, str]] = (
            OrderedDict()
        )
        self._target_admissions: dict[str, TargetLeaseCoordinator] = {}
        self._lock = threading.RLock()
        self._workflow_lock = threading.RLock()
        self._persistence_lock = threading.RLock()
        self._last_persisted_digest = ""
        self._last_persisted_at = 0.0
        self._failed_persisted_digest = ""
        self._persistence_error = ""
        self._persistence_disabled = False
        self._recovered_context = False
        self._restoring_context = False
        self._restore_context()

    @staticmethod
    def _persisted_workflow_summary(raw: object) -> dict[str, object] | None:
        if not isinstance(raw, Mapping):
            return None
        request_fingerprint = raw.get("request_fingerprint")
        if not isinstance(request_fingerprint, str) or not request_fingerprint:
            return None
        intent = raw.get("intent")
        phase_states = raw.get("phase_states")
        return {
            "request_fingerprint": request_fingerprint,
            "completed": bool(raw.get("completed", False)),
            "partial": bool(raw.get("partial", False)),
            "next_action": str(raw.get("next_action", "")),
            "intent": dict(intent) if isinstance(intent, Mapping) else None,
            "phase_states": (
                dict(phase_states) if isinstance(phase_states, Mapping) else {}
            ),
        }

    def _snapshot_context(self) -> dict[str, object] | None:
        with self._lock:
            orchestration = self.orchestration
            if orchestration is None:
                return None
            intent = orchestration.intent
            shared_arguments = dict(self._shared_arguments)
            target_payloads = [
                {
                    key: value
                    for key, value in self._target_arguments[target.target_id].items()
                    if key not in _SECRET_ARGUMENTS
                }
                for target in intent.targets
                if target.target_id in self._target_arguments
            ]
        with self._workflow_lock:
            workflow_summaries = [
                {
                    **summary,
                    "intent": (
                        dict(summary["intent"])
                        if isinstance(summary.get("intent"), Mapping)
                        else None
                    ),
                    "phase_states": (
                        dict(summary["phase_states"])
                        if isinstance(summary.get("phase_states"), Mapping)
                        else {}
                    ),
                }
                for summary in self._workflow_summaries
            ]
            mutation_journals = [
                dict(identity)
                for identity in self._mutation_journal_identities.values()
            ]
        return {
            "orchestration": {
                "intent": intent.original_intent.value,
                "final_purpose": intent.final_purpose,
                "entry_domain": intent.entry_domain,
                "delivery_strategy": (
                    intent.delivery_strategy.value
                    if intent.delivery_strategy is not None
                    else None
                ),
                "authorized_exceptions": (
                    intent.authorization.authorized_exceptions.to_public_dict()
                ),
                "authorization_policy": intent.authorization.to_public_dict(),
            },
            "shared_arguments": shared_arguments,
            "targets": target_payloads,
            "workflow_summaries": workflow_summaries,
            "mutation_journal_identities": mutation_journals,
        }

    @staticmethod
    def _context_digest(context: Mapping[str, object]) -> str:
        encoded = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _persist_context(self, *, force: bool = False) -> None:
        if self._state_store is None or self._restoring_context:
            return
        context = self._snapshot_context()
        if context is None:
            return
        digest = self._context_digest(context)
        now = time.monotonic()
        with self._persistence_lock:
            if self._persistence_disabled or self._restoring_context:
                return
            with self._lock:
                if not force and digest == self._failed_persisted_digest:
                    return
                if (
                    not force
                    and digest == self._last_persisted_digest
                    and now - self._last_persisted_at
                    < _PERSISTENCE_TOUCH_INTERVAL_SECONDS
                ):
                    return
            try:
                self._state_store.save(self.task_id, context)
            except Exception as exc:
                self._state_store.delete(self.task_id)
                with self._lock:
                    self._last_persisted_digest = ""
                    self._last_persisted_at = 0.0
                    self._failed_persisted_digest = digest
                    self._persistence_error = f"{type(exc).__name__}: {exc}"
                return
            with self._lock:
                self._last_persisted_digest = digest
                self._last_persisted_at = now
                self._failed_persisted_digest = ""
                self._persistence_error = ""

    def disable_persistence(self) -> None:
        with self._persistence_lock:
            self._persistence_disabled = True
            if self._state_store is not None:
                self._state_store.delete(self.task_id)
            with self._lock:
                self._last_persisted_digest = ""
                self._last_persisted_at = 0.0
                self._failed_persisted_digest = ""

    def seal_persistence(self) -> None:
        """Persist the final task projection, then reject late background writes."""

        self._persist_context(force=True)
        with self._persistence_lock:
            self._persistence_disabled = True

    def _restore_context(self) -> None:
        if self._state_store is None:
            return
        try:
            raw = self._state_store.load(self.task_id)
        except Exception as exc:
            self._persistence_error = f"{type(exc).__name__}: {exc}"
            return
        if raw is None:
            return
        try:
            orchestration = raw.get("orchestration")
            targets = raw.get("targets")
            shared = raw.get("shared_arguments")
            if not isinstance(orchestration, Mapping):
                raise ValueError("persisted orchestration is unavailable")
            if not isinstance(targets, list) or not targets or not all(
                isinstance(target, Mapping) for target in targets
            ):
                raise ValueError("persisted targets are unavailable")
            arguments = dict(shared) if isinstance(shared, Mapping) else {}
            arguments["targets"] = [dict(target) for target in targets]
            for source, destination in (
                ("intent", "intent"),
                ("final_purpose", "final_purpose"),
                ("entry_domain", "entry_domain"),
                ("delivery_strategy", "delivery_strategy"),
            ):
                value = orchestration.get(source)
                if isinstance(value, str) and value.strip():
                    arguments[destination] = value.strip()
            raw_policy = orchestration.get("authorization_policy")
            if raw_policy is not None:
                if not isinstance(raw_policy, Mapping):
                    raise TypeError("persisted authorization_policy must be an object")
                policy = TaskAuthorizationPolicy.from_public_dict(raw_policy)
                restored_intent = arguments.get("intent")
                if restored_intent != policy.original_intent:
                    raise ValueError(
                        "persisted authorization policy intent does not match orchestration"
                    )
                restored_delivery = str(arguments.get("delivery_strategy", ""))
                if (
                    policy.original_intent == "diagnose-and-fix"
                    and restored_delivery != policy.delivery_strategy
                ):
                    raise ValueError(
                        "persisted authorization policy delivery strategy does not match orchestration"
                    )
                arguments["authorized_exceptions"] = (
                    policy.authorized_exceptions.to_public_dict()
                )
                arguments["allow_insecure_tls"] = policy.allow_insecure_tls
            else:
                authorized_exceptions = orchestration.get("authorized_exceptions")
                if isinstance(authorized_exceptions, Mapping):
                    arguments["authorized_exceptions"] = dict(authorized_exceptions)
            entry_domain = orchestration.get("entry_domain")
            preferred_tool = (
                _DOMAIN_TO_TOOL.get(entry_domain)
                if isinstance(entry_domain, str)
                else None
            )
            tool_name = (
                preferred_tool
                if preferred_tool in self.tool_backends
                else next(iter(self.tool_backends))
            )
            self._restoring_context = True
            try:
                self.bind_intent(tool_name, arguments)
            finally:
                self._restoring_context = False
            raw_summaries = raw.get("workflow_summaries", [])
            summaries = (
                [
                    summary
                    for summary in (
                        self._persisted_workflow_summary(item)
                        for item in raw_summaries
                    )
                    if summary is not None
                ]
                if isinstance(raw_summaries, list)
                else []
            )
            raw_identities = raw.get("mutation_journal_identities", [])
            identities: OrderedDict[str, dict[str, str]] = OrderedDict()
            if isinstance(raw_identities, list):
                for item in raw_identities:
                    if not isinstance(item, Mapping):
                        continue
                    fingerprint = item.get("request_fingerprint")
                    domain = item.get("domain")
                    operation_id = item.get("operation_id")
                    if not all(
                        isinstance(value, str) and value
                        for value in (fingerprint, domain, operation_id)
                    ):
                        continue
                    identities[fingerprint] = {
                        "request_fingerprint": fingerprint,
                        "domain": domain,
                        "operation_id": operation_id,
                    }
            with self._workflow_lock:
                self._workflow_summaries = summaries[-_MAX_WORKFLOW_SUMMARIES:]
                self._mutation_journal_identities = OrderedDict(
                    list(identities.items())[-_MAX_MUTATION_OUTCOMES:]
                )
            self._recovered_context = True
            context = self._snapshot_context()
            if context is not None:
                self._last_persisted_digest = self._context_digest(context)
                self._last_persisted_at = time.monotonic()
        except Exception as exc:
            self._persistence_error = f"{type(exc).__name__}: {exc}"
            self._state_store.delete(self.task_id)

    @staticmethod
    def _target_role(
        raw: Mapping[str, object],
        common: Mapping[str, object],
        *,
        multiple: bool,
    ) -> str:
        value = raw.get("role", common.get("target_role", common.get("reference_role")))
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return "symmetric" if multiple else "candidate"

    @staticmethod
    def _selector_arguments(
        raw: Mapping[str, object], common: Mapping[str, object], name: str
    ) -> str:
        value = raw.get(name, common.get(name, ""))
        return str(value) if isinstance(value, (str, int)) else ""

    def _target_bindings(
        self, arguments: Mapping[str, object]
    ) -> tuple[TaskTargetBinding, ...]:
        raw_targets = arguments.get("targets")
        if raw_targets is None:
            raw_targets = [arguments]
        if not isinstance(raw_targets, list) or not raw_targets or not all(
            isinstance(target, Mapping) for target in raw_targets
        ):
            raise ValueError("targets must be a non-empty array of target objects")
        multiple = len(raw_targets) > 1
        bindings: list[TaskTargetBinding] = []
        for index, raw in enumerate(raw_targets, start=1):
            host = raw.get("ip", arguments.get("ip"))
            if not isinstance(host, str) or not host.strip():
                raise ValueError("the first domain call must provide ip or targets")
            ssh_identity_source = self._selector_arguments(
                raw, arguments, "ssh_identity_file"
            )
            ssh_selector = CredentialSelector.for_ssh(
                user=self._selector_arguments(raw, arguments, "ssh_user"),
                user_env=self._selector_arguments(raw, arguments, "ssh_user_env"),
                password_env=self._selector_arguments(
                    raw, arguments, "ssh_password_env"
                ),
                identity_file=ssh_identity_source,
                environ=os.environ,
            )
            telnet_selector = CredentialSelector.for_telnet(
                user=self._selector_arguments(raw, arguments, "telnet_user"),
                user_env=self._selector_arguments(raw, arguments, "telnet_user_env"),
                password_env=self._selector_arguments(
                    raw, arguments, "telnet_password_env"
                ),
                environ=os.environ,
            )
            redfish_selector = CredentialSelector.for_redfish(
                user=self._selector_arguments(raw, arguments, "redfish_user"),
                user_env=self._selector_arguments(
                    raw, arguments, "redfish_user_env"
                ),
                password_env=self._selector_arguments(
                    raw, arguments, "redfish_password_env"
                ),
                environ=os.environ,
            )
            selectors = (ssh_selector, telnet_selector, redfish_selector)
            policy_name = self._selector_arguments(
                raw, arguments, "ssh_host_key_policy"
            ) or "insecure"
            target = TargetSpec.for_credential_selectors(
                host=host,
                ssh_port=int(raw.get("ssh_port", arguments.get("ssh_port", 22))),
                telnet_port=int(
                    raw.get("telnet_port", arguments.get("telnet_port", 23))
                ),
                redfish_port=int(
                    raw.get("redfish_port", arguments.get("redfish_port", 443))
                ),
                credential_selectors=selectors,
                policy=TargetPolicy(
                    read_only=str(arguments.get("intent", "diagnosis-only"))
                    .strip()
                    .lower()
                    in {
                        "diagnosis-only",
                        "debug-only",
                        "diagnose",
                        "bundle-and-diagnose",
                    },
                    ssh_host_key_policy=policy_name,
                ),
            )
            target_id_value = raw.get(
                "target_id",
                arguments.get("target_id", f"target-{index}"),
            )
            bindings.append(
                TaskTargetBinding(
                    target_id=str(target_id_value),
                    role=self._target_role(
                        raw, arguments, multiple=multiple
                    ),
                    target=target,
                    credential_selectors=selectors,
                )
            )
        return tuple(bindings)

    @staticmethod
    def _inferred_delivery_strategy(
        intent_value: str,
        arguments: Mapping[str, object],
        *,
        tool_name: str,
    ) -> str | None:
        if TaskIntentKind.parse(intent_value) is not TaskIntentKind.DIAGNOSE_AND_FIX:
            return None
        supplied = arguments.get("delivery_strategy")
        if isinstance(supplied, str) and supplied.strip():
            return supplied
        workflow = arguments.get(_WORKFLOW_ARGUMENT)
        if isinstance(workflow, Mapping):
            has_build_upgrade = "build" in workflow or "upgrade" in workflow
            has_live_patch = "live_patch" in workflow
            if has_build_upgrade and has_live_patch:
                raise ValueError(
                    "diagnose-and-fix workflow cannot mix live_patch with build/upgrade"
                )
            if has_build_upgrade:
                return DeliveryStrategy.BUILD_UPGRADE.value
            if has_live_patch:
                return DeliveryStrategy.LIVE_PATCH.value
            if "developer" in workflow:
                return DeliveryStrategy.SOURCE_ONLY.value
        domain = _TOOL_DOMAINS.get(tool_name)
        if domain == "live_patch":
            return DeliveryStrategy.LIVE_PATCH.value
        if domain == "upgrade":
            return DeliveryStrategy.BUILD_UPGRADE.value
        return None

    def _new_intent(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> TaskIntent:
        domain = _TOOL_DOMAINS[tool_name]
        intent_value = arguments.get("intent")
        if not isinstance(intent_value, str) or not intent_value.strip():
            if (
                domain == "live_patch"
                and str(arguments.get("action", "")).strip().lower()
                == "rollback"
            ):
                intent_value = "rollback"
            else:
                intent_value = {
                    "upgrade": "upgrade-and-verify",
                    "live_patch": "live-patch",
                }.get(domain, "diagnosis-only")
        entry_domain = arguments.get("entry_domain", domain)
        if not isinstance(entry_domain, str):
            raise TypeError("entry_domain must be a string")
        final_purpose = arguments.get("final_purpose")
        if not isinstance(final_purpose, str) or not final_purpose.strip():
            final_purpose = next(
                (
                    str(arguments[key]).strip()
                    for key in ("problem", "keyword", "profile")
                    if isinstance(arguments.get(key), str)
                    and str(arguments[key]).strip()
                ),
                f"complete the {intent_value} task",
            )
        authorized_exceptions = arguments.get("authorized_exceptions")
        if authorized_exceptions is not None and not isinstance(
            authorized_exceptions, Mapping
        ):
            raise TypeError("authorized_exceptions must be an object")
        allow_insecure_tls = arguments.get("allow_insecure_tls", True)
        if not isinstance(allow_insecure_tls, bool):
            raise TypeError("allow_insecure_tls must be a boolean")
        return TaskIntent.create(
            original_intent=intent_value,
            final_purpose=final_purpose,
            entry_domain=entry_domain,
            targets=self._target_bindings(arguments),
            delivery_strategy=self._inferred_delivery_strategy(
                intent_value,
                arguments,
                tool_name=tool_name,
            ),
            authorized_exceptions=authorized_exceptions,
            allow_insecure_tls=allow_insecure_tls,
        )

    def _remember_orchestration(self, context: TaskOrchestrationContext) -> None:
        self._orchestration_history.append(context)
        del self._orchestration_history[:-_MAX_ORCHESTRATION_HISTORY]

    def _current_target_payloads(
        self,
        arguments: Mapping[str, object],
    ) -> list[dict[str, object]]:
        assert self.orchestration is not None
        selected = self._explicit_target_id(arguments)
        connection_overrides = {
            key: value
            for key, value in arguments.items()
            if key in (_SHARED_ARGUMENTS | _SECRET_ARGUMENTS) and key != "ip"
        }
        payloads: list[dict[str, object]] = []
        for binding in self.orchestration.intent.targets:
            payload = dict(self._target_arguments.get(binding.target_id, {}))
            payload.update(
                {
                    "ip": binding.target.host,
                    "ssh_port": binding.target.ssh_port,
                    "telnet_port": binding.target.telnet_port,
                    "redfish_port": binding.target.redfish_port,
                    "target_id": binding.target_id,
                    "role": binding.role,
                }
            )
            if not selected or selected == binding.target_id:
                payload.update(connection_overrides)
            payloads.append(payload)
        return payloads

    def _merged_intent_arguments(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        current = self.orchestration
        supplied_ip = arguments.get("ip")
        has_ip = isinstance(supplied_ip, str) and bool(supplied_ip.strip())
        has_targets = "targets" in arguments
        merged = dict(self._shared_arguments)
        if current is not None and (has_ip or has_targets):
            merged = {
                key: value
                for key, value in self._shared_arguments.items()
                if key in _REUSABLE_CONNECTION_ARGUMENTS
            }
            if len(current.intent.targets) == 1:
                current_target = current.intent.targets[0]
                current_payload = self._target_arguments.get(
                    current_target.target_id, {}
                )
                merged.update(
                    {
                        key: value
                        for key, value in current_payload.items()
                        if key in _REUSABLE_CONNECTION_ARGUMENTS
                    }
                )
                if any(
                    key in current_payload
                    for key in (
                        "ssh_password",
                        "telnet_password",
                        "redfish_password",
                    )
                ) and "ssh_host_key_policy" in current_payload:
                    merged["ssh_host_key_policy"] = current_payload[
                        "ssh_host_key_policy"
                    ]
        if current is not None and not has_ip and not has_targets:
            merged.pop("ip", None)
            merged["targets"] = self._current_target_payloads(arguments)
        elif has_targets:
            merged.pop("ip", None)
        merged.update(arguments)
        if current is None:
            merged.setdefault("allow_insecure_tls", True)
            return merged
        if has_ip and not has_targets and len(current.intent.targets) == 1:
            current_target = current.intent.targets[0]
            merged.setdefault("target_id", current_target.target_id)
            merged.setdefault("target_role", current_target.role)

        supplied_intent = arguments.get("intent")
        if (
            tool_name == "live_patch_run"
            and (not isinstance(supplied_intent, str) or not supplied_intent.strip())
        ):
            supplied_action = str(arguments.get("action", "")).strip().lower()
            if supplied_action == "rollback":
                supplied_intent = "rollback"
                merged["intent"] = supplied_intent
        same_intent = (
            not isinstance(supplied_intent, str)
            or not supplied_intent.strip()
        )
        if isinstance(supplied_intent, str) and supplied_intent.strip():
            same_intent = (
                TaskIntentKind.parse(supplied_intent)
                is current.intent.original_intent
            )
        if same_intent:
            inferred = self._inferred_delivery_strategy(
                current.intent.original_intent.value,
                arguments,
                tool_name=tool_name,
            )
            current_delivery = (
                current.intent.delivery_strategy.value
                if current.intent.delivery_strategy is not None
                else ""
            )
            selecting_delivery = (
                current.intent.original_intent
                is TaskIntentKind.DIAGNOSE_AND_FIX
                and current_delivery == DeliveryStrategy.SOURCE_ONLY.value
                and inferred
                in {
                    DeliveryStrategy.LIVE_PATCH.value,
                    DeliveryStrategy.BUILD_UPGRADE.value,
                }
            )
            merged.setdefault("intent", current.intent.original_intent.value)
            merged.setdefault("entry_domain", current.intent.entry_domain)
            merged.setdefault("final_purpose", current.intent.final_purpose)
            current_exceptions = (
                current.intent.authorization.authorized_exceptions.to_public_dict()
            )
            if selecting_delivery:
                merged.setdefault("authorized_exceptions", current_exceptions)
                if current.intent.authorization.allow_insecure_tls:
                    merged.setdefault("allow_insecure_tls", True)
            else:
                supplied_exceptions = arguments.get("authorized_exceptions")
                if supplied_exceptions is None:
                    merged["authorized_exceptions"] = current_exceptions
                else:
                    if not isinstance(supplied_exceptions, Mapping):
                        raise TypeError("authorized_exceptions must be an object")
                    requested_exceptions = MutationAuthorizedExceptions.from_value(
                        {
                            **current_exceptions,
                            **dict(supplied_exceptions),
                        }
                    ).to_public_dict()
                    merged["authorized_exceptions"] = {
                        name: current_exceptions[name]
                        and requested_exceptions[name]
                        for name in current_exceptions
                    }
                if "allow_insecure_tls" in arguments:
                    supplied_tls = arguments["allow_insecure_tls"]
                    if not isinstance(supplied_tls, bool):
                        raise TypeError("allow_insecure_tls must be a boolean")
                    merged["allow_insecure_tls"] = (
                        current.intent.authorization.allow_insecure_tls
                        and supplied_tls
                    )
                elif current.intent.authorization.allow_insecure_tls:
                    merged["allow_insecure_tls"] = True
            if "delivery_strategy" not in arguments:
                if inferred is not None:
                    merged["delivery_strategy"] = inferred
                elif current.intent.delivery_strategy is not None:
                    merged["delivery_strategy"] = (
                        current.intent.delivery_strategy.value
                    )
        else:
            merged.setdefault("final_purpose", current.intent.final_purpose)
            merged.setdefault(
                "authorized_exceptions",
                current.intent.authorization.authorized_exceptions.to_public_dict(),
            )
            merged.setdefault(
                "allow_insecure_tls",
                current.intent.authorization.allow_insecure_tls,
            )
        return merged

    @staticmethod
    def _captured_target_arguments(
        arguments: Mapping[str, object],
        intent: TaskIntent,
    ) -> dict[str, dict[str, object]]:
        raw_targets = arguments.get("targets")
        if raw_targets is None:
            raw_targets = [arguments]
        assert isinstance(raw_targets, list)
        common = {
            key: value
            for key, value in arguments.items()
            if key in (_SHARED_ARGUMENTS | _SECRET_ARGUMENTS) and key != "ip"
        }
        captured: dict[str, dict[str, object]] = {}
        for binding, raw in zip(intent.targets, raw_targets, strict=True):
            assert isinstance(raw, Mapping)
            payload = dict(common)
            payload.update(
                {
                    key: value
                    for key, value in raw.items()
                    if key in (_SHARED_ARGUMENTS | _SECRET_ARGUMENTS)
                    and key != "ip"
                }
            )
            payload.update(
                {
                    "ip": binding.target.host,
                    "ssh_port": binding.target.ssh_port,
                    "telnet_port": binding.target.telnet_port,
                    "redfish_port": binding.target.redfish_port,
                    "target_id": binding.target_id,
                    "role": binding.role,
                }
            )
            captured[binding.target_id] = payload
        return captured

    @staticmethod
    def _credential_selector_fingerprints(intent: TaskIntent) -> frozenset[str]:
        return frozenset(
            selector.fingerprint
            for target in intent.targets
            for selector in target.credential_selectors
        )

    def bind_intent(self, tool_name: str, arguments: Mapping[str, object]) -> None:
        with self._lock:
            current = self.orchestration
            merged = self._merged_intent_arguments(tool_name, arguments)
            intent = self._new_intent(tool_name, merged)
            if (
                current is not None
                and self._credential_selector_fingerprints(current.intent)
                != self._credential_selector_fingerprints(intent)
            ):
                self._credential_values = None
            if current is not None and current.intent.fingerprint != intent.fingerprint:
                self._remember_orchestration(current)
            if current is None or current.intent.fingerprint != intent.fingerprint:
                self.orchestration = TaskOrchestrationContext(
                    task_id=self.task_id,
                    intent=intent,
                )
            self._shared_arguments = {
                key: value
                for key, value in merged.items()
                if key in _SHARED_ARGUMENTS
                and key != "ip"
                and key not in _SECRET_ARGUMENTS
            }
            self._target_arguments = self._captured_target_arguments(
                merged,
                intent,
            )
        self._persist_context()

    @staticmethod
    def _explicit_target_id(arguments: Mapping[str, object]) -> str:
        selected_id = arguments.get("target_id")
        return (
            selected_id.strip()
            if isinstance(selected_id, str) and selected_id.strip()
            else ""
        )

    def _selected_target(self, arguments: Mapping[str, object]) -> TaskTargetBinding:
        assert self.orchestration is not None
        targets = self.orchestration.intent.targets
        selected_id = self._explicit_target_id(arguments)
        if selected_id:
            for target in targets:
                if target.target_id == selected_id:
                    return target
            raise ValueError(f"unknown task target_id: {selected_id}")
        if len(targets) == 1:
            return targets[0]
        candidates = [target for target in targets if target.role == "candidate"]
        if len(candidates) == 1:
            return candidates[0]
        return targets[0]

    def _selected_mutation_target(
        self, arguments: Mapping[str, object]
    ) -> TaskTargetBinding:
        assert self.orchestration is not None
        targets = self.orchestration.intent.targets
        selected_id = self._explicit_target_id(arguments)
        if selected_id:
            return self._selected_target(arguments)
        if len(targets) == 1:
            return targets[0]
        candidates = [target for target in targets if target.role == "candidate"]
        if len(candidates) == 1:
            return candidates[0]
        eligible = candidates or list(targets)
        label = "candidate" if candidates else "task"
        target_ids = ", ".join(sorted(target.target_id for target in eligible))
        raise ValueError(
            "mutation target is ambiguous; specify target_id from "
            f"{label} targets: {target_ids}"
        )

    @staticmethod
    def _project_domain_arguments(
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        domain = _TOOL_DOMAINS[tool_name]
        allowed = _DOMAIN_CONNECTION_ARGUMENTS[domain]
        if arguments.get("_context_authoritative") is True:
            allowed = allowed | {
                "debug": _SSH_SECRET_ARGUMENTS | _TELNET_SECRET_ARGUMENTS,
                "log_analyzer": _SSH_SECRET_ARGUMENTS | _REDFISH_SECRET_ARGUMENTS,
                "live_patch": _SSH_SECRET_ARGUMENTS | _TELNET_SECRET_ARGUMENTS,
                "upgrade": _REDFISH_SECRET_ARGUMENTS,
            }[domain]
        projected = dict(arguments)
        disallowed = (
            ((_SHARED_ARGUMENTS | _SECRET_ARGUMENTS) - allowed)
            | _DOMAIN_ARGUMENTS_TO_STRIP.get(domain, frozenset())
        )
        for key in disallowed:
            projected.pop(key, None)
        raw_targets = projected.get("targets")
        if isinstance(raw_targets, list):
            projected["targets"] = [
                (
                    {
                        key: value
                        for key, value in target.items()
                        if key not in disallowed
                    }
                    if isinstance(target, Mapping)
                    else target
                )
                for target in raw_targets
            ]
        projected.pop("_context_authoritative", None)
        return projected

    def arguments_for(
        self, tool_name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        self.bind_intent(tool_name, arguments)
        assert self.orchestration is not None
        merged = dict(self._shared_arguments)
        if (
            tool_name == "debug_run"
            and len(self.orchestration.intent.targets) > 1
            and not self._explicit_target_id(arguments)
        ):
            merged.update(arguments)
            raw_targets = arguments.get("targets")
            target_payloads: list[dict[str, object]] = []
            for index, target in enumerate(self.orchestration.intent.targets):
                payload = dict(self._target_arguments[target.target_id])
                if (
                    isinstance(raw_targets, list)
                    and index < len(raw_targets)
                    and isinstance(raw_targets[index], Mapping)
                ):
                    payload.update(
                        {
                            key: value
                            for key, value in raw_targets[index].items()
                            if key not in _SECRET_ARGUMENTS and not key.startswith("_")
                        }
                    )
                target_payloads.append(payload)
            merged["targets"] = target_payloads
            merged.pop("ip", None)
        else:
            target = (
                self._selected_mutation_target(arguments)
                if tool_name in _MUTATION_TOOLS
                else self._selected_target(arguments)
            )
            merged.update(self._target_arguments[target.target_id])
            merged.update(arguments)
            merged["ip"] = target.target.host
            if tool_name in {
                "debug_run",
                "debug_collect",
                "log_bundle_collect",
                "live_patch_run",
                "upgrade_run",
            }:
                merged["ssh_port"] = target.target.ssh_port
            if tool_name in {"debug_run", "debug_collect", "live_patch_run"}:
                merged["telnet_port"] = target.target.telnet_port
            if tool_name in {"log_bundle_collect", "upgrade_run"}:
                merged["redfish_port"] = target.target.redfish_port
            merged.pop("targets", None)
        merged.pop(_WORKFLOW_ARGUMENT, None)
        for key in _ORCHESTRATION_ARGUMENTS:
            merged.pop(key, None)
        merged.pop("role", None)
        merged = self._project_domain_arguments(tool_name, merged)
        if tool_name in _CREDENTIAL_VALUE_TOOLS:
            merged["_credential_values"] = self.credential_values()
        if tool_name in {"live_patch_run", "upgrade_run"}:
            merged["_task_intent"] = (
                self.orchestration.intent.original_intent.value
            )
            frozen_delivery_strategy = ""
            if arguments.get(CONTEXT_WORKFLOW_STEP_ARGUMENT) is True:
                frozen_delivery_strategy = str(
                    arguments.get("delivery_strategy", "")
                ).strip()
            merged["_task_delivery_strategy"] = frozen_delivery_strategy or (
                self.orchestration.intent.delivery_strategy.value
                if self.orchestration.intent.delivery_strategy is not None
                else ""
            )
            merged["_task_authorized_exceptions"] = (
                self.orchestration.intent.authorization.authorized_exceptions.to_public_dict()
            )
            merged[_TASK_AUTHORIZATION_POLICY_ARGUMENT] = (
                self.orchestration.intent.authorization.to_public_dict()
            )
        merged.pop(CONTEXT_WORKFLOW_STEP_ARGUMENT, None)
        return merged

    def credential_values(self) -> dict[str, str]:
        with self._lock:
            if self._credential_values is None:
                self._credential_values = load_selected_credentials_file()
                self._credential_parse_count += 1
            return dict(self._credential_values)

    def record_workflow_summary(
        self,
        request_fingerprint: str,
        result: dict[str, object],
    ) -> None:
        with self._workflow_lock:
            self._workflow_summaries.append(
                self._workflow_summary(request_fingerprint, result)
            )
            if len(self._workflow_summaries) > _MAX_WORKFLOW_SUMMARIES:
                del self._workflow_summaries[:-_MAX_WORKFLOW_SUMMARIES]
        self._persist_context()

    def record_mutation_identity(
        self,
        domain: str,
        request_fingerprint: str,
        operation_id: str,
    ) -> None:
        identity = {
            "domain": str(domain),
            "request_fingerprint": str(request_fingerprint),
            "operation_id": str(operation_id),
        }
        with self._workflow_lock:
            self._mutation_journal_identities[request_fingerprint] = identity
            self._mutation_journal_identities.move_to_end(request_fingerprint)
            while len(self._mutation_journal_identities) > _MAX_MUTATION_OUTCOMES:
                self._mutation_journal_identities.popitem(last=False)
        self._persist_context()

    def cached_mutation(
        self,
        request_fingerprint: str,
    ) -> DomainOutcome[object] | None:
        with self._workflow_lock:
            outcome = self._mutation_outcomes.get(request_fingerprint)
            if outcome is not None:
                self._mutation_outcomes.move_to_end(request_fingerprint)
            return outcome

    def store_mutation(
        self,
        request_fingerprint: str,
        outcome: DomainOutcome[object],
    ) -> DomainOutcome[object]:
        with self._workflow_lock:
            self._mutation_outcomes[request_fingerprint] = outcome
            self._mutation_outcomes.move_to_end(request_fingerprint)
            while len(self._mutation_outcomes) > _MAX_MUTATION_OUTCOMES:
                self._mutation_outcomes.popitem(last=False)
            return outcome

    @contextmanager
    def domain_admission(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: OperationContext,
    ):
        self.bind_intent(tool_name, arguments)
        assert self.orchestration is not None
        if tool_name in _MUTATION_TOOLS:
            bindings = (self._selected_mutation_target(arguments),)
        elif (
            tool_name == "debug_run"
            and len(self.orchestration.intent.targets) > 1
            and not self._explicit_target_id(arguments)
        ):
            bindings = self.orchestration.intent.targets
        else:
            bindings = (self._selected_target(arguments),)
        ordered = sorted(
            bindings,
            key=lambda binding: binding.target_id,
        )
        with self._lock:
            coordinators = [
                self._target_admissions.setdefault(
                    binding.target_id,
                    TargetLeaseCoordinator(),
                )
                for binding in ordered
            ]
        with ExitStack() as stack:
            for coordinator in coordinators:
                manager = (
                    coordinator.mutation(context)
                    if tool_name in _MUTATION_TOOLS
                    else coordinator.read(context)
                )
                stack.enter_context(manager)
            yield

    def resource_for(self, tool_name: str) -> tuple[object, object]:
        backend = self.tool_backends[tool_name]
        key = id(backend)
        with self._lock:
            resource = self._resources.get(key)
            if resource is None:
                resource = backend.open_task(self.task_id)
                self._resources[key] = resource
            self._resource_tools[tool_name] = resource
            return backend, resource

    def maintain(self) -> int:
        total = 0
        seen: set[int] = set()
        with self._lock:
            resources = list(self._resource_tools.items())
        for tool_name, resource in resources:
            backend = self.tool_backends[tool_name]
            key = id(backend)
            if key in seen:
                continue
            seen.add(key)
            total += int(backend.maintain_task(resource) or 0)
        return total

    @staticmethod
    def _workflow_summary(
        request_fingerprint: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        intent = result.get("intent")
        public_intent = (
            {
                key: intent.get(key)
                for key in (
                    "original_intent",
                    "delivery_strategy",
                    "fingerprint",
                )
            }
            if isinstance(intent, Mapping)
            else None
        )
        return {
            "request_fingerprint": request_fingerprint,
            "completed": bool(result.get("completed", False)),
            "partial": bool(result.get("partial", False)),
            "next_action": str(result.get("next_action", "")),
            "intent": public_intent,
            "phase_states": (
                dict(result["phase_states"])
                if isinstance(result.get("phase_states"), Mapping)
                else {}
            ),
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            resources = dict(self._resource_tools)
            orchestration = self.orchestration
            orchestration_history = tuple(self._orchestration_history)
            target_admissions = dict(self._target_admissions)
            credential_parse_count = self._credential_parse_count
            persistence_error = self._persistence_error
            recovered_context = self._recovered_context
        with self._workflow_lock:
            workflow_summaries = [
                {
                    **summary,
                    "intent": (
                        dict(summary["intent"])
                        if isinstance(summary.get("intent"), Mapping)
                        else None
                    ),
                    "phase_states": (
                        dict(summary["phase_states"])
                        if isinstance(summary.get("phase_states"), Mapping)
                        else {}
                    ),
                }
                for summary in self._workflow_summaries
            ]
            cached_mutation_count = len(self._mutation_outcomes)
            mutation_journal_identity_count = len(
                self._mutation_journal_identities
            )
        return {
            "task_id": self.task_id,
            "credential_parse_count": credential_parse_count,
            "orchestration": (
                orchestration.to_public_dict()
                if orchestration is not None
                else None
            ),
            "orchestration_history": [
                context.to_public_dict()
                for context in orchestration_history
            ],
            "automatic_workflow": (
                dict(workflow_summaries[-1])
                if workflow_summaries
                else None
            ),
            "automatic_workflows": workflow_summaries,
            "workflow_history": {
                "entry_count": len(workflow_summaries),
                "max_entries": _MAX_WORKFLOW_SUMMARIES,
            },
            "workflow_cache": {
                "enabled": False,
                "entry_count": 0,
                "bytes": 0,
                "max_entries": 0,
                "max_result_bytes": 0,
                "max_bytes": 0,
            },
            "cached_mutation_count": cached_mutation_count,
            "task_context": {
                "persistent": self._state_store is not None,
                "recovered": recovered_context,
                "last_error": persistence_error,
                "mutation_journal_identity_count": (
                    mutation_journal_identity_count
                ),
                "connections_recovered": False,
                "evidence_results_recovered": False,
            },
            "target_admission": {
                target_id: coordinator.to_public_dict()
                for target_id, coordinator in target_admissions.items()
            },
            "domain_resources": {
                tool_name: self.tool_backends[tool_name].task_status(resource)
                for tool_name, resource in resources.items()
            },
        }

    def close(self) -> None:
        seen: set[int] = set()
        with self._lock:
            resources = list(self._resource_tools.items())
            self._resource_tools.clear()
            self._resources.clear()
            self._credential_values = None
            self._workflow_summaries.clear()
            self._mutation_outcomes.clear()
            self._mutation_journal_identities.clear()
            self._target_arguments.clear()
        for tool_name, resource in resources:
            backend = self.tool_backends[tool_name]
            key = id(backend)
            if key in seen:
                continue
            seen.add(key)
            backend.close_task(resource)


class OrchestratedMcpBackend:
    """Share typed intent across domain backends while isolating their resources."""

    def __init__(
        self,
        tool_backends: Mapping[str, object],
        *,
        state_store: TaskContextStore | None = None,
    ) -> None:
        if not tool_backends:
            raise ValueError("at least one domain tool backend is required")
        unknown = set(tool_backends) - set(_TOOL_DOMAINS)
        if unknown:
            raise ValueError(
                "unsupported orchestrated MCP tools: " + ", ".join(sorted(unknown))
            )
        for tool_name, backend in tool_backends.items():
            if not callable(getattr(backend, tool_name, None)):
                raise TypeError(f"backend for {tool_name} does not implement that tool")
        self.tool_backends = dict(tool_backends)
        self.state_store = state_store

    def open_task(self, task_id: str) -> _OrchestratedMcpTask:
        return _OrchestratedMcpTask(
            task_id,
            self.tool_backends,
            state_store=self.state_store,
        )

    def forget_task(self, task_id: str) -> bool:
        return (
            self.state_store.delete(task_id)
            if self.state_store is not None
            else False
        )

    @staticmethod
    def prepare_task_completion(task: _OrchestratedMcpTask) -> None:
        task.seal_persistence()

    def persistent_status(self) -> dict[str, object]:
        if self.state_store is None:
            return {"enabled": False}
        return self.state_store.status()

    @staticmethod
    def close_task(task: _OrchestratedMcpTask) -> None:
        task.close()

    @staticmethod
    def maintain_task(task: _OrchestratedMcpTask) -> int:
        return task.maintain()

    @staticmethod
    def task_status(task: _OrchestratedMcpTask) -> dict[str, object]:
        return task.status()

    @staticmethod
    def _workflow_sections(arguments: Mapping[str, object]) -> Mapping[str, object]:
        raw = arguments.get(_WORKFLOW_ARGUMENT, {})
        if not isinstance(raw, Mapping):
            raise TypeError("workflow must be an object")
        return raw

    def _should_orchestrate(
        self,
        task: _OrchestratedMcpTask,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> bool:
        task.bind_intent(tool_name, arguments)
        if arguments.get("_context_authoritative") is True:
            return False
        if arguments.get(CONTEXT_WORKFLOW_STEP_ARGUMENT) is True:
            return False
        assert task.orchestration is not None
        steps = task.orchestration.intent.steps
        if len(steps) <= 1 or _DOMAIN_TO_TOOL.get(steps[0].domain) != tool_name:
            return False
        required_tools = {
            _DOMAIN_TO_TOOL[step.domain]
            for step in steps
            if step.domain in _DOMAIN_TO_TOOL
        }
        if not required_tools.issubset(self.tool_backends):
            return False
        if (
            task.orchestration.intent.original_intent
            is TaskIntentKind.DIAGNOSE_AND_FIX
        ):
            sections = self._workflow_sections(arguments)
            for step in steps:
                if step.domain in _EXTERNAL_WORKFLOW_DOMAINS and not isinstance(
                    sections.get(step.domain), Mapping
                ):
                    return False
            if any(step.domain == "live_patch" for step in steps) and not isinstance(
                sections.get("live_patch"), Mapping
            ):
                return False
        return True

    @staticmethod
    def _domain_context(
        parent: OperationContext,
        execution: DomainExecutionContext,
    ) -> OperationContext:
        return parent.derive(execution.operation_id)

    @staticmethod
    def _developer_outcome(
        raw: object,
    ) -> DomainOutcome[object]:
        if not isinstance(raw, Mapping):
            raise ValueError("diagnose-and-fix requires workflow.developer")

        def string_tuple(name: str) -> tuple[str, ...]:
            values = raw.get(name, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise TypeError(f"workflow.developer.{name} must be an array of strings")
            return tuple(values)

        edit = DeveloperEditIntent(
            component_roots=string_tuple("component_roots"),
            authored_files=string_tuple("authored_files"),
            change_summary=str(raw.get("change_summary", "")),
            runtime_artifact=str(raw.get("runtime_artifact", "")),
            restart_scope=str(raw.get("restart_scope", "none")),
            verification_checks=string_tuple("verification_checks"),
        )
        return DomainOutcome.succeeded(
            {"edit_handoff": "provided"},
            edit_intent=edit,
        )

    @staticmethod
    def _provided_domain_outcome(
        domain: str,
        raw: object,
    ) -> DomainOutcome[object]:
        if not isinstance(raw, Mapping):
            raise ValueError(f"workflow.{domain} must be an object")
        value = dict(raw)
        raw_evidence = value.get("evidence_ids", [])
        evidence_ids = (
            tuple(
                item
                for item in raw_evidence
                if isinstance(item, str) and item
            )
            if isinstance(raw_evidence, list)
            else ()
        )
        return DomainOutcome.succeeded(value, evidence_ids=evidence_ids)

    @staticmethod
    def _runtime_status(value: Mapping[str, object]) -> Mapping[str, object]:
        containers: list[Mapping[str, object]] = [value]
        result = value.get("result")
        if isinstance(result, Mapping):
            containers.append(result)
        for container in containers:
            runtime = container.get("runtime")
            if not isinstance(runtime, Mapping):
                continue
            status = runtime.get("status")
            if isinstance(status, Mapping):
                return status
        return {}

    @classmethod
    def _evidence_ids(cls, value: Mapping[str, object]) -> tuple[str, ...]:
        raw = value.get("evidence_ids", [])
        evidence_ids = [
            item for item in raw if isinstance(item, str) and item
        ] if isinstance(raw, list) else []
        status = cls._runtime_status(value)
        ledger = status.get("evidence_ledger")
        if isinstance(ledger, Mapping):
            records = ledger.get("records", [])
            if isinstance(records, list):
                evidence_ids.extend(
                    str(record["evidence_id"])
                    for record in records
                    if isinstance(record, Mapping)
                    and isinstance(record.get("evidence_id"), str)
                    and record["evidence_id"]
                )
        return tuple(dict.fromkeys(evidence_ids))

    @classmethod
    def _observed_target_epoch(
        cls,
        value: Mapping[str, object],
        target: TaskTargetBinding,
    ) -> int:
        observed = value.get("observed_target_epochs")
        if isinstance(observed, Mapping) and target.target_id in observed:
            epoch = observed[target.target_id]
        elif "target_epoch" in value:
            epoch = value.get("target_epoch")
        else:
            epoch = None
            status = cls._runtime_status(value)
            targets = status.get("targets", [])
            if isinstance(targets, list):
                for candidate in targets:
                    if not isinstance(candidate, Mapping):
                        continue
                    target_value = candidate.get("target")
                    if not isinstance(target_value, Mapping):
                        continue
                    fingerprint = target_value.get("fingerprint")
                    host = target_value.get("host")
                    if fingerprint != target.target.fingerprint and (
                        not isinstance(host, str)
                        or host.strip().lower() != target.target.host
                    ):
                        continue
                    epochs = candidate.get("epochs")
                    if isinstance(epochs, Mapping):
                        epoch = epochs.get("target_epoch")
                    break
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError(
                "fresh verification backend must report a non-negative target epoch"
            )
        return epoch

    @staticmethod
    def _domain_outcome(
        task: _OrchestratedMcpTask,
        execution: DomainExecutionContext,
        arguments: Mapping[str, object],
        value: object,
    ) -> DomainOutcome[object]:
        if execution.phase == "mutation":
            mutation = MutationDomainResult.from_backend(value)
            target = task._selected_mutation_target(arguments)
            return DomainOutcome.modified(
                mutation.value,
                evidence_ids=mutation.evidence_ids,
                modified_target_epochs={target.target_id: mutation.epoch_after},
                operation_id=execution.operation_id,
            )
        if not isinstance(value, Mapping):
            raise TypeError("MCP domain backend must return an object")
        public_value = dict(value)
        evidence_ids = OrchestratedMcpBackend._evidence_ids(public_value)
        if execution.phase == "fresh_verification":
            target = task._selected_target(arguments)
            observed_epoch = OrchestratedMcpBackend._observed_target_epoch(
                public_value,
                target,
            )
            minimum_epoch = int(
                execution.minimum_target_epochs.get(target.target_id, 0)
            )
            if observed_epoch < minimum_epoch:
                raise ValueError(
                    "fresh verification reported stale target epoch "
                    f"{observed_epoch} for {target.target_id}; "
                    f"requires at least {minimum_epoch}"
                )
            return DomainOutcome.verified(
                public_value,
                evidence_ids=evidence_ids,
                observed_target_epochs={target.target_id: observed_epoch},
            )
        return DomainOutcome.succeeded(public_value, evidence_ids=evidence_ids)

    @staticmethod
    def _step_arguments(
        root_arguments: Mapping[str, object],
        sections: Mapping[str, object],
        execution: DomainExecutionContext,
    ) -> dict[str, object]:
        first_step = execution.intent.steps[0]
        if execution.domain == first_step.domain and execution.phase == first_step.phase:
            selected = {
                key: value
                for key, value in root_arguments.items()
                if key != _WORKFLOW_ARGUMENT
            }
        else:
            section_name = (
                "verification"
                if execution.phase == "fresh_verification"
                else execution.domain
            )
            raw = sections.get(section_name, {})
            if not isinstance(raw, Mapping):
                raise TypeError(f"workflow.{section_name} must be an object")
            selected = {
                key: value
                for key, value in raw.items()
                if key not in _WORKFLOW_SECTION_PROTECTED_ARGUMENTS
                and not str(key).startswith("_")
            }
        if execution.domain == "live_patch" and execution.edit_intent is not None:
            selected.setdefault("local_path", execution.edit_intent.runtime_artifact)
            selected.setdefault("restart_scope", execution.edit_intent.restart_scope)
            selected.setdefault(
                "verification_checks",
                list(execution.edit_intent.verification_checks),
            )
        if execution.domain == "upgrade":
            build_result = next(
                (
                    previous.value
                    for previous in reversed(execution.previous)
                    if previous.domain == "build"
                    and previous.phase == "package"
                    and isinstance(previous.value, Mapping)
                ),
                None,
            )
            if isinstance(build_result, Mapping):
                for name in (
                    "artifact_path",
                    "artifact_sha256",
                    "product_version",
                ):
                    if name in build_result:
                        selected.setdefault(name, build_result[name])
        if execution.phase == "fresh_verification":
            if len(execution.minimum_target_epochs) != 1:
                raise ValueError(
                    "fresh verification requires exactly one modified target"
                )
            for name in ("ip", "targets", "role", "target_role", "target_id"):
                selected.pop(name, None)
            selected["target_id"] = next(iter(execution.minimum_target_epochs))
        else:
            target_id = root_arguments.get("target_id")
            if isinstance(target_id, str) and target_id.strip():
                selected.setdefault("target_id", target_id.strip())
        if execution.phase == "fresh_verification":
            selected.setdefault("profile", "freshness")
            if execution.minimum_target_epochs:
                selected["_minimum_target_epoch"] = max(
                    int(epoch)
                    for epoch in execution.minimum_target_epochs.values()
                )
        return selected

    @staticmethod
    def _mutation_request_fingerprint(
        task: _OrchestratedMcpTask,
        domain: str,
        arguments: Mapping[str, object],
    ) -> str:
        target = task._selected_mutation_target(arguments)
        ignored = _ORCHESTRATION_ARGUMENTS | _SECRET_ARGUMENTS | {
            _WORKFLOW_ARGUMENT,
            "deadline",
            "ssh_user",
            "ssh_user_env",
            "ssh_password_env",
            "ssh_identity_file",
            "telnet_user",
            "telnet_user_env",
            "telnet_password_env",
            "redfish_user",
            "redfish_user_env",
            "redfish_password_env",
        }
        operation = {
            key: value
            for key, value in arguments.items()
            if key not in ignored and not key.startswith("_")
        }
        if domain == "live_patch":
            local_path = operation.get("local_path")
            if isinstance(local_path, str) and local_path.strip():
                path = Path(local_path).expanduser()
                if path.is_file():
                    path = path.resolve()
                    operation["local_path"] = str(path)
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    operation["local_sha256"] = digest.hexdigest()
        return _fingerprint(
            {
                "domain": domain,
                "phase": "mutation",
                "target": target.target.fingerprint,
                "operation": operation,
            }
        )

    @staticmethod
    def _mutation_operation_id(domain: str, request_fingerprint: str) -> str:
        safe_domain = str(domain).strip().lower().replace("_", "-")
        return f"op-mutation-{request_fingerprint[:40]}-{safe_domain}"

    def _run_workflow(
        self,
        task: _OrchestratedMcpTask,
        entry_tool: str,
        arguments: Mapping[str, object],
        context: OperationContext,
    ) -> dict[str, object]:
        assert task.orchestration is not None
        request_fingerprint = _fingerprint(
            {
                "entry_tool": entry_tool,
                "intent": task.orchestration.intent.fingerprint,
                "arguments": dict(arguments),
            }
        )
        with task._workflow_lock:
            sections = self._workflow_sections(arguments)
            workflow_context = TaskOrchestrationContext(
                task_id=f"{task.task_id}:{request_fingerprint[:16]}",
                intent=task.orchestration.intent,
            )
            handlers = {}
            for step in workflow_context.intent.steps:
                if step.domain == "developer":
                    handlers[step.key] = lambda _execution, raw=sections.get(
                        "developer"
                    ): self._developer_outcome(raw)
                    continue
                if step.domain == "build":
                    handlers[step.key] = lambda _execution, raw=sections.get(
                        "build"
                    ): self._provided_domain_outcome("build", raw)
                    continue
                tool_name = _DOMAIN_TO_TOOL.get(step.domain)
                if tool_name is None or tool_name not in self.tool_backends:
                    continue

                def handler(
                    execution: DomainExecutionContext,
                    *,
                    selected_tool: str = tool_name,
                ) -> DomainOutcome[object]:
                    step_arguments = self._step_arguments(
                        arguments,
                        sections,
                        execution,
                    )
                    mutation_fingerprint = ""
                    domain_execution = execution
                    if execution.phase == "mutation":
                        mutation_fingerprint = self._mutation_request_fingerprint(
                            task,
                            execution.domain,
                            step_arguments,
                        )
                        domain_execution = replace(
                            execution,
                            operation_id=self._mutation_operation_id(
                                execution.domain,
                                mutation_fingerprint,
                            ),
                        )
                        task.record_mutation_identity(
                            execution.domain,
                            mutation_fingerprint,
                            domain_execution.operation_id,
                        )
                        cached_mutation = task.cached_mutation(
                            mutation_fingerprint
                        )
                        if cached_mutation is not None:
                            return cached_mutation
                    backend, resource = task.resource_for(selected_tool)
                    callback = getattr(backend, selected_tool)
                    child_context = self._domain_context(context, domain_execution)
                    with task.domain_admission(
                        selected_tool,
                        step_arguments,
                        child_context,
                    ):
                        value = callback(
                            resource,
                            task.arguments_for(selected_tool, step_arguments),
                            child_context,
                        )
                    outcome = self._domain_outcome(
                        task,
                        domain_execution,
                        step_arguments,
                        value,
                    )
                    if mutation_fingerprint:
                        task.store_mutation(mutation_fingerprint, outcome)
                    return outcome

                handlers[step.key] = handler
            result = TaskWorkflowOrchestrator(workflow_context).run(handlers)
            public_result = result.to_public_dict()
            task.record_workflow_summary(
                request_fingerprint,
                public_result,
            )
            return public_result

    def __getattr__(self, name: str):
        if name not in self.tool_backends:
            raise AttributeError(name)

        def call(task: _OrchestratedMcpTask, arguments, context):
            if self._should_orchestrate(task, name, arguments):
                return self._run_workflow(task, name, arguments, context)
            backend, resource = task.resource_for(name)
            callback = getattr(backend, name)
            domain_context = context
            if name in _MUTATION_TOOLS:
                domain = _TOOL_DOMAINS[name]
                mutation_fingerprint = self._mutation_request_fingerprint(
                    task,
                    domain,
                    arguments,
                )
                mutation_operation_id = self._mutation_operation_id(
                    domain,
                    mutation_fingerprint,
                )
                derive = getattr(context, "derive", None)
                if callable(derive):
                    domain_context = derive(mutation_operation_id)
                task.record_mutation_identity(
                    domain,
                    mutation_fingerprint,
                    str(
                        getattr(
                            domain_context,
                            "operation_id",
                            mutation_operation_id,
                        )
                    ),
                )
            with task.domain_admission(name, arguments, domain_context):
                projected_arguments = task.arguments_for(name, arguments)
                projected_arguments.pop(CONTEXT_WORKFLOW_STEP_ARGUMENT, None)
                return callback(
                    resource,
                    projected_arguments,
                    domain_context,
                )

        return call

class RuntimeMcpService:
    """Bind the three domain tools to a reusable task lifecycle registry."""

    def __init__(
        self,
        backend: DebugMcpBackend[TaskT],
        *,
        context_repository: RuntimeRepository | None = None,
        blob_repository: BlobRepository | None = None,
        context_runtime: ContextRuntime | None = None,
        envelope_max_bytes: int = AGENT_ENVELOPE_MAX_BYTES,
        context_max_cached_projections: int = 64,
        context_max_cached_projection_bytes: int = 8 * 1024 * 1024,
        context_retention_seconds: float = 7 * 24 * 60 * 60,
        context_storage_soft_limit_bytes: int = 1024 * 1024 * 1024,
        context_maintenance_interval_seconds: float = 60,
        context_mode: str = "authoritative",
        **registry_options: object,
    ) -> None:
        selected_context_mode = str(context_mode).strip().lower()
        if selected_context_mode not in {"authoritative", "shadow"}:
            raise ValueError("context_mode must be authoritative or shadow")
        self.backend = backend
        self.context_mode = selected_context_mode
        self.registry: TaskRunRegistry[TaskT] = TaskRunRegistry(
            factory=backend.open_task,
            closer=self._close_task_resource,
            status_reader=backend.task_status,
            maintenance=backend.maintain_task,
            completion_preparer=getattr(
                backend,
                "prepare_task_completion",
                None,
            ),
            **registry_options,
        )
        definitions = self._build_tool_definitions()
        definition_names = tuple(str(definition["name"]) for definition in definitions)
        definition_name_set = set(definition_names)
        binding_names = tuple(
            binding.name
            for binding in _OPERATION_BINDINGS
            if binding.name in definition_name_set
        )
        if definition_names != binding_names or definition_name_set - set(
            _OPERATION_BINDING_BY_NAME
        ):
            raise RuntimeError(
                "operation definitions and binding metadata are out of sync"
            )
        self.catalog = OperationCatalog(
            (
                OperationDescriptor.from_tool_definition(
                    definition,
                    lifecycle=_OPERATION_BINDING_BY_NAME[
                        str(definition["name"])
                    ].lifecycle,
                    handler_name=_OPERATION_BINDING_BY_NAME[
                        str(definition["name"])
                    ].handler_name,
                    mutation=_OPERATION_BINDING_BY_NAME[
                        str(definition["name"])
                    ].mutation,
                )
                for definition in definitions
            ),
            backend=backend,
        )
        capability_descriptors: list[CapabilityDescriptor] = []
        for operation_descriptor in self.catalog.descriptors():
            contract = _CAPABILITY_CONTRACTS.get(operation_descriptor.name)
            if contract is None:
                continue
            capability, owner_skill, timeout_seconds, evidence_types = contract
            capability_descriptors.append(
                CapabilityDescriptor(
                    operation=operation_descriptor.name,
                    capability=capability,
                    owner_skill=owner_skill,
                    input_schema=operation_descriptor.input_schema,
                    output_schema={"type": "object", "additionalProperties": True},
                    timeout_seconds=timeout_seconds,
                    evidence_types=evidence_types,
                    mutation=operation_descriptor.mutation,
                )
            )
        self.capability_registry = CapabilityRegistry(capability_descriptors)
        self.runtime_sdk = RuntimeSDK(self.capability_registry)
        self.context_runtime = context_runtime or ContextRuntime(
            self.catalog,
            repository=context_repository,
            blob_repository=blob_repository,
            envelope_max_bytes=envelope_max_bytes,
            max_cached_projections=context_max_cached_projections,
            max_cached_projection_bytes=context_max_cached_projection_bytes,
            retention_seconds=context_retention_seconds,
            storage_soft_limit_bytes=context_storage_soft_limit_bytes,
        )
        if context_maintenance_interval_seconds < 0:
            raise ValueError("context maintenance interval must not be negative")
        self._context_maintenance_interval_seconds = float(
            context_maintenance_interval_seconds
        )
        self._context_maintenance_lock = threading.Lock()
        self._last_context_maintenance_at = 0.0
        self._context_maintenance_attempts = 0
        self._context_maintenance_failures = 0
        self._context_maintenance_last_error = ""
        self._context_maintenance_last_result: dict[str, object] = {}

    def _close_task_resource(self, task: TaskT) -> None:
        self.backend.close_task(task)

    def _maintain_context_if_due(self) -> None:
        now = time.monotonic()
        if (
            self._context_maintenance_interval_seconds > 0
            and now - self._last_context_maintenance_at
            < self._context_maintenance_interval_seconds
        ):
            return
        if not self._context_maintenance_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if (
                self._context_maintenance_interval_seconds > 0
                and now - self._last_context_maintenance_at
                < self._context_maintenance_interval_seconds
            ):
                return
            try:
                maintenance_result = self.context_runtime.maintain()
                self._context_maintenance_last_result = (
                    dict(maintenance_result)
                    if isinstance(maintenance_result, Mapping)
                    else {}
                )
                self._context_maintenance_last_error = ""
            except Exception as exc:
                self._context_maintenance_failures += 1
                self._context_maintenance_last_error = (
                    f"{type(exc).__name__}: {exc}"
                )[:2048]
            self._context_maintenance_attempts += 1
            self._last_context_maintenance_at = now
        finally:
            self._context_maintenance_lock.release()

    def _build_tool_definitions(self) -> list[dict[str, object]]:
        common_target = {
            "type": "string",
            "minLength": 1,
            "description": (
                "BMC host name or address. Required only on the first domain "
                "call when the task has no bound target context."
            ),
        }
        orchestration_properties = {
            "intent": {
                "type": "string",
                "enum": TaskIntentKind.public_values(),
                "description": "Original task intent; parsed once on the first domain call.",
            },
            "final_purpose": {
                "type": "string",
                "minLength": 1,
                "description": "Final task purpose retained across internal handoffs.",
            },
            "delivery_strategy": {
                "type": "string",
                "enum": DeliveryStrategy.public_values(),
                "description": (
                    "Delivery path for diagnose-and-fix. When omitted, infer it "
                    "from workflow sections or the selected mutation tool; otherwise "
                    "remain source-only without asking the user to repeat intent."
                ),
            },
            "authorized_exceptions": {
                "type": "object",
                "description": (
                    "Task-level authorization for narrowly scoped mutation "
                    "exceptions. A mutation flag cannot authorize itself."
                ),
                "properties": {
                    "force_path": {"type": "boolean", "default": False},
                    "no_backup": {"type": "boolean", "default": False},
                    "no_remount": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            "target_id": {
                "type": "string",
                "description": "Internal target selector for a previously bound multi-target task.",
            },
            "target_role": {
                "type": "string",
                "enum": ["reference", "candidate", "symmetric"],
            },
            "workflow": {
                "type": "object",
                "description": (
                    "Optional typed domain arguments for automatic multi-Skill "
                    "continuation without caller-managed operation or handoff IDs."
                ),
                "additionalProperties": {"type": "object"},
            },
        }
        deadline = {
            "type": "number",
            "exclusiveMinimum": 0,
            "default": 600,
            "description": "Bounded end-to-end budget in seconds.",
        }
        definitions: list[dict[str, object]] = []
        if callable(getattr(self.backend, "debug_run", None)):
            definitions.append({
                "name": "debug_run",
                "description": (
                    "Run the typed openUBMC Debug workflow for one task target. "
                    "The task TargetRun reuses epoch-valid transport/capability state "
                    "while each call recollects live evidence."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **orchestration_properties,
                        "ip": common_target,
                        "deadline": deadline,
                        "mdb_queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Reviewed read-only mdbctl queries collected inside "
                                "the same task-scoped Debug lease."
                            ),
                        },
                        "mdb_expand_classes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "MDB classes whose current objects are discovered "
                                "and read inside the same Debug lease."
                            ),
                        },
                        "mdb_concurrency": {
                            "oneOf": [
                                {
                                    "type": "string",
                                    "pattern": "^(auto|unbounded|[1-9][0-9]*)$",
                                },
                                {"type": "integer", "minimum": 1},
                            ],
                            "default": "auto",
                        },
                        "mdb_only": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Collect only reviewed MDB queries and lightweight "
                                "preflight freshness."
                            ),
                        },
                        "reference_role": {
                            "type": "string",
                            "enum": ["reference", "candidate", "symmetric"],
                        },
                        "targets": {
                            "type": "array",
                            "minItems": 2,
                            "items": {
                                "type": "object",
                                "required": ["ip"],
                                "properties": {
                                    "ip": common_target,
                                    "role": {
                                        "enum": ["reference", "candidate"]
                                    },
                                    "target_id": {"type": "string"},
                                },
                                "additionalProperties": True,
                            },
                        },
                        "concurrency": {
                            "oneOf": [
                                {
                                    "type": "string",
                                    "pattern": "^(auto|unbounded|[1-9][0-9]*)$"
                                },
                                {"type": "integer", "minimum": 1}
                            ],
                            "default": "auto"
                        },
                    },
                    "additionalProperties": True,
                },
            })
        if callable(getattr(self.backend, "debug_collect", None)):
            definitions.append({
                "name": "debug_collect",
                "description": (
                    "Collect a bounded openUBMC Debug evidence profile with "
                    "task-scoped, epoch-valid capability reuse and fresh evidence reads. "
                    "The mdb and object-alarm profiles are fast current snapshots; "
                    "debug_run retains the full freshness workflow."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **orchestration_properties,
                        "ip": common_target,
                        "deadline": deadline,
                        "mdb_queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Reviewed read-only mdbctl queries collected inside "
                                "the same task-scoped Debug lease."
                            ),
                        },
                        "mdb_expand_classes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "MDB classes whose current objects are discovered "
                                "and read inside the same Debug lease."
                            ),
                        },
                        "mdb_concurrency": {
                            "oneOf": [
                                {
                                    "type": "string",
                                    "pattern": "^(auto|unbounded|[1-9][0-9]*)$",
                                },
                                {"type": "integer", "minimum": 1},
                            ],
                            "default": "auto",
                        },
                        "mdb_only": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Collect only reviewed MDB queries and lightweight "
                                "preflight freshness."
                            ),
                        },
                        "profile": {
                            "type": "string",
                            "enum": [
                                "standard",
                                "freshness",
                                "mdb",
                                "object-alarm",
                                "log-file",
                            ],
                            "default": "standard",
                            "description": (
                                "Select the evidence shape. mdb collects only current "
                                "MDB evidence; object-alarm collects current object and "
                                "alarm evidence. Both skip Telnet, source correlation, "
                                "and the end freshness pass."
                            ),
                        },
                    },
                    "additionalProperties": True,
                },
            })
        if callable(getattr(self.backend, "log_bundle_collect", None)):
            definitions.append(
                {
                    "name": "log_bundle_collect",
                    "description": (
                        "Collect an openUBMC one-click log bundle through the "
                        "Log Analyzer Redfish-primary workflow."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            **orchestration_properties,
                            "ip": common_target,
                            "deadline": deadline,
                            "transport": {
                                "type": "string",
                                "enum": ["auto", "redfish", "ssh"],
                                "default": "auto",
                            },
                            "problem": {"type": "string"},
                            "extract": {"type": "boolean", "default": True},
                        },
                        "additionalProperties": True,
                    },
                }
            )
        if callable(getattr(self.backend, "live_patch_run", None)):
            definitions.append(
                {
                    "name": "live_patch_run",
                    "description": (
                        "Apply or roll back one typed openUBMC Live Patch mutation "
                        "and retain the task target for fresh verification."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            **orchestration_properties,
                            "ip": common_target,
                            "deadline": deadline,
                            "action": {
                                "type": "string",
                                "enum": ["apply", "rollback"],
                                "default": "apply",
                            },
                            "local_path": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Absolute local runtime artifact path.",
                            },
                            "backup_path": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Absolute remote backup path for rollback.",
                            },
                            "remove_created": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "Remove a checksum-matched target that the "
                                    "corresponding patch created from absence."
                                ),
                            },
                            "force_path": {
                                "type": "boolean",
                                "default": False,
                            },
                            "no_backup": {
                                "type": "boolean",
                                "default": False,
                            },
                            "no_remount": {
                                "type": "boolean",
                                "default": False,
                            },
                            "expected_current_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-fA-F]{64}$",
                            },
                            "remote_path": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Absolute authored target file path.",
                            },
                            "restart_scope": {
                                "type": "string",
                                "enum": ["none", "skynet"],
                                "default": "none",
                            },
                            "verification_checks": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "default": [],
                            },
                        },
                        "required": ["remote_path"],
                        "allOf": [
                            {
                                "if": {
                                    "properties": {
                                        "action": {"const": "rollback"}
                                    },
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
                        "additionalProperties": True,
                    },
                }
            )
        if callable(getattr(self.backend, "upgrade_run", None)):
            definitions.append(
                {
                    "name": "upgrade_run",
                    "description": (
                        "Install one identified openUBMC upgrade artifact and "
                        "retain the task target for fresh verification."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            **orchestration_properties,
                            "ip": common_target,
                            "deadline": deadline,
                            "artifact_path": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Absolute local HPM artifact path.",
                            },
                            "artifact_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-fA-F]{64}$",
                            },
                            "product_version": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "upload_timeout": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "default": 600,
                                "description": (
                                    "Per-request timeout for HPM byte upload; "
                                    "bounded by the task deadline."
                                ),
                            },
                            "transport": {
                                "type": "string",
                                "enum": ["redfish"],
                                "default": "redfish",
                            },
                            "allow_insecure_tls": {
                                "type": "boolean",
                                "default": True,
                            },
                        },
                        "required": [
                            "artifact_path",
                            "artifact_sha256",
                            "product_version",
                        ],
                        "additionalProperties": True,
                    },
                }
            )
        definitions.extend(
            [
                {
                    "name": "case_read",
                    "description": (
                        "Read the bounded recoverable openUBMC Case projection without "
                        "advancing the workflow."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": ["case_id"],
                        "properties": {"case_id": {"type": "string", "minLength": 1}},
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "evidence_read",
                    "description": (
                        "Read one bounded verified slice of evidence referenced by a Case."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": ["case_id", "evidence_id"],
                        "properties": {
                            "case_id": {"type": "string", "minLength": 1},
                            "evidence_id": {"type": "string", "minLength": 1},
                            "offset": {"type": "integer", "minimum": 0, "default": 0},
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1048576,
                                "default": 65536,
                            },
                            "target_id": {"type": "string"},
                            "generation": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "case_close",
                    "description": "Seal one resolved Case while retaining readable history.",
                    "inputSchema": {
                        "type": "object",
                        "required": ["case_id", "expected_revision"],
                        "properties": {
                            "case_id": {"type": "string", "minLength": 1},
                            "expected_revision": {"type": "integer", "minimum": 0},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "case_forget",
                    "description": (
                        "Immediately forget one resolved Case and its unshared evidence."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": ["case_id"],
                        "properties": {"case_id": {"type": "string", "minLength": 1}},
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "phase_record",
                    "description": (
                        "Record one typed Developer or Build phase outcome in a Case."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": [
                            "case_id",
                            "expected_revision",
                            "idempotency_key",
                            "phase_type",
                            "producer_identity",
                            "status",
                            "source_revision",
                            "summary",
                        ],
                        "properties": {
                            "case_id": {"type": "string", "minLength": 1},
                            "expected_revision": {"type": "integer", "minimum": 0},
                            "idempotency_key": {"type": "string", "minLength": 1},
                            "phase_type": {
                                "type": "string",
                                "enum": ["developer.change", "build.artifact"],
                            },
                            "producer_identity": {"type": "string", "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": ["running", "completed", "failed", "cancelled"],
                            },
                            "source_revision": {"type": "string", "minLength": 1},
                            "summary": {"type": "string", "minLength": 1},
                            "authored_files": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "verification_plan": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "design": {
                                "type": "object",
                                "description": (
                                    "Structured solution design: what_changed, rationale, "
                                    "invariants, tradeoffs, and rollback when applicable."
                                ),
                                "additionalProperties": True,
                            },
                            "validation_results": {
                                "type": "array",
                                "items": {
                                    "oneOf": [
                                        {"type": "string", "minLength": 1},
                                        {"type": "object"},
                                    ]
                                },
                            },
                            "source_delivery": {
                                "type": "string",
                                "enum": [
                                    "local_only",
                                    "committed",
                                    "pushed",
                                    "pull_request",
                                ],
                                "default": "local_only",
                            },
                            "artifact_path": {"type": "string"},
                            "artifact_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-fA-F]{64}$",
                            },
                            "product_version": {"type": "string"},
                            "component_versions": {
                                "type": "array",
                                "items": {
                                    "oneOf": [
                                        {"type": "string", "minLength": 1},
                                        {"type": "object"},
                                    ]
                                },
                            },
                            "build_commands": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "build_logs": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "known_gaps": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "remote_path": {"type": "string"},
                            "restart_scope": {"enum": ["none", "skynet"]},
                        },
                        "additionalProperties": True,
                    },
                },
                {
                    "name": "workflow.advance",
                    "description": (
                        "Advance the current Case automatically to completion or a real "
                        "input, external-phase, budget, cancellation, or mutation blocker."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            **orchestration_properties,
                            "case_id": {"type": "string", "minLength": 1},
                            "ip": common_target,
                            "expected_revision": {"type": "integer", "minimum": 0},
                            "idempotency_key": {"type": "string", "minLength": 1},
                            "max_steps": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 64,
                                "default": 8,
                            },
                            "include_closeout_bundle": {
                                "type": "boolean",
                                "default": True,
                                "description": (
                                    "Return the immutable closeout JSON/Markdown "
                                    "bundle manifest when the Case becomes terminal."
                                ),
                            },
                        },
                        "additionalProperties": True,
                    },
                },
                {
                    "name": "workflow.next",
                    "description": (
                        "Continue an existing Case to the next external gate or "
                        "terminal result using its frozen targets, credentials, "
                        "artifacts, authorization, and acceptance plan."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string", "minLength": 1},
                            "max_steps": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 64,
                                "default": 8,
                            },
                            "include_closeout_bundle": {
                                "type": "boolean",
                                "default": True,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            ]
        )
        definitions.append({
                "name": "runtime_status",
                "description": "Report reusable task state, epochs, leases, and bounded evidence metadata.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            })
        return definitions

    def tool_definitions(self) -> list[dict[str, object]]:
        return self.catalog.tool_definitions()

    @staticmethod
    def _timeout(arguments: Mapping[str, object]) -> float:
        raw = arguments.get("deadline", 600)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("deadline must be a positive number")
        timeout = float(raw)
        if timeout <= 0:
            raise ValueError("deadline must be a positive number")
        return timeout

    @staticmethod
    def _canonicalize_tool_arguments(
        name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        canonical = dict(arguments)
        inferred_defaults = False
        for field in ("intent", "delivery_strategy"):
            value = canonical.get(field)
            if isinstance(value, str):
                canonical[field] = value.strip().lower().replace("_", "-")
        intent = canonical.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            inferred_intent = {
                "live_patch_run": "live-patch",
                "upgrade_run": "upgrade-and-verify",
            }.get(name)
            if inferred_intent:
                canonical["intent"] = inferred_intent
                inferred_defaults = True
        if name == "live_patch_run":
            action = canonical.get("action")
            if isinstance(action, str):
                normalized = action.strip().lower().replace("-", "_")
                if normalized == "live_patch":
                    canonical["action"] = "apply"
            expected_current = canonical.get("expected_current_sha256")
            if isinstance(expected_current, str) and not expected_current.strip():
                canonical.pop("expected_current_sha256", None)
        intent = canonical.get("intent")
        delivery = canonical.get("delivery_strategy")
        if (
            intent == "diagnose-and-fix"
            and (not isinstance(delivery, str) or not delivery.strip())
        ):
            workflow = canonical.get(_WORKFLOW_ARGUMENT)
            inferred = ""
            if isinstance(workflow, Mapping):
                has_build_upgrade = "build" in workflow or "upgrade" in workflow
                has_live_patch = "live_patch" in workflow
                if has_build_upgrade and has_live_patch:
                    raise ValueError(
                        "diagnose-and-fix workflow cannot mix live_patch with build/upgrade"
                    )
                if has_build_upgrade:
                    inferred = "build-upgrade"
                elif has_live_patch:
                    inferred = "live-patch"
                elif "developer" in workflow:
                    inferred = "source-only"
            if not inferred and name == "live_patch_run":
                inferred = "live-patch"
            elif not inferred and name == "upgrade_run":
                inferred = "build-upgrade"
            canonical["delivery_strategy"] = inferred or "source-only"
        elif not isinstance(delivery, str) or not delivery.strip():
            if intent in {"live-patch", "rollback"}:
                canonical["delivery_strategy"] = "live-patch"
            elif intent == "upgrade-and-verify":
                canonical["delivery_strategy"] = "build-upgrade"
        if inferred_defaults:
            canonical["_context_defaults_inferred"] = True
        return canonical

    @staticmethod
    def _validate_boolean_argument_types(
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
    ) -> None:
        properties = descriptor.input_schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return
        for field, schema in properties.items():
            if (
                field in arguments
                and isinstance(schema, Mapping)
                and schema.get("type") == "boolean"
                and not isinstance(arguments[field], bool)
            ):
                raise TypeError(f"{field} must be a boolean")

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        _context_workflow_step: bool = False,
    ) -> dict[str, object]:
        self._maintain_context_if_due()
        if not isinstance(arguments, Mapping):
            raise TypeError("tool arguments must be an object")
        arguments = dict(arguments)
        descriptor = self.catalog.require(name)
        external_context_marker = (
            arguments.get(CONTEXT_WORKFLOW_STEP_ARGUMENT) is True
            and not _context_workflow_step
        )
        if descriptor.handler_name is not None and not external_context_marker:
            arguments = self.context_runtime.restore_domain_arguments(
                task_id,
                name,
                arguments,
            )
        for internal_name in _INTERNAL_TASK_ARGUMENTS:
            arguments.pop(internal_name, None)
        arguments.pop(CONTEXT_WORKFLOW_STEP_ARGUMENT, None)
        arguments = self._canonicalize_tool_arguments(name, arguments)
        self._validate_boolean_argument_types(descriptor, arguments)
        self.catalog.validate_arguments(name, arguments)
        if _context_workflow_step:
            arguments[CONTEXT_WORKFLOW_STEP_ARGUMENT] = True
        if descriptor.lifecycle == "status":
            status = {
                "api_version": RUNTIME_API_VERSION,
                "mcp_protocol_version": MCP_PROTOCOL_VERSION,
                **self.registry.status(),
            }
            persistent_status = getattr(self.backend, "persistent_status", None)
            if callable(persistent_status):
                status["persistent_task_contexts"] = persistent_status()
            status["context_runtime"] = self.context_runtime.status()
            status["capability_registry"] = (
                self.capability_registry.to_public_dict()
            )
            status["context_maintenance"] = {
                "attempts": self._context_maintenance_attempts,
                "failures": self._context_maintenance_failures,
                "last_error": self._context_maintenance_last_error,
                "last_result": dict(self._context_maintenance_last_result),
            }
            status["context_mode"] = self.context_mode
            return self.context_runtime.wrap_status(
                status,
                task_id=task_id,
                operation_id=operation_id,
            )
        if descriptor.handler_name is None:
            if name == "case_read":
                case_id = str(arguments.get("case_id", "")).strip()
                value = self.context_runtime.read_case(case_id)
                return self.context_runtime.wrap_read(
                    value,
                    operation=name,
                    operation_id=operation_id,
                    case_id=case_id,
                )
            if name == "evidence_read":
                case_id = str(arguments.get("case_id", "")).strip()
                value = self.context_runtime.read_evidence(
                    case_id,
                    str(arguments.get("evidence_id", "")).strip(),
                    offset=int(arguments.get("offset", 0)),
                    limit=int(arguments.get("limit", 65536)),
                    target_id=str(arguments.get("target_id", "")),
                    generation=str(arguments.get("generation", "")),
                )
                return self.context_runtime.wrap_read(
                    value,
                    operation=name,
                    operation_id=operation_id,
                    case_id=case_id,
                )
            if name == "case_close":
                case_id = str(arguments.get("case_id", "")).strip()
                value = self.context_runtime.close_case(
                    case_id,
                    expected_revision=int(arguments.get("expected_revision", -1)),
                )
                return self.context_runtime.wrap_read(
                    value,
                    operation=name,
                    operation_id=operation_id,
                    case_id=case_id,
                )
            if name == "case_forget":
                case_id = str(arguments.get("case_id", "")).strip()
                value = self.context_runtime.forget_case(case_id)
                return self.context_runtime.wrap_read(
                    value,
                    operation=name,
                    operation_id=operation_id,
                    case_id="",
                )
            if name == "phase_record":
                return self.context_runtime.phase_record(
                    descriptor,
                    arguments,
                    task_id=task_id,
                    operation_id=operation_id,
                )
            if name == "workflow.advance":
                return self.context_runtime.workflow_advance(
                    descriptor,
                    arguments,
                    task_id=task_id,
                    operation_id=operation_id,
                    domain_invoker=lambda operation, domain_arguments, derived_id: (
                        self.call_tool(
                            operation,
                            domain_arguments,
                            task_id=task_id,
                            operation_id=derived_id,
                            _context_workflow_step=True,
                        )
                    ),
                )
            if name == "workflow.next":
                return self.context_runtime.workflow_next(
                    descriptor,
                    arguments,
                    task_id=task_id,
                    operation_id=operation_id,
                    domain_invoker=lambda operation, domain_arguments, derived_id: (
                        self.call_tool(
                            operation,
                            domain_arguments,
                            task_id=task_id,
                            operation_id=derived_id,
                            _context_workflow_step=True,
                        )
                    ),
                )
            raise RuntimeError(f"context operation is not implemented: {name}")
        callback = getattr(self.backend, str(descriptor.handler_name), None)
        if not callable(callback):
            raise RuntimeError(
                f"operation catalog handler became unavailable: {descriptor.name}"
            )
        capability_descriptor = self.capability_registry.require(name)
        timeout = min(
            self._timeout(arguments), capability_descriptor.timeout_seconds
        )
        context_arguments = arguments
        domain_arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"case_id", "expected_revision", "idempotency_key"}
            and not key.startswith("_workflow_")
            and key != "_context_defaults_inferred"
        }
        if self.context_mode == "authoritative" and not external_context_marker:
            domain_arguments.pop(_WORKFLOW_ARGUMENT, None)
            if isinstance(self.backend, OrchestratedMcpBackend):
                domain_arguments["_context_authoritative"] = True
        if descriptor.mutation or name == "debug_collect":
            minimum_target_epoch = self.context_runtime.minimum_target_epoch(
                task_id,
                arguments,
            )
            domain_arguments.setdefault(
                "_minimum_target_epoch",
                minimum_target_epoch,
            )
            if name == "debug_collect" and minimum_target_epoch > 0:
                context_arguments = dict(arguments)
                context_arguments.setdefault(
                    "_minimum_target_epoch",
                    minimum_target_epoch,
                )
        executor = lambda: dict(
            self.runtime_sdk.execute(
                name,
                context=RuntimeSDKContext(
                    task_id=task_id,
                    operation_id=operation_id,
                    timeout_seconds=timeout,
                    target_id=str(domain_arguments.get("target_id", "")),
                    minimum_target_epoch=int(
                        domain_arguments.get("_minimum_target_epoch", 0)
                    ),
                ),
                arguments=domain_arguments,
                adapter=CallableDomainAdapter(
                    lambda _sdk_context, _sdk_arguments: self.registry.execute(
                        task_id=task_id,
                        operation_id=operation_id,
                        timeout_seconds=timeout,
                        callback=lambda task, context: callback(
                            task, domain_arguments, context
                        ),
                    )
                ),
            ).value
        )
        if self.context_mode == "shadow":
            legacy_value = executor()
            if not isinstance(legacy_value, Mapping):
                raise TypeError("domain operation must return an object")
            return self.context_runtime.shadow_domain(
                descriptor,
                context_arguments,
                task_id=task_id,
                operation_id=operation_id,
                value=legacy_value,
            )
        if external_context_marker and isinstance(
            self.backend, OrchestratedMcpBackend
        ):
            legacy_value = executor()
            if not isinstance(legacy_value, Mapping):
                raise TypeError("domain operation must return an object")
            return dict(legacy_value)
        return self.context_runtime.invoke_domain(
            descriptor,
            context_arguments,
            task_id=task_id,
            operation_id=operation_id,
            executor=executor,
        )

    def cancel_operation(self, task_id: str, operation_id: str) -> bool:
        return self.registry.cancel_operation(task_id, operation_id)

    def complete_task(self, task_id: str) -> bool:
        completed = self.registry.complete(task_id)
        self.context_runtime.repository.unbind_task(task_id)
        return completed

    def error_result(
        self,
        exc: Exception,
        *,
        name: str,
        arguments: Mapping[str, object],
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult:
        return self.context_runtime.error_result(
            exc,
            operation=name,
            arguments=arguments,
            task_id=task_id,
            operation_id=operation_id,
        )

    def close(self) -> None:
        self.registry.close()


class JsonRpcMcpEndpoint:
    """Translate MCP JSON-RPC messages without coupling transport to Debug logic."""

    def __init__(
        self,
        service: RuntimeMcpService,
        *,
        session_task_id: str | None = None,
    ) -> None:
        self.service = service
        self.session_task_id = session_task_id or f"mcp-session-{uuid.uuid4().hex}"

    def task_id_for_params(self, params: object) -> str:
        if not isinstance(params, Mapping):
            return self.session_task_id
        metadata = params.get("_meta")
        if isinstance(metadata, Mapping):
            for key in ("codex/taskId", "taskId", "task_id"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return self.session_task_id

    @staticmethod
    def _response(message_id: object, result: object) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error(
        message_id: object,
        code: int,
        message: str,
    ) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _tool_label(tool_name: str | None) -> str:
        return {
            "debug_run": "openUBMC 诊断",
            "debug_collect": "openUBMC 实时采集",
            "log_bundle_collect": "日志包采集",
            "live_patch_run": "Live Patch",
            "upgrade_run": "固件升级",
            "workflow.next": "工作流继续",
            "runtime_status": "Target Runtime",
        }.get(tool_name, "MCP 工具调用")

    @staticmethod
    def _summary_text(value: Mapping[str, object], *names: str) -> str:
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @classmethod
    def _human_summary(
        cls,
        tool_name: str | None,
        value: object,
        *,
        error: bool,
    ) -> str:
        label = cls._tool_label(tool_name)
        if error:
            mapping = value if isinstance(value, Mapping) else {}
            message = (
                cls._summary_text(mapping, "message", "error")
                or str(value)
                or "调用未完成"
            )
            error_type = cls._summary_text(mapping, "error")
            lowered = f"{error_type} {message}".lower()
            if any(
                token in lowered
                for token in ("ssh", "telnet", "redfish", "connection", "session")
            ):
                recovery = "失效连接会被丢弃，任务上下文仍保留，重试时将重新建连"
            else:
                recovery = "任务上下文仍保留，但本次调用未自动重放"
            next_action = cls._summary_text(mapping, "next_action", "next_step")
            if not next_action:
                if "deadline" in lowered or "timeout" in lowered:
                    next_action = "增大 deadline 或缩小采集范围后重试"
                elif "capacity" in lowered or "busy" in lowered:
                    next_action = "等待正在执行的任务结束后重试"
                elif error_type in {"ValueError", "TypeError"}:
                    next_action = "修正工具参数后重试"
                else:
                    next_action = "沿用同一任务 ID 重试；实时证据会重新读取"
            return (
                f"{label}失败：{message}。自动恢复：{recovery}。"
                f"下一步：{next_action}。"
            )

        if not isinstance(value, Mapping):
            return str(value)
        closeout_markdown = value.get("closeout_markdown")
        if isinstance(closeout_markdown, str) and closeout_markdown.strip():
            return closeout_markdown.strip()
        if tool_name == "runtime_status":
            task_count = value.get("task_count", 0)
            persistent = value.get("persistent_task_contexts")
            retained = (
                persistent.get("entry_count", 0)
                if isinstance(persistent, Mapping)
                else 0
            )
            return (
                f"Target Runtime 正常：当前有 {task_count} 个活动任务上下文，"
                f"磁盘保留 {retained} 个可恢复上下文。"
            )
        if "completed" in value:
            if bool(value.get("completed")):
                state = "已完成"
            elif bool(value.get("partial")):
                state = "部分完成"
            else:
                state = "尚未完成"
            next_action = cls._summary_text(value, "next_action", "next_step")
            suffix = f" 下一步：{next_action}。" if next_action else ""
            return f"{label}{state}。{suffix}".strip()
        if tool_name == "log_bundle_collect":
            result = value.get("result")
            result = result if isinstance(result, Mapping) else value
            path = cls._summary_text(result, "bundle_root", "local_bundle_path")
            next_action = cls._summary_text(result, "next_step")
            path_text = f" 本地结果：{path}。" if path else ""
            next_text = f" 下一步：{next_action}。" if next_action else ""
            return f"日志包采集已完成。{path_text}{next_text}".strip()
        if tool_name in {"live_patch_run", "upgrade_run"}:
            journal = value.get("journal")
            stage = (
                cls._summary_text(journal, "stage")
                if isinstance(journal, Mapping)
                else ""
            )
            operation_status = (
                mutation_journal_operation_status(journal)
                if isinstance(journal, Mapping)
                else ""
            )
            next_action = cls._summary_text(value, "next_action", "next_step")
            if isinstance(journal, Mapping) and not next_action:
                next_action = cls._summary_text(
                    journal,
                    "recovery_decision",
                    "next_action",
                    "next_step",
                )
            if operation_status == "completed" and stage != "rollback_verified":
                return f"{label}已完成并验证；结构化结果中保留完整证据。"
            if stage == "replan_required":
                state = "尚未完成，需要重新规划"
                next_action = next_action or "修正变更计划后沿用同一任务重新执行"
            elif stage == "rollback_verified":
                if operation_status == "completed":
                    return f"{label}回滚已完成并验证；结构化结果中保留完整证据。"
                state = "尚未完成，已回滚并验证恢复"
                next_action = next_action or "确认新的变更方案后重新执行"
            elif stage == "verification_failed_terminal":
                state = "尚未完成，变更验证失败且流程已终止"
                next_action = next_action or "检查验证证据并制定恢复或重试方案"
            elif stage == "rollback_verification_failed_terminal":
                state = "尚未完成，回滚验证失败且流程已终止"
                next_action = next_action or "先确认目标当前状态，再决定恢复动作"
            elif operation_status == "mutation_outcome_unknown":
                state = "尚未完成，变更结果未知"
                next_action = next_action or "先核对持久化变更日志和目标现状"
            elif operation_status == "blocked":
                state = "尚未完成，当前恢复流程受阻"
                next_action = next_action or "补齐恢复条件后继续同一变更日志"
            elif operation_status == "failed":
                state = "尚未完成，变更流程失败"
                next_action = next_action or "检查结构化证据后重新规划"
            else:
                stage_text = stage or "unknown"
                state = f"尚未完成，当前变更日志阶段为 {stage_text}"
                next_action = next_action or "查看结构化证据并继续当前流程"
            return (
                f"{label}{state}。下一步：{next_action}。"
                "结构化结果中保留完整证据。"
            )
        if tool_name in {"debug_run", "debug_collect"}:
            ok = value.get("ok")
            code = cls._summary_text(value, "normalized_code", "code")
            targets = value.get("targets")
            target_text = (
                f"，覆盖 {len(targets)} 个目标"
                if isinstance(targets, list)
                else ""
            )
            state = "完成" if ok is not False else "部分失败"
            code_text = f"，状态码 {code}" if code else ""
            return (
                f"{label}{state}{target_text}{code_text}；"
                "本次证据为实时读取，未复用旧诊断结果。"
            )
        return f"{label}已完成；完整数据位于结构化结果中。"

    @classmethod
    def _tool_result(
        cls,
        value: object,
        *,
        tool_name: str | None = None,
        error: bool = False,
    ) -> dict[str, object]:
        if isinstance(value, ContextToolResult) and str(
            value.envelope.get("status", "")
        ) in {
            "failed",
            "cancelled",
            "blocked",
            "mutation_outcome_unknown",
        }:
            error = True
        text = cls._human_summary(tool_name, value, error=error)
        encoded = text.encode("utf-8")
        text_limit = (
            16_384
            if isinstance(value, Mapping) and value.get("closeout_markdown")
            else 4096
        )
        if len(encoded) > text_limit:
            text = encoded[: text_limit - 3].decode("utf-8", errors="ignore") + "..."
        result: dict[str, object] = {
            "content": [{"type": "text", "text": text}],
            "isError": error,
        }
        if isinstance(value, ContextToolResult):
            if tool_name in {
                "case_read",
                "evidence_read",
                "case_close",
                "case_forget",
                "phase_record",
                "workflow.advance",
                "workflow.next",
            }:
                structured = dict(value.envelope)
                structured.update(dict(value))
                structured["agent_envelope"] = dict(value.envelope)
                result["structuredContent"] = structured
            else:
                result["structuredContent"] = value.envelope
        elif isinstance(value, dict):
            result["structuredContent"] = value
        return result

    def handle(self, message: Mapping[str, object]) -> dict[str, object] | None:
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._error(message_id, -32600, "invalid JSON-RPC request")
        if method == "initialize":
            requested = (
                params.get("protocolVersion")
                if isinstance(params, Mapping)
                else None
            )
            protocol = requested if isinstance(requested, str) else MCP_PROTOCOL_VERSION
            return self._response(
                message_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "openubmc-target-runtime",
                        "version": RUNTIME_API_VERSION,
                    },
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "notifications/openubmc-task-complete":
            self.service.complete_task(self.task_id_for_params(params))
            return None
        if method == "notifications/cancelled":
            task_id = self.task_id_for_params(params)
            request_id = params.get("requestId") if isinstance(params, Mapping) else None
            if request_id is not None:
                self.service.cancel_operation(task_id, str(request_id))
            return None
        if method == "tools/list":
            return self._response(
                message_id,
                {"tools": self.service.tool_definitions()},
            )
        if method == "tools/call":
            if not isinstance(params, Mapping):
                return self._response(
                    message_id,
                    self._tool_result("tools/call params must be an object", error=True),
                )
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, Mapping):
                return self._response(
                    message_id,
                    self._tool_result("tool name and arguments are required", error=True),
                )
            try:
                value = self.service.call_tool(
                    name,
                    arguments,
                    task_id=self.task_id_for_params(params),
                    operation_id=str(message_id),
                )
            except Exception as exc:
                task_id = self.task_id_for_params(params)
                return self._response(
                    message_id,
                    self._tool_result(
                        self.service.error_result(
                            exc,
                            name=name,
                            arguments=arguments,
                            task_id=task_id,
                            operation_id=str(message_id),
                        ),
                        tool_name=name,
                        error=True,
                    ),
                )
            return self._response(
                message_id,
                self._tool_result(value, tool_name=name),
            )
        return self._error(message_id, -32601, f"method not found: {method}")


class StdioMcpServer:
    """Serve newline-delimited MCP messages and accept cancellation concurrently."""

    def __init__(
        self,
        endpoint: JsonRpcMcpEndpoint,
        *,
        max_workers: int = 8,
    ) -> None:
        self.endpoint = endpoint
        self.max_workers = max_workers

    def serve(self, reader=None, writer=None) -> None:
        input_stream = sys.stdin if reader is None else reader
        output_stream = sys.stdout if writer is None else writer
        write_lock = threading.Lock()
        inflight_lock = threading.Lock()
        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        futures: set[Future[dict[str, object] | None]] = set()
        inflight: dict[str, str] = {}

        def write_response(response: dict[str, object] | None) -> None:
            if response is None:
                return
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            with write_lock:
                output_stream.write(encoded + "\n")
                output_stream.flush()

        try:
            for line in input_stream:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    write_response(
                        JsonRpcMcpEndpoint._error(None, -32700, "invalid JSON")
                    )
                    continue
                if not isinstance(message, dict):
                    write_response(
                        JsonRpcMcpEndpoint._error(None, -32600, "invalid request")
                    )
                    continue
                if message.get("method") == "notifications/cancelled":
                    params = message.get("params", {})
                    request_id = (
                        params.get("requestId")
                        if isinstance(params, Mapping)
                        else None
                    )
                    if request_id is not None:
                        operation_id = str(request_id)
                        with inflight_lock:
                            task_id = inflight.get(operation_id)
                        if task_id is not None:
                            self.endpoint.service.cancel_operation(
                                task_id, operation_id
                            )
                if message.get("method") == "tools/call":
                    operation_id = str(message.get("id"))
                    task_id = self.endpoint.task_id_for_params(
                        message.get("params", {})
                    )
                    with inflight_lock:
                        inflight[operation_id] = task_id
                    future = executor.submit(self.endpoint.handle, message)
                    futures.add(future)

                    def completed(
                        item: Future[dict[str, object] | None],
                        *,
                        request_id: object = message.get("id"),
                        tracked_operation_id: str = operation_id,
                    ) -> None:
                        futures.discard(item)
                        with inflight_lock:
                            inflight.pop(tracked_operation_id, None)
                        try:
                            write_response(item.result())
                        except Exception as exc:
                            write_response(
                                JsonRpcMcpEndpoint._error(
                                    request_id,
                                    -32603,
                                    f"internal error: {type(exc).__name__}",
                                )
                            )

                    future.add_done_callback(completed)
                else:
                    write_response(self.endpoint.handle(message))
        finally:
            executor.shutdown(wait=True, cancel_futures=False)
            self.endpoint.service.close()
