from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "openubmc-target-runtime"
SCRIPTS = REPO_ROOT / "openubmc-live-patch" / "scripts"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    MutationAuthorization,
    MutationAuthorizationDenied,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    ResolvedSshCredentials,
    ResolvedTelnetCredentials,
    TargetSpec,
)


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        "openubmc_live_patch_runtime_adapter",
        SCRIPTS / "target_runtime_adapter.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTelnetResult:
    framing_complete = True
    timed_out = False
    connection_closed = False

    def __init__(self, value: str) -> None:
        self.value = value


class FakeTelnetTransport:
    def __init__(self, label: str) -> None:
        self.label = label
        self.opens: list[object] = []
        self.commands: list[str] = []
        self.closed: list[object] = []

    def open_session(self, *, target, credentials):
        session = object()
        self.opens.append(session)
        return session

    def run_command(self, session, command: str, **_kwargs: object):
        self.commands.append(command)
        return FakeTelnetResult(f"{self.label}:{id(session)}:{command}")

    @staticmethod
    def command_invalidates_session(_session, _result) -> bool:
        return False

    def close_session(self, session) -> None:
        self.closed.append(session)


class LivePatchRuntimeTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_adapter()
        self.credential_loads = 0
        self.selector = CredentialSelector.for_ssh(
            user="root",
            user_env="",
            password_env="",
            identity_file="",
            environ={},
        )
        self.target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=self.selector.fingerprint,
        )

        def load_credentials(_selector):
            self.credential_loads += 1
            return ResolvedSshCredentials(user="root", password="ssh-secret")

        self.task = OpenUBMCTaskRun(
            task_id="live-patch-task",
            credential_resolver=CredentialResolver(load_credentials),
        )
        self.telnet_credentials = ResolvedTelnetCredentials(
            user="root",
            password="telnet-secret",
        )

    def read_request(self, request_id: str) -> RemoteReadRequest:
        return RemoteReadRequest.create(
            request_id=request_id,
            target=self.target,
            credential_selector=self.selector,
            collector_name="debug-freshness",
            operation={"request": request_id},
        )

    def test_live_patch_keeps_its_telnet_session_and_direct_ssh_staging_contract(self) -> None:
        debug_transport = FakeTelnetTransport("debug")
        live_patch_transport = FakeTelnetTransport("live-patch")
        debug_lane = self.task.telnet_lane(
            target=self.target,
            credentials=self.telnet_credentials,
            lease_name="debug-log-file",
            transport=debug_transport,
        )
        debug_lane.run_command("debug-read")
        self.task.run_read(
            self.read_request("diagnosis"),
            lambda _context: "diagnosed",
        )

        adapter = self.module.LivePatchRuntimeAdapter(
            task_run=self.task,
            target=self.target,
            credential_selector=self.selector,
            telnet_credentials=self.telnet_credentials,
            telnet_transport=live_patch_transport,
        )
        staged_credentials: list[dict[str, str | int]] = []

        def apply(context):
            staged_credentials.append(context.ssh_credentials_mapping())
            result = context.run_telnet("apply-patch")
            context.record_backup("/tmp/unit.lua.bak")
            return {
                "ok": True,
                "telnet": result.value,
                "backup": "/tmp/unit.lua.bak",
            }

        result = adapter.run(
            operation_id="live-patch-1",
            authorization=MutationAuthorization.from_task_intent(
                "diagnose-and-fix",
                delivery_strategy="live-patch",
            ),
            restart_scope="none",
            operation={"remote": "/opt/bmc/apps/demo/unit.lua"},
            apply=apply,
            verify=lambda context: context.run_read(
                self.read_request("fresh-verify"),
                lambda read_context: {"epoch": read_context.epochs.target_epoch},
            ),
        )

        self.assertEqual(self.credential_loads, 1)
        self.assertEqual(staged_credentials[0]["user"], "root")
        self.assertEqual(staged_credentials[0]["password"], "ssh-secret")
        self.assertEqual(debug_transport.commands, ["debug-read"])
        self.assertEqual(live_patch_transport.commands, ["apply-patch"])
        self.assertIsNot(debug_transport.opens[0], live_patch_transport.opens[0])
        self.assertEqual(result.epoch_after, 1)
        self.assertEqual(result.verification.target_epoch, 1)
        self.assertEqual(result.journal.backup_reference, "/tmp/unit.lua.bak")
        self.assertEqual(
            result.journal.stage,
            "verified",
        )
        self.assertEqual(len(debug_transport.closed), 1)
        self.assertEqual(len(live_patch_transport.closed), 1)

    def test_diagnosis_only_is_rejected_before_live_patch_session_creation(self) -> None:
        live_patch_transport = FakeTelnetTransport("live-patch")
        adapter = self.module.LivePatchRuntimeAdapter(
            task_run=self.task,
            target=self.target,
            credential_selector=self.selector,
            telnet_credentials=self.telnet_credentials,
            telnet_transport=live_patch_transport,
        )

        with self.assertRaises(MutationAuthorizationDenied):
            adapter.run(
                operation_id="not-authorized",
                authorization=MutationAuthorization.from_original_intent(
                    "diagnosis-only"
                ),
                restart_scope="none",
                operation={"remote": "/opt/bmc/apps/demo/unit.lua"},
                apply=lambda _context: {"ok": True},
                verify=lambda _context: None,
            )

        self.assertEqual(live_patch_transport.opens, [])
        self.assertEqual(self.credential_loads, 0)

    def test_authorization_projection_keeps_all_existing_apply_gates(self) -> None:
        projection = self.module.ProjectedLivePatchAuthorization.from_authorization(
            MutationAuthorization.from_task_intent(
                "diagnose-and-fix",
                delivery_strategy="live-patch",
            ),
            restart_scope="skynet",
        )

        self.assertEqual(
            projection.to_cli_arguments(),
            (
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "skynet",
            ),
        )

    def test_rollback_projection_requires_the_distinct_rollback_authorization(self) -> None:
        projection = self.module.ProjectedLivePatchAuthorization.from_authorization(
            MutationAuthorization.from_original_intent("rollback"),
            restart_scope="none",
            action="rollback",
        )

        self.assertEqual(projection.original_intent, "rollback")
        with self.assertRaises(MutationAuthorizationDenied):
            self.module.ProjectedLivePatchAuthorization.from_authorization(
                MutationAuthorization.from_original_intent("live-patch"),
                restart_scope="none",
                action="rollback",
            )


if __name__ == "__main__":
    unittest.main()
