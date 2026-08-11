#!/usr/bin/env python3
"""Run Upgrade through the shared Target Runtime mutation transaction."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import re
import sys
from typing import Generic, TypeVar


TARGET_RUNTIME_API_VERSION = "openubmc.target-runtime.v1"


def _load_runtime():
    local_loader = Path(__file__).resolve().with_name("_runtime_loader.py")
    loader_path = (
        local_loader
        if local_loader.is_file()
        else Path(__file__).resolve().parents[2]
        / "openubmc-target-runtime"
        / "tools"
        / "runtime_loader.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_openubmc_upgrade_runtime_loader",
        loader_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError("Target Runtime loader is unavailable")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    try:
        return loader.load_runtime_module(
            Path(__file__),
            expected_api=TARGET_RUNTIME_API_VERSION,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


_runtime = _load_runtime()
FreshVerificationContext = _runtime.FreshVerificationContext
MutationAuthorization = _runtime.MutationAuthorization
TaskAuthorizationPolicy = _runtime.TaskAuthorizationPolicy
MutationContext = _runtime.MutationContext
MutationRequest = _runtime.MutationRequest
MutationTransactionResult = _runtime.MutationTransactionResult
OpenUBMCTaskRun = _runtime.OpenUBMCTaskRun
RemoteReadRequest = _runtime.RemoteReadRequest
ResolvedRedfishCredentials = _runtime.ResolvedRedfishCredentials
TargetSpec = _runtime.TargetSpec


MutationValueT = TypeVar("MutationValueT")
DebugValueT = TypeVar("DebugValueT")


@dataclass(frozen=True)
class UpgradeArtifact:
    path: str
    sha256: str
    product_version: str

    def __post_init__(self) -> None:
        artifact_path = Path(self.path)
        if not artifact_path.is_absolute():
            raise ValueError("upgrade artifact path must be absolute")
        normalized_digest = self.sha256.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized_digest) is None:
            raise ValueError("upgrade artifact SHA-256 must be 64 hexadecimal characters")
        if not self.product_version.strip():
            raise ValueError("upgrade artifact product_version must not be empty")
        object.__setattr__(self, "path", str(artifact_path))
        object.__setattr__(self, "sha256", normalized_digest)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "product_version": self.product_version,
        }


@dataclass(frozen=True)
class UpgradeExecutionContext:
    mutation: MutationContext
    redfish_lane: object
    artifact: UpgradeArtifact

    @property
    def target(self) -> TargetSpec:
        return self.mutation.target

    @property
    def journal(self):
        return self.mutation.journal

    def redfish_credentials_mapping(self) -> dict[str, str | int]:
        credentials = self.mutation.credentials
        if not isinstance(credentials, ResolvedRedfishCredentials):
            raise TypeError("Upgrade mutation did not receive Redfish credentials")
        return credentials.to_redfish_mapping()

    def redfish_request(
        self,
        operation: str,
        *,
        replay_safe: bool = False,
        **kwargs: object,
    ):
        return self.redfish_lane.request(
            operation,
            replay_safe=replay_safe,
            **kwargs,
        )

    def mark_effects_started(self) -> None:
        self.mutation.mark_effects_started()


@dataclass(frozen=True)
class UpgradeVerificationContext:
    task_id: str
    target: TargetSpec
    credentials: object
    redfish_lane: object
    artifact: UpgradeArtifact

    def redfish_request(
        self,
        operation: str,
        *,
        replay_safe: bool = True,
        **kwargs: object,
    ):
        return self.redfish_lane.request(
            operation,
            replay_safe=replay_safe,
            **kwargs,
        )


class UpgradeRuntimeAdapter(Generic[MutationValueT, DebugValueT]):
    """Upload once, advance target epoch, reconnect, and verify fresh state."""

    LEASE_NAME = "upgrade"

    def __init__(
        self,
        *,
        task_run: OpenUBMCTaskRun,
        target: TargetSpec,
        redfish_selector,
        ssh_selector,
        redfish_transport: object,
    ) -> None:
        target.validate_credential_selector(redfish_selector)
        target.validate_credential_selector(ssh_selector)
        self.task_run = task_run
        self.target = target
        self.redfish_selector = redfish_selector
        self.ssh_selector = ssh_selector
        self.redfish_transport = redfish_transport

    def _redfish_lane(self):
        return self.task_run.redfish_lane(
            target=self.target,
            credential_selector=self.redfish_selector,
            lease_name=self.LEASE_NAME,
            transport=self.redfish_transport,
        )

    def mutation_request(
        self,
        *,
        operation_id: str,
        artifact: UpgradeArtifact,
        runtime_verification: bool = False,
        mutation_options: Mapping[str, object] | None = None,
    ) -> MutationRequest:
        return MutationRequest.create(
            operation_id=operation_id,
            target=self.target,
            credential_selector=self.redfish_selector,
            action="upgrade",
            operation={
                "artifact_path": artifact.path,
                "artifact_sha256": artifact.sha256,
                "product_version": artifact.product_version,
                "runtime_verification": runtime_verification,
                "mutation_options": dict(mutation_options or {}),
            },
        )

    @staticmethod
    def _installed_version(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in (
                "version",
                "installed_version",
                "active_bmc_version",
                "manager_firmware_version",
            ):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
        raise ValueError("fresh Upgrade verification did not return an installed version")

    def run(
        self,
        *,
        operation_id: str,
        authorization: MutationAuthorization,
        artifact: UpgradeArtifact,
        apply: Callable[[UpgradeExecutionContext], MutationValueT],
        read_installed_version: Callable[[UpgradeVerificationContext], object],
        debug_verify: Callable[[FreshVerificationContext], DebugValueT] | None = None,
        mutation_options: Mapping[str, object] | None = None,
        operation_context: object | None = None,
    ) -> MutationTransactionResult[MutationValueT, dict[str, object]]:
        authorization.require("upgrade")
        request = self.mutation_request(
            operation_id=operation_id,
            artifact=artifact,
            runtime_verification=debug_verify is not None,
            mutation_options=mutation_options,
        )

        def apply_upgrade(mutation: MutationContext) -> MutationValueT:
            return apply(
                UpgradeExecutionContext(
                    mutation=mutation,
                    redfish_lane=self._redfish_lane(),
                    artifact=artifact,
                )
            )

        def verify_upgrade(fresh: FreshVerificationContext) -> dict[str, object]:
            version_request = RemoteReadRequest.create(
                request_id=f"{operation_id}:installed-version",
                target=self.target,
                credential_selector=self.redfish_selector,
                collector_name="upgrade-installed-version",
                operation={"expected_version": artifact.product_version},
            )

            def collect_version(_read_context):
                return read_installed_version(
                    UpgradeVerificationContext(
                        task_id=self.task_run.task_id,
                        target=self.target,
                        credentials=_read_context.credentials,
                        redfish_lane=self._redfish_lane(),
                        artifact=artifact,
                    )
                )

            version_result = fresh.run_read(version_request, collect_version)
            installed_version = self._installed_version(version_result.value)
            if installed_version != artifact.product_version:
                raise ValueError(
                    "target installed version does not match the upgrade artifact: "
                    f"expected {artifact.product_version}, found {installed_version}"
                )
            debug_value = debug_verify(fresh) if debug_verify is not None else None
            return {
                "installed_version": installed_version,
                "version": version_result.value,
                "debug": debug_value,
                "target_epoch": version_result.target_epoch,
                "redfish_epoch": version_result.lane_epochs["redfish"],
            }

        return self.task_run.run_mutation(
            request,
            authorization=authorization,
            apply=apply_upgrade,
            verify=verify_upgrade,
            operation_context=operation_context,
        )

    def recover(
        self,
        *,
        operation_id: str,
        authorization: MutationAuthorization,
        artifact: UpgradeArtifact,
        inspection: Mapping[str, object],
        read_installed_version: Callable[[UpgradeVerificationContext], object],
        mutation_options: Mapping[str, object] | None = None,
        operation_context: object | None = None,
    ):
        """Resume only verification for one durably uncertain Upgrade."""

        request = self.mutation_request(
            operation_id=operation_id,
            artifact=artifact,
            mutation_options=mutation_options,
        )

        def verify_upgrade(fresh: FreshVerificationContext) -> dict[str, object]:
            version_request = RemoteReadRequest.create(
                request_id=f"{operation_id}:installed-version",
                target=self.target,
                credential_selector=self.redfish_selector,
                collector_name="upgrade-installed-version",
                operation={"expected_version": artifact.product_version},
            )

            def collect_version(_read_context):
                return read_installed_version(
                    UpgradeVerificationContext(
                        task_id=self.task_run.task_id,
                        target=self.target,
                        credentials=_read_context.credentials,
                        redfish_lane=self._redfish_lane(),
                        artifact=artifact,
                    )
                )

            version_result = fresh.run_read(version_request, collect_version)
            installed_version = self._installed_version(version_result.value)
            if installed_version != artifact.product_version:
                raise ValueError(
                    "target installed version does not match the upgrade artifact: "
                    f"expected {artifact.product_version}, found {installed_version}"
                )
            return {
                "installed_version": installed_version,
                "version": version_result.value,
                "debug": None,
                "target_epoch": version_result.target_epoch,
                "redfish_epoch": version_result.lane_epochs["redfish"],
            }

        return self.task_run.recover_mutation(
            request,
            authorization=authorization,
            inspect=lambda _context: inspection,
            verify=verify_upgrade,
            operation_context=operation_context,
        )
