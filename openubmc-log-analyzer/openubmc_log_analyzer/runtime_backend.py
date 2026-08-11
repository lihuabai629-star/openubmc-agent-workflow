"""Public Target Runtime backend for Log Analyzer bundle collection."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from typing import Callable
import uuid


_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script_module(module_name: str, filename: str):
    """Load a bundled CLI module without occupying a generic module name."""

    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Log Analyzer module: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


_runtime_distribution = _load_script_module(
    "_openubmc_log_analyzer_runtime_distribution",
    "_runtime_distribution.py",
)
pull_bundle = _load_script_module(
    "_openubmc_log_analyzer_pull_bundle",
    "pull_bundle.py",
)
read_runtime_api_version = _runtime_distribution.read_runtime_api_version
runtime_content_digest = _runtime_distribution.runtime_content_digest


TARGET_RUNTIME_API_VERSION = "openubmc.target-runtime.v1"
PACKAGE_MARKER = ".openubmc-log-analyzer-package.json"
_RUNTIME_CACHE: dict[str, object] = {}


def _runtime_failure(reason: str) -> SystemExit:
    return SystemExit(
        "Target Runtime v1 validation failed before remote execution: "
        f"{reason}; repair or reinstall the Runtime/MCP environment"
    )


def _import_runtime(package_root: Path, digest: str):
    key = f"{package_root.resolve()}|{digest}"
    cached = _RUNTIME_CACHE.get(key)
    if cached is not None:
        return cached
    module_name = "_openubmc_log_runtime_" + digest.rsplit(":", 1)[-1][:16]
    spec = importlib.util.spec_from_file_location(
        module_name,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    if spec is None or spec.loader is None:
        raise _runtime_failure(f"cannot import Runtime package from {package_root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for loaded in tuple(sys.modules):
            if loaded == module_name or loaded.startswith(module_name + "."):
                sys.modules.pop(loaded, None)
        raise
    _RUNTIME_CACHE[key] = module
    return module


def _validated_runtime(
    package_root: Path,
    *,
    expected_api: str,
    expected_digest: str | None,
):
    actual_api = read_runtime_api_version(package_root)
    if actual_api != expected_api:
        raise _runtime_failure(
            f"Runtime API mismatch: expected {expected_api}, found {actual_api}"
        )
    actual_digest = runtime_content_digest(package_root)
    if expected_digest is not None and actual_digest != expected_digest:
        raise _runtime_failure(
            "Runtime content digest mismatch: "
            f"expected {expected_digest}, found {actual_digest}"
        )
    return _import_runtime(package_root, actual_digest)


def _load_runtime_module():
    skill_root = Path(__file__).resolve().parents[1]
    marker_path = skill_root / PACKAGE_MARKER
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            contract = marker["target_runtime"]
            vendor_path = Path(contract["vendorPath"])
            expected_api = str(contract["apiVersion"])
            expected_digest = str(contract["contentDigest"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise _runtime_failure("package marker is unavailable or invalid") from exc
        if (
            vendor_path.is_absolute()
            or vendor_path.as_posix() != str(contract["vendorPath"])
            or any(part in {"", ".", ".."} for part in vendor_path.parts)
        ):
            raise _runtime_failure("package vendorPath must stay inside the Skill root")
        package_root = (skill_root / vendor_path).resolve()
        if skill_root.resolve() not in package_root.parents:
            raise _runtime_failure("package vendorPath escapes the Skill root")
        return _validated_runtime(
            package_root,
            expected_api=expected_api,
            expected_digest=expected_digest,
        )

    spec = importlib.util.find_spec("openubmc_target_runtime")
    if spec is not None and spec.origin:
        return _validated_runtime(
            Path(spec.origin).resolve().parent,
            expected_api=TARGET_RUNTIME_API_VERSION,
            expected_digest=None,
        )
    canonical = (
        Path(__file__).resolve().parents[2]
        / "openubmc-target-runtime"
        / "openubmc_target_runtime"
    )
    if (canonical / "__init__.py").is_file():
        return _validated_runtime(
            canonical,
            expected_api=TARGET_RUNTIME_API_VERSION,
            expected_digest=None,
        )
    raise _runtime_failure("no installed, canonical, or vendored Runtime is available")


class PullBundleRedfishTransport:
    """Adapt the existing Redfish protocol behavior to a domain Runtime lane."""

    def __init__(self, args) -> None:
        self.args = args

    def open_session(self, *, target, credentials):
        return pull_bundle.redfish_create_session(
            ip=target.host,
            user=credentials.user,
            password=credentials.password,
            port=target.redfish_port,
            timeout=int(getattr(self.args, "redfish_timeout", 60)),
            proxy_mode=str(getattr(self.args, "redfish_proxy", "auto")),
        )

    @staticmethod
    def request(session, operation: str, **kwargs: object):
        del operation
        callback = kwargs.get("callback")
        if not callable(callback):
            raise TypeError("Redfish Runtime request requires a callback")
        return callback(session)

    @staticmethod
    def is_authentication_failure(error: BaseException) -> bool:
        if not isinstance(error, pull_bundle.BundlePullError):
            return False
        message = error.message.casefold()
        return error.code == "redfish_auth_failed" or any(
            token in message for token in ("http 401", "http 403", "invalid token")
        )

    def close_session(self, session) -> None:
        try:
            pull_bundle.redfish_delete_session(
                session,
                timeout=int(getattr(self.args, "redfish_timeout", 60)),
            )
        except pull_bundle.BundlePullError:
            pass


class LogBundleRuntimeLease:
    """Task-owned Redfish primary and SSH bundle fallback lanes."""

    def __init__(
        self,
        *,
        args,
        task_id: str,
        task_run=None,
        redfish_transport=None,
        ssh_transport=None,
    ) -> None:
        runtime = _load_runtime_module()
        self._runtime = runtime
        self.args = args
        self._closed = False
        self.redfish_selector = runtime.CredentialSelector.for_redfish(
            user=str(getattr(args, "redfish_user", "")),
            user_env=str(getattr(args, "redfish_user_env", "")),
            password_env=str(getattr(args, "redfish_password_env", "")),
            environ=os.environ,
        )
        self.ssh_selector = runtime.CredentialSelector.for_ssh(
            user=str(getattr(args, "ssh_user", "")),
            user_env=str(getattr(args, "ssh_user_env", "")),
            password_env=str(getattr(args, "ssh_password_env", "")),
            identity_file=str(getattr(args, "ssh_identity_file", "")),
            environ=os.environ,
        )
        self.target = runtime.TargetSpec.for_credential_selectors(
            host=str(args.ip),
            ssh_port=int(getattr(args, "ssh_port", 22)),
            redfish_port=int(getattr(args, "redfish_port", 443)),
            credential_selectors=(self.redfish_selector, self.ssh_selector),
            policy=runtime.TargetPolicy(ssh_host_key_policy="insecure"),
        )
        self.redfish_target = self.target
        self.ssh_target = self.target

        def load_redfish(_selector):
            user = pull_bundle.resolve_value(
                str(getattr(args, "redfish_user", "Administrator")),
                str(getattr(args, "redfish_user_env", "")),
                "Redfish 用户名",
            )
            password = pull_bundle.resolve_secret(
                str(getattr(args, "redfish_password", "")),
                str(getattr(args, "redfish_password_env", "")),
                "Redfish 密码",
                json_mode=bool(getattr(args, "json", False)),
            )
            return runtime.ResolvedRedfishCredentials(
                user=user,
                password=password,
                port=int(getattr(args, "redfish_port", 443)),
            )

        def load_ssh(_selector):
            user = pull_bundle.resolve_value(
                str(getattr(args, "ssh_user", "Administrator")),
                str(getattr(args, "ssh_user_env", "")),
                "SSH 用户名",
            )
            password = pull_bundle.resolve_secret(
                str(getattr(args, "ssh_password", "")),
                str(getattr(args, "ssh_password_env", "")),
                "SSH 密码",
                json_mode=bool(getattr(args, "json", False)),
                allow_empty=bool(getattr(args, "ssh_identity_file", "")),
            )
            return runtime.ResolvedSshCredentials(
                user=user,
                password=password,
                port=int(getattr(args, "ssh_port", 22)),
                identity_file=str(getattr(args, "ssh_identity_file", "")),
            )

        resolver = runtime.CredentialResolver(
            ssh_loader=load_ssh,
            redfish_loader=load_redfish,
        )
        self.task_run = task_run or runtime.OpenUBMCTaskRun(
            task_id=task_id,
            credential_resolver=resolver,
        )
        self._owns_task_run = task_run is None
        self.redfish_transport = redfish_transport or PullBundleRedfishTransport(args)
        self.ssh_transport = ssh_transport or runtime.OpenSshControlMasterTransport(
            host_key_policy="insecure",
        )
        self._redfish_lane_value = None
        self._ssh_lane_value = None

    def _redfish_lane(self):
        if self._redfish_lane_value is None:
            self._redfish_lane_value = self.task_run.redfish_lane(
                target=self.redfish_target,
                credential_selector=self.redfish_selector,
                lease_name="log-analyzer-bundle",
                transport=self.redfish_transport,
            )
        return self._redfish_lane_value

    def _ssh_lane(self):
        if self._ssh_lane_value is None:
            self._ssh_lane_value = self.task_run.ssh_lane(
                target=self.ssh_target,
                credential_selector=self.ssh_selector,
                lease_name="log-analyzer-bundle-fallback",
                transport=self.ssh_transport,
            )
        return self._ssh_lane_value

    @staticmethod
    def _identity_from_manager(runtime, payload: Mapping[str, object]):
        return runtime.TargetIdentity(
            product_id=str(payload.get("Model", "")),
            machine_id=str(payload.get("UUID", payload.get("SerialNumber", ""))),
            firmware_id=str(payload.get("FirmwareVersion", "")),
            reboot_anchor=str(
                payload.get("LastResetTime", payload.get("DateTime", ""))
            ),
        )

    def _manager_payload(self) -> dict[str, object]:
        lane = self._redfish_lane()

        def fetch(session):
            return pull_bundle.redfish_request_json(
                session,
                path=(
                    f"/redfish/v1/Managers/"
                    f"{getattr(self.args, 'redfish_manager_id', '1')}"
                ),
                timeout=int(getattr(self.args, "redfish_timeout", 60)),
                error_code="redfish_manager_fetch_failed",
                failure_message="Failed to fetch Redfish manager resource",
            )

        payload = lane.request(
            "log-analyzer-manager-identity",
            replay_safe=True,
            callback=fetch,
        )
        observation = self.task_run.observe_target_identity(
            self.redfish_target,
            self._identity_from_manager(self._runtime, payload),
        )
        if observation.change in {"reboot", "firmware-change", "replacement"}:
            payload = lane.request(
                "log-analyzer-manager-identity-refresh",
                replay_safe=True,
                callback=fetch,
            )
        return payload

    def _collect_redfish(self, *, local_dir: Path):
        if getattr(self.args, "remote_command", ""):
            raise pull_bundle.BundlePullError(
                "invalid_request",
                "--remote-command is SSH-only; use --transport ssh or remove --remote-command.",
            )
        manager_payload = self._manager_payload()
        return self._redfish_lane().request(
            "log-analyzer-bundle-collect",
            replay_safe=False,
            callback=lambda session: pull_bundle.run_redfish_bundle_flow_with_session(
                self.args,
                ip=str(self.args.ip),
                local_dir=local_dir,
                session=session,
                manager_payload=manager_payload,
            ),
        )

    @staticmethod
    def _require_success(result, *, code: str, message: str):
        if int(result.returncode) != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise pull_bundle.BundlePullError(
                code,
                f"{message}{': ' + detail if detail else ''}",
            )
        return result

    def _collect_ssh(
        self,
        *,
        local_dir: Path,
        search_roots: list[str],
        name_globs: list[str],
    ):
        lane = self._ssh_lane()
        remote_bundle_path = str(getattr(self.args, "remote_path", "")).strip()
        generation_ran = False
        remote_command = str(getattr(self.args, "remote_command", ""))
        if remote_command:
            generation_ran = True
            generated = self._require_success(
                lane.run_channel(
                    pull_bundle.build_remote_shell(remote_command),
                    timeout=float(getattr(self.args, "generate_timeout", 1800)),
                ),
                code="remote_collect_failed",
                message="Failed to generate remote bundle",
            )
            remote_bundle_path = (
                pull_bundle.parse_remote_bundle_path(
                    f"{generated.stdout}\n{generated.stderr}"
                )
                or remote_bundle_path
            )
        if not remote_bundle_path:
            discovered = self._require_success(
                lane.run_channel(
                    pull_bundle.build_discovery_command(search_roots, name_globs),
                    timeout=float(getattr(self.args, "search_timeout", 60)),
                ),
                code="bundle_discovery_failed",
                message="Failed to discover remote bundle",
            )
            remote_bundle_path = pull_bundle.parse_remote_bundle_path(
                discovered.stdout or ""
            )
        if not remote_bundle_path:
            raise pull_bundle.BundlePullError(
                "remote_bundle_not_found",
                "No remote bundle was found. Provide --remote-path or --remote-command, or widen --search-root/--name-glob.",
            )
        local_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(remote_bundle_path).name or f"openubmc-bundle-{uuid.uuid4().hex}.tar.gz"
        local_path = local_dir / filename
        transferred = lane.download_file(
            remote_bundle_path,
            str(local_path),
            timeout=float(getattr(self.args, "download_timeout", 1800)),
        )
        self._require_success(
            transferred,
            code="bundle_download_failed",
            message="Failed to download remote bundle",
        )
        return pull_bundle.BundleStageResult(
            remote_bundle_path=remote_bundle_path,
            local_bundle_path=local_path,
            generation_ran=generation_ran,
            transport="ssh",
        )

    def collect(
        self,
        *,
        local_dir: Path,
        search_roots: list[str],
        name_globs: list[str],
    ):
        transport = str(getattr(self.args, "transport", "auto"))
        if transport == "ssh" or (
            transport == "auto" and bool(getattr(self.args, "remote_command", ""))
        ):
            return self._collect_ssh(
                local_dir=local_dir,
                search_roots=search_roots,
                name_globs=name_globs,
            )
        if transport == "redfish":
            return self._collect_redfish(local_dir=local_dir)
        try:
            return self._collect_redfish(local_dir=local_dir)
        except pull_bundle.BundlePullError as redfish_error:
            try:
                return self._collect_ssh(
                    local_dir=local_dir,
                    search_roots=search_roots,
                    name_globs=name_globs,
                )
            except pull_bundle.BundlePullError as ssh_error:
                raise pull_bundle.BundlePullError(
                    "auto_transport_failed",
                    f"Redfish failed: {redfish_error.message}; SSH failed: {ssh_error.message}",
                ) from ssh_error

    def runtime_status(self) -> dict[str, object]:
        return self.task_run.runtime_status()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_task_run:
            self.task_run.close()

    def __enter__(self) -> "LogBundleRuntimeLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_log_bundle_runtime_lease(
    *,
    args,
    task_id: str | None = None,
    task_run=None,
    redfish_transport=None,
    ssh_transport=None,
) -> LogBundleRuntimeLease:
    return LogBundleRuntimeLease(
        args=args,
        task_id=task_id or f"one-shot-{uuid.uuid4().hex}",
        task_run=task_run,
        redfish_transport=redfish_transport,
        ssh_transport=ssh_transport,
    )


_MCP_STRING_OPTIONS = {
    "ip": "--ip",
    "transport": "--transport",
    "ssh_user": "--ssh-user",
    "ssh_password": "--ssh-password",
    "ssh_user_env": "--ssh-user-env",
    "ssh_password_env": "--ssh-password-env",
    "ssh_identity_file": "--ssh-identity-file",
    "redfish_user": "--redfish-user",
    "redfish_password": "--redfish-password",
    "redfish_user_env": "--redfish-user-env",
    "redfish_password_env": "--redfish-password-env",
    "redfish_manager_id": "--redfish-manager-id",
    "redfish_proxy": "--redfish-proxy",
    "redfish_action": "--redfish-action",
    "remote_path": "--remote-path",
    "remote_command": "--remote-command",
    "local_dir": "--local-dir",
    "extract_dir": "--extract-dir",
    "problem": "--problem",
    "analysis_since": "--analysis-since",
    "analysis_until": "--analysis-until",
}
_MCP_INTEGER_OPTIONS = {
    "ssh_port": "--ssh-port",
    "redfish_port": "--redfish-port",
    "analysis_max_files": "--analysis-max-files",
    "analysis_max_lines": "--analysis-max-lines",
    "search_timeout": "--search-timeout",
    "generate_timeout": "--generate-timeout",
    "download_timeout": "--download-timeout",
    "redfish_timeout": "--redfish-timeout",
    "redfish_task_timeout": "--redfish-task-timeout",
    "redfish_poll_interval": "--redfish-poll-interval",
}
_MCP_LIST_OPTIONS = {
    "search_roots": "--search-root",
    "name_globs": "--name-glob",
}


def _mcp_parse_args(arguments: Mapping[str, object]):
    known = (
        set(_MCP_STRING_OPTIONS)
        | set(_MCP_INTEGER_OPTIONS)
        | set(_MCP_LIST_OPTIONS)
        | {"extract", "deadline"}
    )
    unknown = set(arguments) - known
    if unknown:
        raise ValueError(
            "unsupported Log Analyzer MCP arguments: "
            + ", ".join(sorted(unknown))
        )
    argv: list[str] = []
    for name, option in _MCP_STRING_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        argv.extend([option, value])
    for name, option in _MCP_INTEGER_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be an integer")
        if int(value) != value:
            raise ValueError(f"{name} must be an integer")
        argv.extend([option, str(int(value))])
    for name, option in _MCP_LIST_OPTIONS.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise TypeError(f"{name} must be an array of strings")
        for item in value:
            argv.extend([option, item])
    extract = arguments.get("extract", True)
    if not isinstance(extract, bool):
        raise TypeError("extract must be a boolean")
    if not extract:
        argv.append("--no-extract")
    argv.append("--json")
    parsed = pull_bundle.parse_args(argv)
    if not str(parsed.ip).strip():
        raise ValueError("ip is required")
    return parsed


def _lease_key(args) -> tuple[object, ...]:
    return (
        str(args.ip).strip().lower(),
        str(args.transport),
        int(args.ssh_port),
        str(args.ssh_user),
        str(args.ssh_user_env),
        str(args.ssh_password_env),
        str(args.ssh_password),
        str(args.ssh_identity_file),
        int(args.redfish_port),
        str(args.redfish_user),
        str(args.redfish_user_env),
        str(args.redfish_password_env),
        str(args.redfish_password),
        str(args.redfish_proxy),
    )


class LogBundleMcpTask:
    def __init__(
        self,
        task_id: str,
        *,
        redfish_transport_factory: Callable[[object], object] | None,
        ssh_transport_factory: Callable[[object], object] | None,
        max_cached_leases: int = 32,
    ) -> None:
        if max_cached_leases < 1:
            raise ValueError("max_cached_leases must be positive")
        self.task_id = task_id
        self._redfish_transport_factory = redfish_transport_factory
        self._ssh_transport_factory = ssh_transport_factory
        self.max_cached_leases = int(max_cached_leases)
        self._leases: OrderedDict[
            tuple[object, ...], LogBundleRuntimeLease
        ] = OrderedDict()
        self._lease_evictions = 0
        self._lock = threading.RLock()

    def lease_for(self, args) -> LogBundleRuntimeLease:
        key = _lease_key(args)
        victim = None
        with self._lock:
            lease = self._leases.get(key)
            if lease is not None:
                self._leases.move_to_end(key)
                return lease
            lease = open_log_bundle_runtime_lease(
                args=args,
                task_id=self.task_id,
                redfish_transport=(
                    self._redfish_transport_factory(args)
                    if self._redfish_transport_factory is not None
                    else None
                ),
                ssh_transport=(
                    self._ssh_transport_factory(args)
                    if self._ssh_transport_factory is not None
                    else None
                ),
            )
            if len(self._leases) >= self.max_cached_leases:
                _, victim = self._leases.popitem(last=False)
                self._lease_evictions += 1
            self._leases[key] = lease
        if victim is not None:
            victim.close()
        return lease

    def maintain(self) -> int:
        with self._lock:
            leases = list(self._leases.values())
        return sum(
            lease.task_run.prune_dead_connections()
            for lease in leases
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            leases = list(self._leases.values())
            evictions = self._lease_evictions
        return {
            "task_id": self.task_id,
            "lease_count": len(leases),
            "lease_cache_limit": self.max_cached_leases,
            "lease_evictions": evictions,
            "leases": [lease.runtime_status() for lease in leases],
        }

    def close(self) -> None:
        with self._lock:
            leases = list(self._leases.values())
            self._leases.clear()
        for lease in leases:
            lease.close()


class LogBundleMcpBackend:
    """Domain backend for the MCP `log_bundle_collect` tool."""

    def __init__(
        self,
        *,
        redfish_transport_factory: Callable[[object], object] | None = None,
        ssh_transport_factory: Callable[[object], object] | None = None,
        max_cached_leases: int = 32,
    ) -> None:
        if max_cached_leases < 1:
            raise ValueError("max_cached_leases must be positive")
        self.redfish_transport_factory = redfish_transport_factory
        self.ssh_transport_factory = ssh_transport_factory
        self.max_cached_leases = int(max_cached_leases)

    def open_task(self, task_id: str) -> LogBundleMcpTask:
        return LogBundleMcpTask(
            task_id,
            redfish_transport_factory=self.redfish_transport_factory,
            ssh_transport_factory=self.ssh_transport_factory,
            max_cached_leases=self.max_cached_leases,
        )

    @staticmethod
    def close_task(task: LogBundleMcpTask) -> None:
        task.close()

    @staticmethod
    def maintain_task(task: LogBundleMcpTask) -> int:
        return task.maintain()

    @staticmethod
    def task_status(task: LogBundleMcpTask) -> dict[str, object]:
        return task.status()

    @staticmethod
    def log_bundle_collect(task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        args = _mcp_parse_args(arguments)
        remaining = max(1, int(context.remaining()))
        for name in (
            "search_timeout",
            "generate_timeout",
            "download_timeout",
            "redfish_timeout",
            "redfish_task_timeout",
        ):
            setattr(args, name, min(int(getattr(args, name)), remaining))
        local_dir = Path(
            args.local_dir
            or f"/tmp/openubmc-log-analyzer/{args.ip}/bundles"
        )
        extract_parent = Path(args.extract_dir or local_dir / "extract")
        search_roots = args.search_roots or list(pull_bundle.DEFAULT_SEARCH_ROOTS)
        name_globs = args.name_globs or list(pull_bundle.DEFAULT_NAME_GLOBS)
        stage = task.lease_for(args).collect(
            local_dir=local_dir,
            search_roots=search_roots,
            name_globs=name_globs,
        )
        context.raise_if_stopped()
        extraction = (
            pull_bundle.extract_archive(stage.local_bundle_path, extract_parent)
            if args.extract
            else None
        )
        analysis = None
        if args.problem.strip():
            if extraction is None:
                raise pull_bundle.BundlePullError(
                    "invalid_request",
                    "--problem requires extraction; remove --no-extract.",
                )
            since = pull_bundle.parse_analysis_time_bound(
                args.analysis_since,
                label="--analysis-since",
            )
            until = pull_bundle.parse_analysis_time_bound(
                args.analysis_until,
                label="--analysis-until",
            )
            analysis = pull_bundle.analyze_bundle(
                extraction.bundle_root,
                args.problem.strip(),
                max_files=args.analysis_max_files,
                max_lines=args.analysis_max_lines,
                since=since,
                until=until,
            )
        result: dict[str, object] = {
            "remote_bundle_path": stage.remote_bundle_path,
            "local_bundle_path": str(stage.local_bundle_path),
            "extract_dir": str(extraction.extract_dir) if extraction else "",
            "bundle_root": str(extraction.bundle_root) if extraction else "",
            "generation_ran": stage.generation_ran,
            "transport": stage.transport,
            "next_step": "使用 openubmc-log-analyzer 工作流分析 bundle_root",
        }
        if analysis is not None:
            result["analysis"] = analysis
        return pull_bundle.build_payload(
            ok=True,
            code="ok",
            error="",
            request={
                "ip": args.ip,
                "transport": args.transport,
                "remote_path": args.remote_path,
                "remote_command": bool(args.remote_command),
                "problem": args.problem,
                "extract": args.extract,
            },
            result=result,
        )
