"""Single-process execution for durable Runtime Effect intents."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import Enum
import re
import threading

from .capability import EffectClass
from .contracts import RUNTIME_API_VERSION


EFFECT_INTENT_SCHEMA = f"{RUNTIME_API_VERSION}/effect-intent-v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class EffectIntent:
    """The durable identity and inputs needed to execute or recover one Effect."""

    run_id: str
    effect_id: str
    operation: str
    effect_class: EffectClass
    request_fingerprint: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        for name, value in (
            ("run_id", self.run_id),
            ("effect_id", self.effect_id),
            ("operation", self.operation),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"Effect intent {name} must be a safe identifier")
        if _SHA256.fullmatch(self.request_fingerprint) is None:
            raise ValueError("Effect intent request_fingerprint must be SHA-256")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "EffectIntent":
        if value.get("schema") != EFFECT_INTENT_SCHEMA:
            raise ValueError("unsupported Effect intent schema")
        if value.get("version") != 1:
            raise ValueError("unsupported Effect intent version")
        raw_arguments = value.get("arguments")
        if not isinstance(raw_arguments, Mapping):
            raise ValueError("Effect intent arguments must be an object")
        return cls(
            run_id=str(value.get("run_id", "")),
            effect_id=str(value.get("effect_id", "")),
            operation=str(value.get("operation", "")),
            effect_class=EffectClass(str(value.get("effect_class", ""))),
            request_fingerprint=str(value.get("request_fingerprint", "")),
            arguments=dict(raw_arguments),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": EFFECT_INTENT_SCHEMA,
            "version": 1,
            "run_id": self.run_id,
            "effect_id": self.effect_id,
            "operation": self.operation,
            "effect_class": self.effect_class.value,
            "request_fingerprint": self.request_fingerprint,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class PreparedEffect:
    intent: EffectIntent
    accepted_payload: Mapping[str, object]


class EffectRunMode(str, Enum):
    DISPATCH = "dispatch"
    REATTACH = "reattach"
    RECOVER = "recover"


class LocalEffectRunner:
    """Run durable Effects locally while preserving one identity across reattach."""

    def __init__(
        self,
        execute: Callable[[EffectIntent], Mapping[str, object]],
        recover: Callable[[EffectIntent], Mapping[str, object]],
        *,
        max_workers: int = 4,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("EffectRunner max_workers must be positive")
        self._execute = execute
        self._recover = recover
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="openubmc-effect",
        )
        self._futures: dict[
            tuple[str, str], Future[Mapping[str, object]]
        ] = {}
        self._lock = threading.Lock()
        self._closed = False

    def ensure(
        self,
        intent: EffectIntent,
        *,
        mode: EffectRunMode,
    ) -> Future[Mapping[str, object]]:
        if not isinstance(mode, EffectRunMode):
            raise TypeError("EffectRunner mode must be an EffectRunMode")
        identity = (intent.run_id, intent.effect_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("EffectRunner is closed")
            current = self._futures.get(identity)
            if current is not None and not current.done():
                return current
            callback = (
                self._recover
                if mode is EffectRunMode.RECOVER
                else self._execute
            )
            future = self._executor.submit(callback, intent)
            self._futures[identity] = future
            return future

    def has_seen(self, intent: EffectIntent) -> bool:
        with self._lock:
            return (intent.run_id, intent.effect_id) in self._futures

    @staticmethod
    def wait(
        future: Future[Mapping[str, object]],
        timeout: float,
    ) -> bool:
        try:
            future.result(timeout=max(0.0, timeout))
        except FutureTimeout:
            return False
        except BaseException:
            # The execution callback persists the authoritative failure or
            # unknown state. RunEngine reads that projection before deciding.
            return True
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
