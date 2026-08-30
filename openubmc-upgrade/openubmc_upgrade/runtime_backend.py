"""Production Redfish backend for typed Upgrade transactions."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import ssl
import stat
import sys
import threading
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
_ADAPTER_NAME = "_openubmc_upgrade_target_runtime_adapter"
_adapter_spec = importlib.util.spec_from_file_location(
    _ADAPTER_NAME,
    SCRIPTS / "target_runtime_adapter.py",
)
if _adapter_spec is None or _adapter_spec.loader is None:
    raise ImportError("openubmc-upgrade Runtime adapter is unavailable")
_adapter = importlib.util.module_from_spec(_adapter_spec)
sys.modules[_ADAPTER_NAME] = _adapter
_adapter_spec.loader.exec_module(_adapter)
UpgradeArtifact = _adapter.UpgradeArtifact
UpgradeRuntimeAdapter = _adapter.UpgradeRuntimeAdapter
from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    MutationAuthorization,
    MutationEffectsRejected,
    MutationJournalStore,
    MutationVerificationTerminalFailure,
    OpenUBMCTaskRun,
    ResolvedRedfishCredentials,
    TargetPolicy,
    TargetSpec,
    TaskAuthorizationPolicy,
    effect_recovery_mode,
    load_selected_credentials_file,
    mutation_recovery_route,
)


@dataclass(frozen=True)
class RedfishResponse:
    status: int
    headers: Mapping[str, str]
    payload: object


class RedfishHttpError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class RedfishTransportError(ConnectionError):
    """Report a same-origin Redfish request that lost its transport response."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        request_bytes: int,
        timeout: float,
        cause: BaseException,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.request_bytes = request_bytes
        self.timeout = timeout
        self.cause_type = type(cause).__name__
        super().__init__(
            f"Redfish {self.method} {path} lost its transport response "
            f"(request_bytes={request_bytes}, timeout_seconds={timeout:g}, "
            f"cause={self.cause_type})"
        )


class UpgradeActivationReverted(MutationVerificationTerminalFailure):
    """Raised when the uploaded version is no longer the active BMC image."""

    def __init__(self, message: str) -> None:
        super().__init__(message, outcome="activation-fallback")


class RedfishHttpSession:
    """Small same-origin HTTPS client with in-memory Basic authentication."""

    def __init__(
        self,
        *,
        target: TargetSpec,
        credentials: ResolvedRedfishCredentials,
        verify_tls: bool = True,
        timeout: float = 30,
    ) -> None:
        rendered_host = (
            f"[{target.host}]" if ":" in target.host and not target.host.startswith("[")
            else target.host
        )
        self.origin = f"https://{rendered_host}:{target.redfish_port}"
        token = base64.b64encode(
            f"{credentials.user}:{credentials.password}".encode("utf-8")
        ).decode("ascii")
        self.authorization = f"Basic {token}"
        self.timeout = timeout
        self.context = (
            ssl.create_default_context()
            if verify_tls
            else ssl._create_unverified_context()  # noqa: SLF001
        )
        self.opener = urlrequest.build_opener(
            urlrequest.ProxyHandler({}),
            urlrequest.HTTPSHandler(context=self.context),
        )

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse.urlsplit(path)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin.lower() != self.origin.lower():
                raise ValueError("Redfish response URI changed target origin")
            return path
        if not path.startswith("/"):
            raise ValueError("Redfish URI must be absolute on the selected target")
        return self.origin + path

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> RedfishResponse:
        if payload is not None and data is not None:
            raise ValueError("Redfish request cannot contain JSON and byte data together")
        request_headers = {
            "Authorization": self.authorization,
            "Accept": "application/json",
            **dict(headers or {}),
        }
        body = data
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urlrequest.Request(
            self._url(path),
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise ValueError("Redfish request timeout must be positive")
        try:
            with self.opener.open(
                request,
                timeout=request_timeout,
            ) as response:
                raw = response.read()
                status = int(response.status)
                response_headers = dict(response.headers.items())
        except urlerror.HTTPError as exc:
            raw = exc.read()
            message = raw.decode("utf-8", errors="replace")[-2048:]
            raise RedfishHttpError(exc.code, message or str(exc)) from exc
        except (OSError, TimeoutError, urlerror.URLError) as exc:
            raise RedfishTransportError(
                method=method,
                path=path,
                request_bytes=len(body or b""),
                timeout=request_timeout,
                cause=exc,
            ) from exc
        parsed: object = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {"body_sha256": hashlib.sha256(raw).hexdigest()}
        return RedfishResponse(status, response_headers, parsed)


class RedfishUpgradeTransport:
    def __init__(self, *, verify_tls: bool = True, timeout: float = 30) -> None:
        self.verify_tls = verify_tls
        self.timeout = timeout

    def open_session(self, *, target, credentials) -> RedfishHttpSession:
        return RedfishHttpSession(
            target=target,
            credentials=credentials,
            verify_tls=self.verify_tls,
            timeout=self.timeout,
        )

    @staticmethod
    def request(session, _operation: str, **kwargs: object):
        callback = kwargs.get("callback")
        if not callable(callback):
            raise ValueError("Upgrade Redfish operation requires a typed callback")
        return callback(session)

    @staticmethod
    def is_authentication_failure(error: BaseException) -> bool:
        return isinstance(error, RedfishHttpError) and error.status in {401, 403}

    @staticmethod
    def close_session(_session) -> None:
        return None


def _argument_text(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name, "")
    return str(value).strip() if isinstance(value, (str, int)) else ""


def _argument_bool(
    arguments: Mapping[str, object],
    name: str,
    *,
    default: bool = False,
) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _argument_timeout(
    arguments: Mapping[str, object],
    name: str,
    default: float,
) -> float:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive number")
    timeout = float(value)
    if timeout <= 0:
        raise ValueError(f"{name} must be a positive number")
    return timeout


def _default_credential_loader(
    arguments: Mapping[str, object],
) -> dict[str, dict[str, str | int]]:
    cached = arguments.get("_credential_values")
    if cached is None:
        values = load_selected_credentials_file()
    elif isinstance(cached, Mapping):
        values = {
            str(key): str(value)
            for key, value in cached.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    else:
        raise TypeError("_credential_values must be an internal mapping")

    def selected(explicit: str, selector: str, defaults: tuple[str, ...]) -> str:
        value = _argument_text(arguments, explicit)
        if value:
            return value
        selected_env = _argument_text(arguments, selector)
        names = ((selected_env,) if selected_env else ()) + defaults
        for name in names:
            candidate = os.environ.get(name, values.get(name, ""))
            if candidate:
                return candidate
        return ""

    user = selected(
        "redfish_user",
        "redfish_user_env",
        ("OPENUBMC_REDFISH_USER", "REDFISH_USERNAME"),
    )
    password = selected(
        "redfish_password",
        "redfish_password_env",
        ("OPENUBMC_REDFISH_PASSWORD", "REDFISH_PASSWORD"),
    )
    if not user or not password:
        raise ValueError("Upgrade requires Redfish credentials")
    return {
        "redfish": {
            "user": user,
            "password": password,
            "port": int(arguments.get("redfish_port", 443)),
        }
    }


def _read_stable_artifact(path: Path) -> tuple[bytes, str]:
    try:
        before_path = os.lstat(path)
    except FileNotFoundError:
        raise ValueError(f"upgrade artifact is unavailable: {path}") from None
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"upgrade artifact must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError(f"upgrade artifact could not be opened safely: {path}") from None

    digest = hashlib.sha256()
    blocks: list[bytes] = []
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino):
            raise ValueError(f"upgrade artifact changed while opening: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = os.lstat(path)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
        raise ValueError(f"upgrade artifact changed while reading: {path}")
    if any(getattr(after, name) != getattr(after_path, name) for name in stable_fields):
        raise ValueError(f"upgrade artifact path changed while reading: {path}")
    return b"".join(blocks), digest.hexdigest()


def _response_uri(response: RedfishResponse) -> str:
    for key in ("Location", "TaskMonitor"):
        value = response.headers.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(response.payload, Mapping):
        for key in ("@odata.id", "TaskMonitor", "TaskUri", "task_uri"):
            value = response.payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _multipart_body(
    artifact: UpgradeArtifact,
    artifact_bytes: bytes,
    update_parameters: Mapping[str, object],
) -> tuple[bytes, str]:
    boundary = "openubmc-target-runtime-" + uuid.uuid4().hex
    filename = Path(artifact.path).name.replace('"', "")
    prefix = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"UpdateParameters\"\r\n"
        "Content-Type: application/json\r\n\r\n"
        + json.dumps(
            dict(update_parameters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"UpdateFile\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix + artifact_bytes + suffix, boundary


def _update_parameters(arguments: Mapping[str, object]) -> dict[str, object]:
    active_mode = _argument_text(arguments, "active_mode") or "ResetBMC"
    if active_mode not in {"Immediately", "ResetBMC"}:
        raise ValueError("active_mode must be Immediately or ResetBMC")
    return {
        "ActiveMode": active_mode,
        "ForceUpdate": _argument_bool(arguments, "force_update", default=True),
    }


@dataclass
class _UpgradeBinding:
    task_run: OpenUBMCTaskRun
    target: TargetSpec
    redfish_selector: CredentialSelector
    ssh_selector: CredentialSelector
    transport: object

    def close(self) -> None:
        self.task_run.close()


class _UpgradeTask:
    def __init__(self, task_id: str, backend: "UpgradeMcpBackend") -> None:
        self.task_id = task_id
        self.backend = backend
        self._bindings: OrderedDict[
            tuple[object, ...], _UpgradeBinding
        ] = OrderedDict()
        self._binding_evictions = 0
        self._lock = threading.RLock()

    @staticmethod
    def _key(arguments: Mapping[str, object]) -> tuple[object, ...]:
        return (
            _argument_text(arguments, "ip").lower(),
            int(arguments.get("redfish_port", 443)),
            _argument_text(arguments, "redfish_user"),
            _argument_text(arguments, "redfish_user_env"),
            _argument_text(arguments, "redfish_password_env"),
            _argument_text(arguments, "redfish_password"),
            _argument_bool(arguments, "allow_insecure_tls", default=True),
        )

    def binding_for(self, arguments: Mapping[str, object]) -> _UpgradeBinding:
        key = self._key(arguments)
        with self._lock:
            existing = self._bindings.get(key)
            if existing is not None:
                self._bindings.move_to_end(key)
                return existing
            binding = self.backend._create_binding(self.task_id, arguments)
            if len(self._bindings) >= self.backend.max_cached_bindings:
                _, victim = self._bindings.popitem(last=False)
                victim.close()
                self._binding_evictions += 1
            self._bindings[key] = binding
            return binding

    def close(self) -> None:
        with self._lock:
            bindings = list(self._bindings.values())
            self._bindings.clear()
        for binding in bindings:
            binding.close()

    def maintain(self) -> int:
        with self._lock:
            bindings = list(self._bindings.values())
        return sum(binding.task_run.prune_dead_connections() for binding in bindings)

    def status(self) -> dict[str, object]:
        with self._lock:
            bindings = list(self._bindings.values())
        return {
            "task_id": self.task_id,
            "target_count": len(bindings),
            "binding_cache_limit": self.backend.max_cached_bindings,
            "binding_evictions": self._binding_evictions,
            "targets": [binding.task_run.runtime_status() for binding in bindings],
        }


class UpgradeMcpBackend:
    def __init__(
        self,
        *,
        journal_store: MutationJournalStore,
        credential_loader: Callable[
            [Mapping[str, object]], dict[str, dict[str, str | int]]
        ] = _default_credential_loader,
        redfish_transport_factory: Callable[[Mapping[str, object]], object]
        | None = None,
        max_cached_bindings: int = 32,
    ) -> None:
        if max_cached_bindings < 1:
            raise ValueError("max_cached_bindings must be positive")
        self.journal_store = journal_store
        self.credential_loader = credential_loader
        self.redfish_transport_factory = redfish_transport_factory
        self.max_cached_bindings = int(max_cached_bindings)

    def open_task(self, task_id: str) -> _UpgradeTask:
        return _UpgradeTask(task_id, self)

    @staticmethod
    def close_task(task: _UpgradeTask) -> None:
        task.close()

    @staticmethod
    def maintain_task(task: _UpgradeTask) -> int:
        return task.maintain()

    @staticmethod
    def task_status(task: _UpgradeTask) -> dict[str, object]:
        return task.status()

    def _create_binding(
        self,
        task_id: str,
        arguments: Mapping[str, object],
    ) -> _UpgradeBinding:
        host = _argument_text(arguments, "ip")
        if not host:
            raise ValueError("Upgrade requires a bound ip")
        credentials = self.credential_loader(arguments)
        redfish_selector = CredentialSelector.for_redfish(
            user=str(credentials["redfish"].get("user", "")),
            user_env="",
            password_env=_argument_text(arguments, "redfish_password_env"),
            environ={},
        )
        ssh_selector = CredentialSelector.for_ssh(
            user=_argument_text(arguments, "ssh_user"),
            user_env=_argument_text(arguments, "ssh_user_env"),
            password_env=_argument_text(arguments, "ssh_password_env"),
            identity_file=_argument_text(arguments, "ssh_identity_file"),
            environ={},
        )
        target = TargetSpec.for_credential_selectors(
            host=host,
            ssh_port=int(arguments.get("ssh_port", 22)),
            telnet_port=int(arguments.get("telnet_port", 23)),
            redfish_port=int(arguments.get("redfish_port", 443)),
            credential_selectors=(redfish_selector, ssh_selector),
            policy=TargetPolicy(read_only=False),
        )
        redfish_credentials = ResolvedRedfishCredentials.from_mapping(
            credentials["redfish"]
        )
        task_run = OpenUBMCTaskRun(
            task_id=task_id,
            credential_resolver=CredentialResolver(
                redfish_loader=lambda _selector: redfish_credentials
            ),
            mutation_journal_store=self.journal_store,
        )
        transport = (
            self.redfish_transport_factory(arguments)
            if self.redfish_transport_factory is not None
            else RedfishUpgradeTransport(
                verify_tls=not _argument_bool(
                    arguments,
                    "allow_insecure_tls",
                    default=True,
                ),
                timeout=_argument_timeout(arguments, "redfish_timeout", 30),
            )
        )
        return _UpgradeBinding(
            task_run=task_run,
            target=target,
            redfish_selector=redfish_selector,
            ssh_selector=ssh_selector,
            transport=transport,
        )

    @staticmethod
    def _upload(
        session,
        update_service: Mapping[str, object],
        artifact: UpgradeArtifact,
        artifact_bytes: bytes,
        arguments: Mapping[str, object],
        *,
        upload_timeout: float,
        mark_effects_started: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        mark_effects = mark_effects_started or (lambda: None)
        multipart_uri = update_service.get("MultipartHttpPushUri")
        http_push_uri = update_service.get("HttpPushUri")
        actions = update_service.get("Actions")
        simple_action = (
            actions.get("#UpdateService.SimpleUpdate")
            if isinstance(actions, Mapping)
            else None
        )
        if isinstance(multipart_uri, str) and multipart_uri:
            parameters = _update_parameters(arguments)
            body, boundary = _multipart_body(
                artifact,
                artifact_bytes,
                parameters,
            )
            mark_effects()
            response = session.request_json(
                "POST",
                multipart_uri,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=upload_timeout,
            )
            method = "MultipartHttpPushUri"
        elif isinstance(http_push_uri, str) and http_push_uri:
            mark_effects()
            response = session.request_json(
                "POST",
                http_push_uri,
                data=artifact_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=upload_timeout,
            )
            method = "HttpPushUri"
        elif isinstance(simple_action, Mapping):
            target = simple_action.get("target")
            image_uri = _argument_text(arguments, "image_uri")
            if not isinstance(target, str) or not target or not image_uri:
                raise ValueError(
                    "SimpleUpdate requires an advertised target and a BMC-reachable image_uri"
                )
            mark_effects()
            response = session.request_json(
                "POST",
                target,
                payload={"ImageURI": image_uri, **_update_parameters(arguments)},
            )
            method = "SimpleUpdate"
        else:
            raise ValueError("target UpdateService advertises no supported upload method")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Redfish upgrade upload returned HTTP {response.status}")
        result = {
            "method": method,
            "http_status": response.status,
            "task_uri": _response_uri(response),
            "upload_timeout_seconds": upload_timeout,
        }
        if method in {"MultipartHttpPushUri", "SimpleUpdate"}:
            result["parameters"] = _update_parameters(arguments)
        return result

    @staticmethod
    def _monitor_task(session, task_uri: str, context) -> dict[str, object]:
        if not task_uri:
            return {"state": "not_advertised", "task_uri": ""}
        terminal_success = {"completed", "completedok", "success", "succeeded"}
        terminal_failure = {
            "exception",
            "killed",
            "cancelled",
            "interrupted",
            "failed",
        }
        observations: list[str] = []
        while True:
            context.raise_if_stopped()
            try:
                response = session.request_json("GET", task_uri)
            except (OSError, TimeoutError, RedfishHttpError) as exc:
                return {
                    "state": "connection_lost",
                    "task_uri": task_uri,
                    "error": type(exc).__name__,
                    "observations": observations,
                }
            payload = response.payload if isinstance(response.payload, Mapping) else {}
            raw_state = payload.get("TaskState", payload.get("TaskStatus", ""))
            state = str(raw_state).strip()
            observations.append(state or f"http-{response.status}")
            normalized = state.lower().replace(" ", "")
            if normalized in terminal_success:
                return {
                    "state": "completed",
                    "task_uri": task_uri,
                    "observations": observations,
                }
            if normalized in terminal_failure:
                raise RuntimeError(f"Redfish upgrade task ended in {state}")
            context.wait(min(2.0, context.remaining()))

    @staticmethod
    def _installed_version(session) -> dict[str, object]:
        managers = session.request_json("GET", "/redfish/v1/Managers")
        payload = managers.payload if isinstance(managers.payload, Mapping) else {}
        members = payload.get("Members", [])
        if not isinstance(members, list):
            raise ValueError("Redfish Managers collection has no Members array")
        for member in members:
            if not isinstance(member, Mapping):
                continue
            path = member.get("@odata.id")
            if not isinstance(path, str) or not path:
                continue
            response = session.request_json("GET", path)
            manager = response.payload if isinstance(response.payload, Mapping) else {}
            for key in ("FirmwareVersion", "ManagerFirmwareVersion", "Version"):
                version = manager.get(key)
                if isinstance(version, str) and version:
                    identity = {"version": version, "manager": path}
                    last_reset_time = manager.get("LastResetTime")
                    if isinstance(last_reset_time, str) and last_reset_time.strip():
                        identity["last_reset_time"] = last_reset_time.strip()
                    return identity
        raise ValueError("Redfish Managers did not report an installed firmware version")

    @staticmethod
    def _activation_state(
        session,
        *,
        manager_version: str,
        expected_version: str,
    ) -> dict[str, object]:
        """Read the minimal UpdateService state needed to classify a fallback."""

        update_service = session.request_json("GET", "/redfish/v1/UpdateService")
        update_payload = (
            update_service.payload
            if isinstance(update_service.payload, Mapping)
            else {}
        )
        inventory_link = update_payload.get("FirmwareInventory")
        inventory_uri = (
            inventory_link.get("@odata.id", "")
            if isinstance(inventory_link, Mapping)
            else ""
        )
        versions: dict[str, str] = {}
        if isinstance(inventory_uri, str) and inventory_uri:
            inventory = session.request_json("GET", inventory_uri)
            inventory_payload = (
                inventory.payload if isinstance(inventory.payload, Mapping) else {}
            )
            members = inventory_payload.get("Members", [])
            if isinstance(members, list):
                for member in members:
                    if not isinstance(member, Mapping):
                        continue
                    path = member.get("@odata.id")
                    if not isinstance(path, str) or not path:
                        continue
                    identifier = path.rstrip("/").rsplit("/", 1)[-1]
                    if identifier not in {"ActiveBMC", "AvailableBMC", "BackupBMC"}:
                        continue
                    response = session.request_json("GET", path)
                    payload = (
                        response.payload
                        if isinstance(response.payload, Mapping)
                        else {}
                    )
                    version = payload.get("Version")
                    if isinstance(version, str):
                        versions[identifier] = version

        oem = update_payload.get("Oem")
        openubmc = oem.get("openUBMC") if isinstance(oem, Mapping) else None
        openubmc = openubmc if isinstance(openubmc, Mapping) else {}
        pending_values = (
            update_payload.get("Task"),
            openubmc.get("FirmwareToTakeEffect"),
            openubmc.get("BackgroundUpdateTasks"),
            openubmc.get("SyncUpdateState"),
        )
        activation_pending = any(
            value not in (None, "", [], {}) for value in pending_values
        )
        expected_locations = sorted(
            name for name, version in versions.items() if version == expected_version
        )
        return {
            "manager_version": manager_version,
            "active_version": versions.get("ActiveBMC", ""),
            "available_version": versions.get("AvailableBMC", ""),
            "backup_version": versions.get("BackupBMC", ""),
            "expected_locations": expected_locations,
            "activation_pending": activation_pending,
        }

    @classmethod
    def _wait_for_installed_version(
        cls,
        verification,
        context,
        arguments: Mapping[str, object],
        mutation_observation: Mapping[str, object],
    ) -> dict[str, object]:
        expected = verification.artifact.product_version
        poll_interval = float(arguments.get("version_poll_interval", 5))
        if poll_interval <= 0:
            raise ValueError("version_poll_interval must be positive")
        last_version = ""
        last_error = ""
        last_activation_state: dict[str, object] = {}
        monitor = mutation_observation.get("monitor")
        monitor_state = (
            str(monitor.get("state", "")) if isinstance(monitor, Mapping) else ""
        )
        while True:
            context.raise_if_stopped()
            try:
                value = verification.redfish_request(
                    "upgrade-read-installed-version",
                    replay_safe=True,
                    callback=cls._installed_version,
                )
            except RedfishHttpError as exc:
                if exc.status in {401, 403}:
                    raise
                last_error = f"{type(exc).__name__}: {exc}"
            except (OSError, TimeoutError, ValueError, urlerror.URLError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                version = str(value.get("version", ""))
                if version == expected:
                    return value
                last_version = version
                last_error = ""

                should_probe_activation = (
                    monitor_state == "connection_lost"
                    or context.remaining() <= poll_interval
                )
                if should_probe_activation:
                    try:
                        last_activation_state = verification.redfish_request(
                            "upgrade-read-activation-state",
                            replay_safe=True,
                            callback=lambda session: cls._activation_state(
                                session,
                                manager_version=version,
                                expected_version=expected,
                            ),
                        )
                    except RedfishHttpError as exc:
                        if exc.status in {401, 403}:
                            raise
                    except (OSError, TimeoutError, ValueError, urlerror.URLError):
                        pass
                    else:
                        if (
                            monitor_state == "connection_lost"
                            and last_activation_state.get("active_version") == version
                            and "AvailableBMC"
                            in last_activation_state.get("expected_locations", [])
                            and not bool(
                                last_activation_state.get("activation_pending", False)
                            )
                        ):
                            raise UpgradeActivationReverted(
                                "target returned on the previous active BMC version after "
                                "activation; the requested version remains only in "
                                "AvailableBMC: "
                                f"expected {expected}, active {version}"
                            )

            if context.remaining() <= poll_interval:
                detail = (
                    f"last version {last_version}"
                    if last_version
                    else last_error or "target did not return a version"
                )
                if last_activation_state:
                    detail += (
                        "; ActiveBMC "
                        f"{last_activation_state.get('active_version', '') or 'unknown'}, "
                        "AvailableBMC "
                        f"{last_activation_state.get('available_version', '') or 'unknown'}, "
                        "activation pending "
                        f"{str(bool(last_activation_state.get('activation_pending'))).lower()}"
                    )
                raise ValueError(
                    "target did not report the expected installed version before "
                    f"the reconnect deadline: expected {expected}; {detail}"
                )
            context.wait(min(poll_interval, context.remaining()))

    def upgrade_run(
        self,
        task: _UpgradeTask,
        arguments: Mapping[str, object],
        context,
    ) -> dict[str, object]:
        context.raise_if_stopped()
        artifact_path = Path(
            _argument_text(arguments, "artifact_path")
        ).expanduser()
        artifact_path = Path(os.path.abspath(os.fspath(artifact_path)))
        expected_sha = _argument_text(arguments, "artifact_sha256").lower()
        product_version = _argument_text(arguments, "product_version")
        artifact = UpgradeArtifact(
            path=str(artifact_path),
            sha256=expected_sha,
            product_version=product_version,
        )
        allow_insecure_tls = _argument_bool(
            arguments,
            "allow_insecure_tls",
            default=True,
        )
        raw_policy = arguments.get("_task_authorization_policy")
        if raw_policy is not None:
            if not isinstance(raw_policy, Mapping):
                raise TypeError("_task_authorization_policy must be an object")
            authorization = TaskAuthorizationPolicy.from_public_dict(raw_policy)
        else:
            intent = _argument_text(arguments, "_task_intent") or _argument_text(
                arguments, "intent"
            )
            authorization = MutationAuthorization.from_task_intent(
                intent,
                delivery_strategy=_argument_text(
                    arguments,
                    "_task_delivery_strategy",
                )
                or _argument_text(arguments, "delivery_strategy"),
                allow_insecure_tls=allow_insecure_tls,
            )
        if allow_insecure_tls:
            authorization.require_insecure_tls()
        binding = task.binding_for(arguments)
        minimum_target_epoch = arguments.get("_minimum_target_epoch", 0)
        if (
            isinstance(minimum_target_epoch, bool)
            or not isinstance(minimum_target_epoch, int)
            or minimum_target_epoch < 0
        ):
            raise TypeError("_minimum_target_epoch must be a non-negative integer")
        if minimum_target_epoch:
            binding.task_run.ensure_target_epoch(
                binding.target,
                minimum_target_epoch,
                reason="context-runtime-upgrade-sync",
            )
        adapter = UpgradeRuntimeAdapter(
            task_run=binding.task_run,
            target=binding.target,
            redfish_selector=binding.redfish_selector,
            ssh_selector=binding.ssh_selector,
            redfish_transport=binding.transport,
        )
        update_parameters = _update_parameters(arguments)
        mutation_options = {
            "image_uri": _argument_text(arguments, "image_uri"),
            "active_mode": update_parameters["ActiveMode"],
            "force_update": update_parameters["ForceUpdate"],
        }
        recovery_mode = effect_recovery_mode(arguments)
        recovery_route = mutation_recovery_route(
            recovery_mode,
            binding.task_run.mutation_journals,
            operation_id=context.operation_id,
            action="upgrade",
            label="Upgrade",
            matches=lambda journal: adapter.mutation_request(
                operation_id=str(getattr(journal, "operation_id", "")),
                artifact=artifact,
                mutation_options=mutation_options,
            ).fingerprint
            == str(getattr(journal, "operation_fingerprint", "")),
        )
        matching_journal = recovery_route.journal
        if matching_journal is not None:
            if recovery_route.disposition == "terminal":
                result = adapter.run(
                    operation_id=context.operation_id,
                    authorization=authorization,
                    artifact=artifact,
                    apply=lambda _execution: (_ for _ in ()).throw(
                        RuntimeError("terminal Upgrade replay invoked upload")
                    ),
                    read_installed_version=lambda _verification: (
                        (_ for _ in ()).throw(
                            RuntimeError(
                                "terminal Upgrade replay invoked verification"
                            )
                        )
                    ),
                    debug_verify=None,
                    mutation_options=mutation_options,
                    operation_context=context,
                )
                if (
                    result.journal.stage == "verification_failed_terminal"
                    and result.journal.last_known_state == "activation-fallback"
                ):
                    raise UpgradeActivationReverted(
                        "the existing upgrade operation already completed with an "
                        "activation fallback; the artifact was not uploaded again"
                    )
                return result.to_public_dict()
            if (
                recovery_route.disposition == "new"
                and recovery_mode is not None
            ):
                recovered = adapter.recover(
                    operation_id=matching_journal.operation_id,
                    authorization=authorization,
                    artifact=artifact,
                    inspection={"target_reachable": True},
                    read_installed_version=lambda _verification: (
                        (_ for _ in ()).throw(
                            RuntimeError("replanned Upgrade recovery invoked verify")
                        )
                    ),
                    mutation_options=mutation_options,
                    operation_context=context,
                )
                return recovered.to_transaction_dict(
                    target_fingerprint=binding.target.fingerprint
                )
            if recovery_route.disposition == "recover":
                recovery = self._recover_uncertain_upgrade(
                    binding=binding,
                    adapter=adapter,
                    journal=matching_journal,
                    artifact=artifact,
                    authorization=authorization,
                    arguments=arguments,
                    context=context,
                    mutation_options=mutation_options,
                )
                if recovery is not None:
                    return recovery
        artifact_bytes, actual_sha = _read_stable_artifact(artifact_path)
        if actual_sha != artifact.sha256:
            raise ValueError("upgrade artifact SHA-256 does not match")
        mutation_observation: dict[str, object] = {}

        def apply(execution) -> dict[str, object]:
            execution.journal.record_execution_evidence(
                expected_checksum=artifact.sha256,
            )
            result = execution.redfish_request(
                "upgrade-upload",
                callback=lambda session: self._apply_with_session(
                    session,
                    artifact,
                    artifact_bytes,
                    arguments,
                    context,
                    execution.mark_effects_started,
                ),
            )
            mutation_observation.update(result)
            return result

        result = adapter.run(
            operation_id=context.operation_id,
            authorization=authorization,
            artifact=artifact,
            apply=apply,
            read_installed_version=lambda verification: self._wait_for_installed_version(
                verification,
                context,
                arguments,
                mutation_observation,
            ),
            debug_verify=None,
            mutation_options=mutation_options,
            operation_context=context,
        )
        if (
            result.journal.stage == "verification_failed_terminal"
            and result.journal.last_known_state == "activation-fallback"
        ):
            raise UpgradeActivationReverted(
                "the existing upgrade operation already completed with an "
                "activation fallback; the artifact was not uploaded again"
            )
        context.raise_if_stopped()
        return result.to_public_dict()

    def _recover_uncertain_upgrade(
        self,
        *,
        binding: _UpgradeBinding,
        adapter: UpgradeRuntimeAdapter,
        journal,
        artifact: UpgradeArtifact,
        authorization: MutationAuthorization,
        arguments: Mapping[str, object],
        context,
        mutation_options: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Classify one durable uncertain upload before any possible re-upload."""

        lane = binding.task_run.redfish_lane(
            target=binding.target,
            credential_selector=binding.redfish_selector,
            lease_name="upgrade",
            transport=binding.transport,
        )

        def inspect_session(session) -> dict[str, object]:
            installed = self._installed_version(session)
            current = str(installed["version"])
            activation = self._activation_state(
                session,
                manager_version=current,
                expected_version=artifact.product_version,
            )
            return {
                "current_version": current,
                "activation": activation,
            }

        inspection = lane.request(
            "upgrade-recovery-inspection",
            replay_safe=True,
            callback=inspect_session,
        )
        current = str(inspection.get("current_version", ""))
        activation = inspection.get("activation")
        activation = activation if isinstance(activation, Mapping) else {}
        pending = bool(activation.get("activation_pending", False))
        expected_locations = activation.get("expected_locations", [])
        expected_locations = (
            list(expected_locations)
            if isinstance(expected_locations, list)
            else []
        )
        if (
            current != artifact.product_version
            and not pending
            and not expected_locations
        ):
            journal.transition(
                "recovery_blocked",
                verification_state="blocked",
                last_known_state="upgrade-recovery-evidence-insufficient",
                recovery_decision="manual",
            )
            return {
                "operation_id": journal.operation_id,
                "action": "upgrade",
                "target_fingerprint": binding.target.fingerprint,
                "epoch_before": journal.epoch_before,
                "epoch_after": journal.epoch_before,
                "mutation": {
                    "recovery": {
                        "decision": "manual",
                        "inspection": inspection,
                    }
                },
                "verification": None,
                "journal": journal.to_public_dict(),
                "idempotent_replay": False,
            }
        if (
            current != artifact.product_version
            and expected_locations
            and not pending
        ):
            journal.transition(
                "verification_failed_terminal",
                verification_state="failed",
                last_known_state="activation-fallback",
                recovery_decision="none",
            )
            raise UpgradeActivationReverted(
                "read-only Upgrade recovery found the requested artifact available "
                "but not active, with no pending activation"
            )
        journal.transition(
            "verification_failed",
            verification_state="failed",
            last_known_state=(
                "upgrade-recovery-found-installed-version"
                if current == artifact.product_version
                else "upgrade-recovery-found-pending-activation"
            ),
            recovery_decision="verify",
        )
        recovered = adapter.recover(
            operation_id=journal.operation_id,
            authorization=authorization,
            artifact=artifact,
            inspection={
                "target_reachable": True,
                "restart_observed": pending or current == artifact.product_version,
            },
            read_installed_version=lambda verification: self._wait_for_installed_version(
                verification,
                context,
                arguments,
                {"monitor": {"state": "connection_lost"}},
            ),
            mutation_options=mutation_options,
            operation_context=context,
        )
        return {
            "operation_id": recovered.operation_id,
            "action": "upgrade",
            "target_fingerprint": binding.target.fingerprint,
            "epoch_before": recovered.journal.epoch_before,
            "epoch_after": (
                recovered.journal.epoch_after
                or recovered.journal.epoch_before + 1
            ),
            "mutation": {"recovery": recovered.to_public_dict()},
            "verification": recovered.verification,
            "journal": recovered.journal.to_public_dict(),
            "idempotent_replay": False,
        }

    def _apply_with_session(
        self,
        session,
        artifact: UpgradeArtifact,
        artifact_bytes: bytes,
        arguments: Mapping[str, object],
        context,
        mark_effects_started: Callable[[], None],
    ) -> dict[str, object]:
        configured_upload_timeout = _argument_timeout(
            arguments,
            "upload_timeout",
            600,
        )
        try:
            manager_before = self._installed_version(session)
        except (OSError, TimeoutError, RedfishHttpError, ValueError, urlerror.URLError) as exc:
            manager_before = {
                "available": False,
                "error": type(exc).__name__,
            }
        discovery = session.request_json("GET", "/redfish/v1/UpdateService")
        if not isinstance(discovery.payload, Mapping):
            raise ValueError("Redfish UpdateService response must be an object")
        upload_timeout = min(
            configured_upload_timeout,
            context.remaining(),
        )
        if upload_timeout <= 0:
            context.raise_if_stopped()
        try:
            upload = self._upload(
                session,
                discovery.payload,
                artifact,
                artifact_bytes,
                arguments,
                upload_timeout=upload_timeout,
                mark_effects_started=mark_effects_started,
            )
        except RedfishHttpError as exc:
            if 400 <= exc.status < 500:
                raise MutationEffectsRejected(
                    f"Redfish upgrade request was explicitly rejected with HTTP {exc.status}: {exc}"
                ) from exc
            raise
        monitor = self._monitor_task(session, str(upload["task_uri"]), context)
        return {
            **upload,
            "monitor": monitor,
            "manager_before": manager_before,
            "artifact_path": artifact.path,
            "artifact_sha256": artifact.sha256,
            "product_version": artifact.product_version,
        }
