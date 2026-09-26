"""Best-effort remembering after authentication, never a connection prerequisite.

Production composition enables this local policy. The Runtime library remains
side-effect free unless a caller supplies it; a failed save cannot fail a lane.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import ipaddress
import json
from pathlib import Path
import threading
import uuid

from .configuration import LocalConfigurationStore
from .credentials import LocalCredentialSource, read_private_credentials
from .credential_file import parse_credentials_text, selected_credential_value


def import_legacy_targets(text: str) -> dict:
    """Preserve legacy defaults when adding an IP-specific remembered account."""
    values = parse_credentials_text(text)
    config = {"schema_version": 1, "credentials": {}, "defaults": {}}
    for purpose, transport in (("bmc", "ssh"), ("bmc", "redfish"), ("os", "ssh")):
        prefix = "OPENUBMC_" + ("OS_" if purpose == "os" else "") + transport.upper()
        record = {}
        for field in ("user", "password", "identity_file"):
            names = [prefix + "_" + field.upper()]
            if purpose == "bmc" and transport == "redfish" and field in {"user", "password"}:
                names.append("REDFISH_" + ("USERNAME" if field == "user" else "PASSWORD"))
            record[field] = selected_credential_value(values, names, environ={}) or ""
        if any(record.values()):
            name = next((name for name, existing in config["credentials"].items()
                         if existing == record), purpose + "-" + transport)
            config["credentials"][name] = record
            config["defaults"].setdefault(purpose, {})[transport] = name
    return config


class VerifiedCredentialMemory:
    """Merge only the authenticated IP/purpose/transport into the chosen store.

    No authentication, discovery of other accounts, or device operation occurs
    here. Pending edits and concurrent changes are preserved, not overwritten.
    """

    def __init__(self, *, config_path=None, environ=None):
        self.source = LocalCredentialSource(config_path=config_path, environ=environ)
        self._lock = threading.RLock()
        self._expected = None

    def _path(self):
        path = self.source.select_path()
        if path is None:
            home = self.source.environ.get("XDG_CONFIG_HOME")
            if not home:
                home = str(Path(self.source.environ.get("HOME") or Path.home()) / ".config")
            path = Path(home) / "openubmc" / "credentials.json"
        return path

    @staticmethod
    def _generation(path):
        status = LocalConfigurationStore(path, kind="targets").status()
        original = None
        if status["active_revision"] is None and (path.exists() or path.is_symlink()):
            original = hashlib.sha256(read_private_credentials(path).encode()).hexdigest()
        return status["revision"], status["active_revision"], original

    def for_connection(self):
        """Bind the destination before authentication so later edits win."""
        path = self._path()
        bound = VerifiedCredentialMemory(config_path=path, environ={})
        bound._expected = self._generation(path)
        return bound

    def remember(self, *, host: str, purpose: str, transport: str, credentials) -> dict:
        try:
            with self._lock:
                return self._remember(host, purpose, transport, credentials)
        except Exception:
            # A connection already succeeded. Keep its in-memory credentials;
            # never include storage exceptions (which may contain secrets).
            return {"remembered": False, "code": "storage_unavailable"}

    def _remember(self, host, purpose, transport, credentials):
        try:
            host = str(ipaddress.ip_address(host.strip()))
        except ValueError:
            return {"remembered": False, "code": "unsupported_scope"}
        if (purpose not in {"bmc", "os"} or transport not in {"ssh", "redfish"}
                or credentials.port != {"ssh": 22, "redfish": 443}[transport]):
            # The existing schema has no hostname or port-qualified override.
            return {"remembered": False, "code": "unsupported_scope"}
        record = {"user": credentials.user, "password": credentials.password}
        if transport == "ssh" and credentials.identity_file:
            record["identity_file"] = credentials.identity_file
        if not record["user"] or not (record["password"] or record.get("identity_file")):
            return {"remembered": False, "code": "incomplete_account"}
        path = self._path()
        store = LocalConfigurationStore(path, kind="targets")
        status = store.status()
        if self._expected is not None and self._generation(path) != self._expected:
            return {"remembered": False, "code": "configuration_changed"}
        if status["revision"] != status["active_revision"]:
            return {"remembered": False, "code": "pending_configuration_edit"}
        source_text = None
        legacy_source = False
        if status["active_revision"]:
            # Persistence reads the current saved==active generation, not the
            # request's intentionally pinned credential-read snapshot.
            config = store.read_saved()
        elif path.exists() or path.is_symlink():
            source_text = read_private_credentials(path)
            if not source_text.lstrip().startswith("{"):
                legacy_source = True
            config = (json.loads(source_text) if source_text.lstrip().startswith("{")
                      else import_legacy_targets(source_text))
        else:
            config = {"schema_version": 1, "legacy_environment_fallback": True}
        store._validate(config)
        records = config.setdefault("credentials", {})
        targets = config.setdefault("targets", {})
        address = next((key for key in targets
                        if str(ipaddress.ip_address(key.strip())) == host), host)
        references = targets.get(address, {}).get(purpose, {})
        previous = references.get(transport)
        selected = previous or config.get("defaults", {}).get(purpose, {}).get(transport)
        if selected and all(records[selected].get(k, "") == record.get(k, "")
                            for k in ("user", "password", "identity_file")):
            return {"remembered": True, "code": "already_available"}
        if legacy_source:
            # Keep legacy per-field environment precedence and Telnet/OS-port
            # settings intact; format migration remains a separate local edit.
            return {"remembered": False, "code": "legacy_source_not_migrated"}
        name = next((name for name, value in records.items() if value == record), None)
        if name is None:
            name = "remembered-" + uuid.uuid4().hex
            records[name] = record
        targets.setdefault(address, {}).setdefault(purpose, {})[transport] = name
        if previous and previous.startswith("remembered-"):
            used = [config.get("defaults", {}), *targets.values()]
            if not any(previous in refs.values() for item in used for refs in item.values()):
                records.pop(previous, None)
        try:
            store.save_and_activate(config, expected_revision=status["revision"],
                                    expected_active_revision=status["active_revision"],
                                    expected_source_text=source_text, blocking=False)
        except Exception:
            return {"remembered": False, "code": "storage_unavailable"}
        return {"remembered": True, "code": "saved"}


_memory = ContextVar("verified_credential_memory", default=None)


@contextmanager
def credential_memory_scope(memory):
    token = _memory.set(memory)
    try:
        yield
    finally:
        _memory.reset(token)


def credential_memory_request(function):
    """Apply the production policy in the actual request worker, not its parent."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with credential_memory_scope(self.credential_memory):
            return function(self, *args, **kwargs)
    return wrapped


class AuthenticationMemory:
    """A lane-local, single-attempt sink captured at construction time."""

    def __init__(self):
        self.memory = _memory.get()
        self.status = {"remembered": False, "code": "not_attempted"}
        if self.memory is not None:
            try:
                self.memory = self.memory.for_connection()
            except Exception:
                self.memory = None
                self.status = {"remembered": False, "code": "storage_unavailable"}

    def authenticated(self, *, host, transport, credentials, purpose="bmc"):
        if self.memory is None or self.status["code"] != "not_attempted":
            return
        try:
            self.status = self.memory.remember(host=host, transport=transport,
                                               credentials=credentials, purpose=purpose)
        except Exception:
            self.status = {"remembered": False, "code": "storage_unavailable"}
