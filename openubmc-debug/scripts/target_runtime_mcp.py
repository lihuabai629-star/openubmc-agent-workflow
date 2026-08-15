#!/usr/bin/env python3
"""Run the local domain-specific Target Runtime MCP for openUBMC Debug."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
import importlib
import math
import os
from pathlib import Path
import sys
import threading

from _comparison import (
    compact_comparison_values,
    run_dual_target_comparison,
    run_multi_target_comparison,
)
from _cli_common import resolve_debug_credentials
from _source_root import resolve_source_root
from _target_runtime_adapter import _load_runtime_module, open_debug_runtime_lease
import workflow_remote
from _remote_common import (
    SSH_HOST_KEY_POLICY_ENV,
    SSH_KNOWN_HOSTS_FILE_ENV,
)


def select_default_credentials_file() -> str:
    selectors = (
        "OPENUBMC_CREDENTIALS_FILE",
        "OPENUBMC_DEBUG_CREDENTIALS_FILE",
    )
    for selector in selectors:
        if selector in os.environ:
            return str(os.environ[selector])
    config_root = Path(
        os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    )
    credentials = config_root / "openubmc" / "credentials.env"
    if credentials.is_file():
        os.environ["OPENUBMC_CREDENTIALS_FILE"] = str(credentials)
        return str(credentials)
    return ""


_STRING_OPTIONS = {
    "ip": "--ip",
    "keyword": "--keyword",
    "logs": "--logs",
    "tree_service": "--tree-service",
    "alarm_service": "--alarm-service",
    "alarm_path": "--alarm-path",
    "alarm_call_signature": "--alarm-call-signature",
    "source_root": "--source-root",
    "ssh_user": "--ssh-user",
    "ssh_user_env": "--ssh-user-env",
    "telnet_user": "--telnet-user",
    "telnet_user_env": "--telnet-user-env",
}
_MCP_DIRECT_CREDENTIAL_OPTIONS = (
    ("ssh_password", "--ssh-password"),
    ("telnet_password", "--telnet-password"),
)
_POLICY_OPTIONS = {
    "mdb_concurrency": "--mdb-concurrency",
}
for _transport, _field in (
    ("ssh", "password_env"),
    ("ssh", "identity_file"),
    ("telnet", "password_env"),
):
    _STRING_OPTIONS[f"{_transport}_{_field}"] = (
        f"--{_transport}-{_field.replace('_', '-')}"
    )
_INTEGER_OPTIONS = {
    "lines": "--lines",
    "rotated_limit": "--rotated-limit",
    "log_max_bytes": "--log-max-bytes",
    "tree_head": "--tree-head",
    "alarm_discovery_service_limit": "--alarm-discovery-service-limit",
    "alarm_discovery_path_limit": "--alarm-discovery-path-limit",
    "alarm_limit": "--alarm-limit",
    "timeout": "--timeout",
    "deadline": "--deadline",
    "source_max_matches": "--source-max-matches",
    "correlate_alarm_limit": "--correlate-alarm-limit",
    "correlation_time_window": "--correlation-time-window",
    "ssh_port": "--ssh-port",
    "telnet_port": "--telnet-port",
}
_BOOLEAN_OPTIONS = {
    "include_rotated": "--include-rotated",
    "mdb_only": "--mdb-only",
    "no_freshness": "--no-freshness",
    "no_source_correlation": "--no-source-correlation",
    "skip_telnet": "--skip-telnet",
    "compact_json": "--compact-json",
}
_LIST_OPTIONS = {
    "files": "--file",
    "alarm_call_args": "--alarm-call-arg",
    "mdb_queries": "--mdb-query",
    "mdb_expand_classes": "--mdb-expand-class",
}
_ORCHESTRATION_OPTIONS = {"profile", "reference_role"}
_TRANSPORT_STRING_OPTIONS = {
    "ssh_host_key_policy",
    "ssh_known_hosts_file",
}
_TRANSPORT_BOOLEAN_OPTIONS = {"allow_insecure_host_key"}


def _boolean_argument(
    arguments: Mapping[str, object],
    name: str,
    *,
    default: bool = False,
) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _workflow_argv(arguments: Mapping[str, object]) -> list[str]:
    unknown = set(arguments) - (
        set(_STRING_OPTIONS)
        | {name for name, _option in _MCP_DIRECT_CREDENTIAL_OPTIONS}
        | set(_POLICY_OPTIONS)
        | set(_INTEGER_OPTIONS)
        | set(_BOOLEAN_OPTIONS)
        | set(_LIST_OPTIONS)
        | _ORCHESTRATION_OPTIONS
        | _TRANSPORT_STRING_OPTIONS
        | _TRANSPORT_BOOLEAN_OPTIONS
    )
    if unknown:
        raise ValueError(
            "unsupported Debug MCP arguments: " + ", ".join(sorted(unknown))
        )
    argv: list[str] = []
    for name, option in _STRING_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        argv.extend([option, value])
    for name, option in _MCP_DIRECT_CREDENTIAL_OPTIONS:
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        argv.extend([option, value])
    for name, option in _POLICY_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"{name} must be a string or integer")
        argv.extend([option, str(value)])
    for name, option in _INTEGER_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        if int(value) != value:
            raise ValueError(f"{name} must be an integer")
        argv.extend([option, str(int(value))])
    for name, option in _BOOLEAN_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be a boolean")
        if value:
            argv.append(option)
    for name, option in _LIST_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise TypeError(f"{name} must be an array of strings")
        for item in value:
            argv.extend([option, item])
    for name in _TRANSPORT_STRING_OPTIONS:
        if name in arguments and not isinstance(arguments[name], str):
            raise TypeError(f"{name} must be a string")
    for name in _TRANSPORT_BOOLEAN_OPTIONS:
        if name in arguments and not isinstance(arguments[name], bool):
            raise TypeError(f"{name} must be a boolean")
    argv.append("--json")
    return argv


def workflow_arguments_from_namespace(args) -> dict[str, object]:
    """Project legacy workflow argparse values into the Catalog input shape."""

    result: dict[str, object] = {}
    for name in (
        set(_STRING_OPTIONS)
        | set(_POLICY_OPTIONS)
        | set(_INTEGER_OPTIONS)
        | set(_BOOLEAN_OPTIONS)
        | set(_LIST_OPTIONS)
    ):
        attribute = "alarm_call_arg" if name == "alarm_call_args" else name
        if hasattr(args, attribute):
            result[name] = getattr(args, attribute)
    return result


def _lease_key(args) -> tuple[object, ...]:
    host_key_policy = str(
        getattr(args, "ssh_host_key_policy", "")
        or os.environ.get(SSH_HOST_KEY_POLICY_ENV, "")
        or "insecure"
    ).strip().lower()
    known_hosts_file = str(
        getattr(args, "ssh_known_hosts_file", "")
        or os.environ.get(SSH_KNOWN_HOSTS_FILE_ENV, "")
    ).strip()
    return (
        str(args.ip).strip().lower(),
        int(args.ssh_port),
        int(args.telnet_port),
        str(args.ssh_user),
        str(args.ssh_user_env),
        str(args.ssh_password_env),
        str(getattr(args, "ssh_password", "")),
        str(args.ssh_identity_file),
        str(args.telnet_user),
        str(args.telnet_user_env),
        str(args.telnet_password_env),
        str(getattr(args, "telnet_password", "")),
        host_key_policy,
        known_hosts_file,
        bool(getattr(args, "allow_insecure_host_key", False)),
        os.environ.get("OPENUBMC_CREDENTIALS_FILE", ""),
        os.environ.get("OPENUBMC_DEBUG_CREDENTIALS_FILE", ""),
    )


class DebugMcpTask:
    """Own all Debug leases created inside one Codex task."""

    def __init__(
        self,
        task_id: str,
        *,
        mutation_journal_store=None,
        max_cached_leases: int = 32,
    ) -> None:
        if max_cached_leases < 1:
            raise ValueError("max_cached_leases must be positive")
        self.task_id = task_id
        self.mutation_journal_store = mutation_journal_store
        self.max_cached_leases = int(max_cached_leases)
        self._leases: OrderedDict[tuple[object, ...], object] = OrderedDict()
        self._active_lease_keys: dict[tuple[object, ...], int] = {}
        self._lease_evictions = 0
        self._peak_leases = 0
        self._lock = threading.RLock()

    def _lease_for(
        self,
        args,
        *,
        credential_values: Mapping[str, str] | None = None,
        pin: bool = False,
    ):
        key = _lease_key(args)
        victim = None
        with self._lock:
            existing = self._leases.get(key)
            if existing is not None:
                self._leases.move_to_end(key)
                if (
                    not bool(args.skip_telnet)
                    and getattr(existing, "telnet_lease", None) is None
                ):
                    if credential_values is None:
                        credentials = resolve_debug_credentials(
                            args,
                            include_telnet=True,
                        )
                    else:
                        credentials = resolve_debug_credentials(
                            args,
                            include_telnet=True,
                            credentials=credential_values,
                        )
                    existing.ensure_telnet(args, credentials["telnet"])
                if pin:
                    self._active_lease_keys[key] = (
                        self._active_lease_keys.get(key, 0) + 1
                    )
                return existing
            if credential_values is None:
                credentials = resolve_debug_credentials(
                    args,
                    include_telnet=not args.skip_telnet,
                )
            else:
                credentials = resolve_debug_credentials(
                    args,
                    include_telnet=not args.skip_telnet,
                    credentials=credential_values,
                )
            lease_options = {
                "args": args,
                "credential_bundle": credentials,
                "task_id": self.task_id,
            }
            if self.mutation_journal_store is not None:
                lease_options["mutation_journal_store"] = (
                    self.mutation_journal_store
                )
            lease = open_debug_runtime_lease(
                **lease_options,
            )
            if len(self._leases) >= self.max_cached_leases:
                victim_key = next(
                    (
                        candidate
                        for candidate in self._leases
                        if self._active_lease_keys.get(candidate, 0) == 0
                    ),
                    None,
                )
                if victim_key is not None:
                    victim = self._leases.pop(victim_key)
                    self._active_lease_keys.pop(victim_key, None)
            if victim is not None:
                self._lease_evictions += 1
            self._leases[key] = lease
            if pin:
                self._active_lease_keys[key] = (
                    self._active_lease_keys.get(key, 0) + 1
                )
            self._peak_leases = max(self._peak_leases, len(self._leases))
        if victim is not None:
            victim.close()
        return lease

    def lease_for(
        self,
        args,
        *,
        credential_values: Mapping[str, str] | None = None,
    ):
        return self._lease_for(
            args,
            credential_values=credential_values,
        )

    @contextmanager
    def lease_scope(
        self,
        args,
        *,
        credential_values: Mapping[str, str] | None = None,
    ):
        key = _lease_key(args)
        lease = self._lease_for(
            args,
            credential_values=credential_values,
            pin=True,
        )
        try:
            yield lease
        finally:
            victims: list[object] = []
            with self._lock:
                active = self._active_lease_keys.get(key, 0)
                if active <= 1:
                    self._active_lease_keys.pop(key, None)
                else:
                    self._active_lease_keys[key] = active - 1
                while len(self._leases) > self.max_cached_leases:
                    victim_key = next(
                        (
                            candidate
                            for candidate in self._leases
                            if self._active_lease_keys.get(candidate, 0) == 0
                        ),
                        None,
                    )
                    if victim_key is None:
                        break
                    victims.append(self._leases.pop(victim_key))
                    self._active_lease_keys.pop(victim_key, None)
                    self._lease_evictions += 1
            for victim in victims:
                victim.close()

    def maintain(self) -> int:
        with self._lock:
            leases = list(self._leases.values())
        return sum(
            int(lease.task_run.prune_dead_connections())
            for lease in leases
            if hasattr(lease, "task_run")
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            leases = list(self._leases.values())
            active_leases = sum(self._active_lease_keys.values())
        return {
            "task_id": self.task_id,
            "debug_run_count": len(leases),
            "debug_lease_cache_limit": self.max_cached_leases,
            "debug_lease_evictions": self._lease_evictions,
            "active_debug_leases": active_leases,
            "peak_debug_leases": self._peak_leases,
            "debug_runs": [lease.runtime_status() for lease in leases],
        }

    def close(self) -> None:
        with self._lock:
            leases = list(self._leases.values())
            self._leases.clear()
            self._active_lease_keys.clear()
        for lease in leases:
            lease.close()


class DebugMcpBackend:
    def __init__(
        self,
        *,
        engine_name: str = "mcp",
        mutation_journal_store=None,
        max_cached_leases: int = 32,
    ) -> None:
        self.engine_name = engine_name
        self.mutation_journal_store = mutation_journal_store
        self.max_cached_leases = int(max_cached_leases)

    def open_task(self, task_id: str) -> DebugMcpTask:
        return DebugMcpTask(
            task_id,
            mutation_journal_store=self.mutation_journal_store,
            max_cached_leases=self.max_cached_leases,
        )

    @staticmethod
    def close_task(task: DebugMcpTask) -> None:
        task.close()

    @staticmethod
    def maintain_task(task: DebugMcpTask) -> int:
        return task.maintain()

    @staticmethod
    def task_status(task: DebugMcpTask) -> dict[str, object]:
        return task.status()

    def _run_single(
        self,
        task: DebugMcpTask,
        arguments: Mapping[str, object],
        context,
        *,
        collect_only: bool,
    ) -> dict[str, object]:
        context.raise_if_stopped()
        bounded = dict(arguments)
        credential_values = bounded.pop("_credential_values", None)
        if credential_values is not None and not isinstance(
            credential_values, Mapping
        ):
            raise TypeError("_credential_values must be an internal mapping")
        minimum_target_epoch = bounded.pop("_minimum_target_epoch", 0)
        if (
            isinstance(minimum_target_epoch, bool)
            or not isinstance(minimum_target_epoch, int)
            or minimum_target_epoch < 0
        ):
            raise TypeError("_minimum_target_epoch must be a non-negative integer")
        bounded["deadline"] = max(
            1,
            min(
                int(bounded.get("deadline", 600)),
                int(math.ceil(context.remaining())),
            ),
        )
        profile = str(bounded.get("profile", "standard"))
        fast_object_alarm = collect_only and profile == "object-alarm"
        fast_mdb = collect_only and (
            profile == "mdb"
            or (profile == "standard" and bounded.get("mdb_only") is True)
        )
        if fast_object_alarm:
            bounded["skip_telnet"] = True
            bounded["no_freshness"] = True
            bounded["no_source_correlation"] = True
        if fast_mdb:
            bounded["mdb_only"] = True
            bounded["skip_telnet"] = True
            bounded["no_freshness"] = True
            bounded["no_source_correlation"] = True
        try:
            args = workflow_remote.parse_args(_workflow_argv(bounded))
            for name in _TRANSPORT_STRING_OPTIONS:
                setattr(args, name, str(bounded.get(name, "")))
            for name in _TRANSPORT_BOOLEAN_OPTIONS:
                setattr(args, name, _boolean_argument(bounded, name))
            args.fast_snapshot = fast_object_alarm or fast_mdb
            workflow_remote._validate_numeric_args(args)
            workflow_remote.validate_workflow_inputs(args)
        except SystemExit as exc:
            message = (
                str(exc.code).strip()
                if isinstance(exc.code, str) and str(exc.code).strip()
                else "Debug arguments failed validation"
            )
            raise ValueError(message) from None
        source_root, source_root_source = resolve_source_root(args.source_root)
        args.source_root = str(source_root) if source_root else ""
        with task.lease_scope(
            args,
            credential_values=(
                dict(credential_values)
                if isinstance(credential_values, Mapping)
                else None
            ),
        ) as lease:
            if minimum_target_epoch:
                lease.task_run.ensure_target_epoch(
                    lease.target,
                    minimum_target_epoch,
                    reason="orchestrated-fresh-verification",
                )
            captured: list[dict[str, object]] = []
            returncode = workflow_remote._execute_workflow(
                args,
                source_root_source=source_root_source,
                engine=self.engine_name,
                env=os.environ.copy(),
                tool_runner=workflow_remote.build_typed_debug_tool_runner(lease),
                runtime_status=lease.runtime_status,
                parallel_lanes=True,
                emit_output=False,
                output_handler=captured.append,
            )
        context.raise_if_stopped()
        if len(captured) != 1:
            raise RuntimeError("Debug workflow did not produce exactly one result")
        result = captured[0]
        if int(result.get("returncode", returncode)) != returncode:
            raise RuntimeError("Debug workflow return code disagrees with its result")
        return result

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        targets = arguments.get("targets")
        if targets is None:
            return self._run_single(task, arguments, context, collect_only=False)
        if not isinstance(targets, list) or len(targets) < 2 or not all(
            isinstance(target, Mapping) for target in targets
        ):
            raise ValueError("targets must contain at least two target objects")
        concurrency = arguments.get("concurrency", "auto")
        common = {
            key: value
            for key, value in arguments.items()
            if key not in {"targets", "reference_role", "concurrency"}
        }
        merged_targets = [
            {**common, **dict(target)}
            for target in targets
        ]
        runner = lambda request, child_context: self._run_single(
            task,
            request,
            child_context,
            collect_only=False,
        )
        if len(merged_targets) == 2:
            payload = run_dual_target_comparison(
                targets=merged_targets,
                run_target=runner,
                context=context,
                concurrency=concurrency,
            )
        else:
            payload = run_multi_target_comparison(
                targets=merged_targets,
                run_target=runner,
                context=context,
                concurrency=concurrency,
            )
        return (
            compact_comparison_values(payload)
            if arguments.get("compact_json") is True
            else payload
        )

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        return self._run_single(task, arguments, context, collect_only=True)


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 1:
        raise SystemExit(f"{name} must be positive")
    return value


def _runtime_state_dir() -> Path:
    configured = os.environ.get("OPENUBMC_TARGET_RUNTIME_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "state" / "openubmc-target-runtime").resolve()


_LOG_ANALYZER_MODULE = None
_LIVE_PATCH_MODULE = None
_UPGRADE_MODULE = None


def _load_log_analyzer_backend():
    """Load the sibling domain through its public Python integration surface."""

    global _LOG_ANALYZER_MODULE
    if _LOG_ANALYZER_MODULE is not None:
        return _LOG_ANALYZER_MODULE.LogBundleMcpBackend()
    skill_root = (
        Path(__file__).resolve().parents[2]
        / "openubmc-log-analyzer"
    )
    package = skill_root / "openubmc_log_analyzer" / "__init__.py"
    if not package.is_file():
        return None
    added = str(skill_root) not in sys.path
    if added:
        sys.path.insert(0, str(skill_root))
    try:
        module = importlib.import_module(
            "openubmc_log_analyzer.runtime_backend"
        )
    finally:
        if added:
            try:
                sys.path.remove(str(skill_root))
            except ValueError:
                pass
    _LOG_ANALYZER_MODULE = module
    return module.LogBundleMcpBackend()


def _load_public_domain_module(skill_name: str, package_name: str, module_name: str):
    skill_root = Path(__file__).resolve().parents[2] / skill_name
    package = skill_root / package_name / "__init__.py"
    if not package.is_file():
        return None
    added = str(skill_root) not in sys.path
    if added:
        sys.path.insert(0, str(skill_root))
    try:
        return importlib.import_module(f"{package_name}.{module_name}")
    finally:
        if added:
            try:
                sys.path.remove(str(skill_root))
            except ValueError:
                pass


def _load_live_patch_backend(journal_store):
    global _LIVE_PATCH_MODULE
    if _LIVE_PATCH_MODULE is None:
        _LIVE_PATCH_MODULE = _load_public_domain_module(
            "openubmc-live-patch",
            "openubmc_live_patch",
            "runtime_backend",
        )
    if _LIVE_PATCH_MODULE is None:
        return None
    return _LIVE_PATCH_MODULE.LivePatchMcpBackend(journal_store=journal_store)


def _load_upgrade_backend(journal_store):
    global _UPGRADE_MODULE
    if _UPGRADE_MODULE is None:
        _UPGRADE_MODULE = _load_public_domain_module(
            "openubmc-upgrade",
            "openubmc_upgrade",
            "runtime_backend",
        )
    if _UPGRADE_MODULE is None:
        return None
    return _UPGRADE_MODULE.UpgradeMcpBackend(journal_store=journal_store)


def create_service():
    select_default_credentials_file()
    runtime = _load_runtime_module()
    state_dir = _runtime_state_dir()
    artifact_dir = state_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    journal_store = runtime.MutationJournalStore(
        state_dir / "mutations",
        artifact_roots=(artifact_dir,),
    )
    debug_backend = DebugMcpBackend(
        mutation_journal_store=journal_store,
        max_cached_leases=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_DEBUG_LEASE_CACHE", 32
        ),
    )
    tool_backends = {
        "debug_run": debug_backend,
        "debug_collect": debug_backend,
    }
    log_backend = _load_log_analyzer_backend()
    if log_backend is not None:
        tool_backends["log_bundle_collect"] = log_backend
    live_patch_backend = _load_live_patch_backend(journal_store)
    if live_patch_backend is not None:
        tool_backends["live_patch_run"] = live_patch_backend
    upgrade_backend = _load_upgrade_backend(journal_store)
    if upgrade_backend is not None:
        tool_backends["upgrade_run"] = upgrade_backend
    task_context_store = runtime.TaskContextStore(
        state_dir / "task-contexts",
        ttl_seconds=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_CONTEXT_TTL",
            7 * 24 * 60 * 60,
        ),
        max_entries=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_CONTEXT_MAX_ENTRIES",
            128,
        ),
        max_state_bytes=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_CONTEXT_MAX_BYTES",
            256 * 1024,
        ),
    )
    orchestrated_backend = runtime.OrchestratedMcpBackend(
        tool_backends,
        state_store=task_context_store,
    )
    return runtime.RuntimeMcpService(
        orchestrated_backend,
        context_repository=runtime.SQLiteRuntimeRepository(
            state_dir / "context-runtime.sqlite3"
        ),
        blob_repository=runtime.FilesystemBlobRepository(
            state_dir / "evidence-blobs"
        ),
        envelope_max_bytes=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_ENVELOPE_MAX_BYTES", 24 * 1024
        ),
        context_max_cached_projections=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_CASE_CACHE", 64
        ),
        context_retention_seconds=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_CASE_TTL", 7 * 24 * 60 * 60
        ),
        context_storage_soft_limit_bytes=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_STORAGE_SOFT_BYTES", 1024 * 1024 * 1024
        ),
        context_mode=os.environ.get(
            "OPENUBMC_TARGET_RUNTIME_CONTEXT_MODE", "authoritative"
        ),
        max_tasks=_positive_env_int("OPENUBMC_TARGET_RUNTIME_MAX_TASKS", 32),
        idle_timeout_seconds=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_IDLE_TIMEOUT", 1800
        ),
        max_lifetime_seconds=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_MAX_LIFETIME", 28800
        ),
        max_concurrent_operations=_positive_env_int(
            "OPENUBMC_TARGET_RUNTIME_MAX_OPERATIONS", 8
        ),
    )


def main() -> int:
    runtime = _load_runtime_module()
    service = create_service()
    endpoint = runtime.JsonRpcMcpEndpoint(
        service,
        session_task_id=os.environ.get("CODEX_TASK_ID", "") or None,
    )
    runtime.StdioMcpServer(endpoint).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
