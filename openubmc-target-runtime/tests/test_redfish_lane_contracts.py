from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    OpenUBMCTaskRun,
    ResolvedRedfishCredentials,
    TargetIdentity,
    TargetSpec,
)


class RedfishAuthenticationError(RuntimeError):
    pass


@dataclass
class FakeSession:
    number: int
    closed: bool = False


class FakeRedfishTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.requests: list[tuple[int, str]] = []
        self.fail_next_auth = False

    def open_session(self, *, target, credentials) -> FakeSession:
        self.opens += 1
        return FakeSession(self.opens)

    def request(self, session: FakeSession, operation: str, **_kwargs):
        self.requests.append((session.number, operation))
        if self.fail_next_auth:
            self.fail_next_auth = False
            raise RedfishAuthenticationError("HTTP 401")
        return {"session": session.number, "operation": operation}

    @staticmethod
    def is_authentication_failure(error: BaseException) -> bool:
        return isinstance(error, RedfishAuthenticationError)

    @staticmethod
    def close_session(session: FakeSession) -> None:
        session.closed = True


def runtime_fixture(task_id: str = "redfish-task"):
    selector = CredentialSelector.for_redfish(
        user="Administrator",
        user_env="",
        password_env="OPENUBMC_REDFISH_PASSWORD",
        environ={},
    )
    target = TargetSpec(
        host="bmc.example",
        credential_selector_fingerprint=selector.fingerprint,
    )
    resolver = CredentialResolver(
        redfish_loader=lambda _selector: ResolvedRedfishCredentials(
            user="Administrator",
            password="private-redfish-password",
            port=443,
        )
    )
    return (
        OpenUBMCTaskRun(task_id=task_id, credential_resolver=resolver),
        target,
        selector,
    )


class RedfishLaneContractTests(unittest.TestCase):
    def test_domain_session_is_reused_but_not_shared_with_another_domain(self) -> None:
        task, target, selector = runtime_fixture()
        transport = FakeRedfishTransport()

        log_lane = task.redfish_lane(
            target=target,
            credential_selector=selector,
            lease_name="log-analyzer-bundle",
            transport=transport,
        )
        upgrade_lane = task.redfish_lane(
            target=target,
            credential_selector=selector,
            lease_name="upgrade",
            transport=transport,
        )

        first = log_lane.request("GET manager", replay_safe=True)
        second = log_lane.request("GET task", replay_safe=True)
        upgrade = upgrade_lane.request("GET update-service", replay_safe=True)

        self.assertEqual((first["session"], second["session"]), (1, 1))
        self.assertEqual(upgrade["session"], 2)
        self.assertEqual(transport.opens, 2)
        self.assertNotEqual(log_lane.session, upgrade_lane.session)

    def test_authentication_failure_discards_session_and_next_request_rebuilds(self) -> None:
        task, target, selector = runtime_fixture()
        transport = FakeRedfishTransport()
        lane = task.redfish_lane(
            target=target,
            credential_selector=selector,
            lease_name="log-analyzer-bundle",
            transport=transport,
        )

        lane.request("GET manager", replay_safe=True)
        first_session = lane.session
        transport.fail_next_auth = True
        with self.assertRaises(RedfishAuthenticationError):
            lane.request("POST dump", replay_safe=False)

        self.assertTrue(first_session.closed)
        self.assertIsNone(lane.session)
        rebuilt = lane.request("GET manager", replay_safe=True)
        self.assertEqual(rebuilt["session"], 2)
        self.assertEqual(lane.redfish_epoch, 1)

    def test_identity_change_invalidates_every_domain_session_for_target(self) -> None:
        task, target, selector = runtime_fixture()
        transport = FakeRedfishTransport()
        log_lane = task.redfish_lane(
            target=target,
            credential_selector=selector,
            lease_name="log-analyzer-bundle",
            transport=transport,
        )
        upgrade_lane = task.redfish_lane(
            target=target,
            credential_selector=selector,
            lease_name="upgrade",
            transport=transport,
        )
        log_lane.request("GET manager", replay_safe=True)
        upgrade_lane.request("GET update-service", replay_safe=True)
        old_log_session = log_lane.session
        old_upgrade_session = upgrade_lane.session

        initial = task.observe_target_identity(
            target,
            TargetIdentity(
                product_id="product-a",
                machine_id="machine-a",
                firmware_id="firmware-1",
                reboot_anchor="boot-a",
            ),
        )
        changed = task.observe_target_identity(
            target,
            TargetIdentity(
                product_id="product-a",
                machine_id="machine-a",
                firmware_id="firmware-2",
                reboot_anchor="boot-b",
            ),
        )

        self.assertEqual(initial.change, "initial")
        self.assertEqual(changed.change, "firmware-change")
        self.assertEqual(changed.target_epoch, 1)
        self.assertTrue(old_log_session.closed)
        self.assertTrue(old_upgrade_session.closed)
        self.assertEqual(log_lane.request("GET manager", replay_safe=True)["session"], 3)
        self.assertEqual(upgrade_lane.request("GET update-service", replay_safe=True)["session"], 4)


if __name__ == "__main__":
    unittest.main()
