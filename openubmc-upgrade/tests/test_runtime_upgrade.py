from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "openubmc-target-runtime"
SCRIPTS = REPO_ROOT / "openubmc-upgrade" / "scripts"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    MutationAuthorization,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    ResolvedRedfishCredentials,
    ResolvedSshCredentials,
    TargetSpec,
)


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        "openubmc_upgrade_runtime_adapter",
        SCRIPTS / "target_runtime_adapter.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeSession:
    number: int
    closed: bool = False


class FakeRedfishTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.operations: list[tuple[int, str]] = []

    def open_session(self, *, target, credentials) -> FakeSession:
        self.opens += 1
        return FakeSession(self.opens)

    def request(self, session: FakeSession, operation: str, **kwargs):
        self.operations.append((session.number, operation))
        callback = kwargs.get("callback")
        return callback(session) if callback is not None else operation

    @staticmethod
    def is_authentication_failure(_error: BaseException) -> bool:
        return False

    @staticmethod
    def close_session(session: FakeSession) -> None:
        session.closed = True


class UpgradeRuntimeTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_adapter()
        self.credential_bundle_parses = 0
        self.ssh_selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="SSH_PASSWORD",
            identity_file="",
            environ={},
        )
        self.redfish_selector = CredentialSelector.for_redfish(
            user="Administrator",
            user_env="",
            password_env="REDFISH_PASSWORD",
            environ={},
        )
        self.target = TargetSpec.for_credential_selectors(
            host="bmc.example",
            credential_selectors=(self.ssh_selector, self.redfish_selector),
        )
        parsed: dict[str, object] | None = None

        def bundle() -> dict[str, object]:
            nonlocal parsed
            if parsed is None:
                self.credential_bundle_parses += 1
                parsed = {
                    "redfish": ResolvedRedfishCredentials(
                        user="Administrator",
                        password="redfish-secret",
                    ),
                    "ssh": ResolvedSshCredentials(
                        user="debug-user",
                        password="ssh-secret",
                    ),
                }
            return parsed

        self.task = OpenUBMCTaskRun(
            task_id="upgrade-task",
            credential_resolver=CredentialResolver(
                redfish_loader=lambda _selector: bundle()["redfish"],
                ssh_loader=lambda _selector: bundle()["ssh"],
            ),
        )
        self.transport = FakeRedfishTransport()
        self.adapter = self.module.UpgradeRuntimeAdapter(
            task_run=self.task,
            target=self.target,
            redfish_selector=self.redfish_selector,
            ssh_selector=self.ssh_selector,
            redfish_transport=self.transport,
        )

    def test_upgrade_reconnects_and_verifies_with_new_epoch(self) -> None:
        artifact_sha = hashlib.sha256(b"firmware").hexdigest()
        uploads: list[dict[str, object]] = []
        debug_epochs: list[int] = []

        def apply(context):
            uploads.append(context.artifact.to_public_dict())
            return context.redfish_request(
                "upgrade-upload",
                callback=lambda session: {
                    "http_status": 202,
                    "task_uri": "/redfish/v1/TaskService/Tasks/1",
                    "session": session.number,
                    "artifact_reference": context.artifact.path,
                },
            )

        def read_version(context):
            return context.redfish_request(
                "upgrade-read-installed-version",
                replay_safe=True,
                callback=lambda session: {
                    "version": "2.0.0",
                    "session": session.number,
                },
            )

        def debug_verify(fresh_context):
            request = RemoteReadRequest.create(
                request_id="post-upgrade-debug",
                target=self.target,
                credential_selector=self.ssh_selector,
                collector_name="debug-freshness",
                operation={"expected_version": "2.0.0"},
            )
            result = fresh_context.run_read(
                request,
                lambda read_context: {
                    "target_epoch": read_context.epochs.target_epoch,
                    "ssh_user": read_context.credentials.user,
                },
            )
            debug_epochs.append(result.target_epoch)
            return result.value

        result = self.adapter.run(
            operation_id="upgrade-1",
            authorization=MutationAuthorization.from_original_intent(
                "upgrade-and-verify"
            ),
            artifact=self.module.UpgradeArtifact(
                path=str(Path(tempfile.gettempdir()) / "openubmc.hpm"),
                sha256=artifact_sha,
                product_version="2.0.0",
            ),
            apply=apply,
            read_installed_version=read_version,
            debug_verify=debug_verify,
        )

        self.assertEqual(len(uploads), 1)
        self.assertEqual(self.credential_bundle_parses, 1)
        self.assertEqual(result.epoch_before, 0)
        self.assertEqual(result.epoch_after, 1)
        self.assertEqual(result.verification["installed_version"], "2.0.0")
        self.assertEqual(result.verification["debug"]["target_epoch"], 1)
        self.assertEqual(debug_epochs, [1])
        self.assertEqual(self.transport.opens, 2)
        self.assertEqual(
            self.transport.operations,
            [
                (1, "upgrade-upload"),
                (2, "upgrade-read-installed-version"),
            ],
        )

    def test_redfish_session_rebuild_does_not_advance_target_epoch(self) -> None:
        lane = self.task.redfish_lane(
            target=self.target,
            credential_selector=self.redfish_selector,
            lease_name="upgrade",
            transport=self.transport,
        )
        lane.request("GET update-service", replay_safe=True)
        first = lane.session
        lane.close()
        lane.request("GET update-service", replay_safe=True)

        status = self.task.runtime_status()["targets"][0]
        self.assertTrue(first.closed)
        self.assertEqual(status["epochs"]["target_epoch"], 0)
        self.assertEqual(status["epochs"]["lanes"]["redfish"]["epoch"], 1)


if __name__ == "__main__":
    unittest.main()
