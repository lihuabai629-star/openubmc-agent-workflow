from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    RUNTIME_API_VERSION,
    CredentialSelector,
    CredentialResolver,
    DuplicateRequestSuppressed,
    EpochState,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    RequestIdConflict,
    ResolvedSshCredentials,
    TargetIdentity,
    TargetSpec,
)


class TargetContractTests(unittest.TestCase):
    def test_target_spec_can_bind_transport_specific_selectors_as_one_target(self) -> None:
        ssh = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="SSH_PASSWORD",
            identity_file="",
            environ={},
        )
        redfish = CredentialSelector.for_redfish(
            user="Administrator",
            user_env="",
            password_env="REDFISH_PASSWORD",
            environ={},
        )

        target = TargetSpec.for_credential_selectors(
            host="BMC.EXAMPLE",
            credential_selectors=(ssh, redfish),
        )

        target.validate_credential_selector(ssh)
        target.validate_credential_selector(redfish)
        self.assertEqual(target.host, "bmc.example")
        self.assertEqual(
            set(target.to_public_dict()["credential_selector_fingerprints"]),
            {ssh.fingerprint, redfish.fingerprint},
        )

    def test_target_spec_contract_is_versioned_normalized_and_secret_free(self) -> None:
        secret = "must-not-enter-the-target-fingerprint"
        credential_path = "/private/workstation/credentials.env"
        identity_path = "/private/workstation/id_ed25519"
        selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="OPENUBMC_TEST_PASSWORD",
            identity_file=identity_path,
            environ={
                "OPENUBMC_CREDENTIALS_FILE": credential_path,
                "OPENUBMC_TEST_PASSWORD": secret,
            },
        )
        target = TargetSpec(
            host=" BMC.EXAMPLE ",
            ssh_port=2222,
            telnet_port=23,
            redfish_port=443,
            credential_selector_fingerprint=selector.fingerprint,
        )

        self.assertEqual(RUNTIME_API_VERSION, "openubmc.target-runtime.v1")
        self.assertEqual(target.host, "bmc.example")
        rendered = json.dumps(
            {
                "selector": selector.to_public_dict(),
                "target": target.to_public_dict(),
            },
            sort_keys=True,
        )
        self.assertNotIn(secret, rendered)
        self.assertNotIn(credential_path, rendered)
        self.assertNotIn(identity_path, rendered)
        self.assertEqual(
            target.to_public_dict()["credential_selector_fingerprint"],
            selector.fingerprint,
        )

    def test_credential_selector_distinguishes_sources_without_exposing_them(self) -> None:
        raw_values = {
            "alice",
            "bob",
            "PASSWORD_A",
            "PASSWORD_B",
            "/private/a/id_ed25519",
            "/private/b/id_ed25519",
            "/private/a/credentials.env",
            "/private/b/credentials.env",
        }
        selectors = [
            CredentialSelector.for_ssh(
                user="alice",
                user_env="",
                password_env="PASSWORD_A",
                identity_file="/private/a/id_ed25519",
                environ={
                    "OPENUBMC_CREDENTIALS_FILE": "/private/a/credentials.env"
                },
            ),
            CredentialSelector.for_ssh(
                user="bob",
                user_env="",
                password_env="PASSWORD_A",
                identity_file="/private/a/id_ed25519",
                environ={
                    "OPENUBMC_CREDENTIALS_FILE": "/private/a/credentials.env"
                },
            ),
            CredentialSelector.for_ssh(
                user="alice",
                user_env="",
                password_env="PASSWORD_B",
                identity_file="/private/a/id_ed25519",
                environ={
                    "OPENUBMC_CREDENTIALS_FILE": "/private/a/credentials.env"
                },
            ),
            CredentialSelector.for_ssh(
                user="alice",
                user_env="",
                password_env="PASSWORD_A",
                identity_file="/private/b/id_ed25519",
                environ={
                    "OPENUBMC_CREDENTIALS_FILE": "/private/a/credentials.env"
                },
            ),
            CredentialSelector.for_ssh(
                user="alice",
                user_env="",
                password_env="PASSWORD_A",
                identity_file="/private/a/id_ed25519",
                environ={
                    "OPENUBMC_CREDENTIALS_FILE": "/private/b/credentials.env"
                },
            ),
        ]

        self.assertEqual(len({selector.fingerprint for selector in selectors}), 5)
        rendered = json.dumps(
            [selector.to_public_dict() for selector in selectors],
            sort_keys=True,
        )
        for raw_value in raw_values:
            self.assertNotIn(raw_value, rendered)

    def test_target_identity_classifies_reboot_upgrade_and_replacement(self) -> None:
        original = TargetIdentity(
            product_id="product-a",
            machine_id="machine-a",
            firmware_id="firmware-1",
            reboot_anchor="boot-a",
            target_clock="2026-08-01T12:00:00Z",
            target_clock_epoch="1785585600",
            target_uptime_seconds="3600.00",
        )

        self.assertEqual(
            original.to_public_dict()["target_clock_epoch"],
            "1785585600",
        )
        self.assertEqual(
            original.to_public_dict()["target_uptime_seconds"],
            "3600.00",
        )

        self.assertEqual(
            original.change_kind(
                TargetIdentity(
                    product_id="product-a",
                    machine_id="machine-a",
                    firmware_id="firmware-1",
                    reboot_anchor="boot-a",
                    target_clock="2026-08-01T12:05:00Z",
                )
            ),
            "unchanged",
        )
        self.assertEqual(
            original.change_kind(
                TargetIdentity(
                    product_id="product-a",
                    machine_id="machine-a",
                    firmware_id="firmware-1",
                    reboot_anchor="boot-b",
                )
            ),
            "reboot",
        )
        self.assertEqual(
            original.change_kind(
                TargetIdentity(
                    product_id="product-a",
                    machine_id="machine-a",
                    firmware_id="firmware-2",
                    reboot_anchor="boot-b",
                )
            ),
            "firmware-change",
        )
        self.assertEqual(
            original.change_kind(
                TargetIdentity(
                    product_id="product-b",
                    machine_id="machine-b",
                    firmware_id="firmware-1",
                    reboot_anchor="boot-a",
                )
            ),
            "replacement",
        )

    def test_lane_reconnect_and_target_change_advance_different_epochs(self) -> None:
        initial = EpochState()
        ssh_ready = initial.connect_lane("ssh").cache_lane_state("ssh")
        telnet_ready = ssh_ready.connect_lane("telnet").cache_lane_state("telnet")

        ssh_invalid = telnet_ready.invalidate_lane("ssh", reason="channel-reset")
        self.assertEqual(ssh_invalid.target_epoch, 0)
        self.assertEqual(ssh_invalid.ssh.epoch, 0)
        self.assertFalse(ssh_invalid.ssh.cache_valid)
        self.assertTrue(ssh_invalid.telnet.cache_valid)

        ssh_reconnected = ssh_invalid.connect_lane("ssh")
        self.assertEqual(ssh_reconnected.target_epoch, 0)
        self.assertEqual(ssh_reconnected.ssh.epoch, 1)
        self.assertEqual(ssh_reconnected.telnet.epoch, 0)

        upgraded = ssh_reconnected.advance_target_epoch(reason="upgrade")
        self.assertEqual(upgraded.target_epoch, 1)
        self.assertEqual(upgraded.ssh.status, "invalid")
        self.assertEqual(upgraded.telnet.status, "invalid")
        self.assertFalse(upgraded.ssh.cache_valid)
        self.assertFalse(upgraded.telnet.cache_valid)

    def test_task_reuses_credentials_but_fresh_request_ids_read_again(self) -> None:
        loader_calls = 0
        remote_reads = 0

        def load_credentials(_selector: CredentialSelector) -> ResolvedSshCredentials:
            nonlocal loader_calls
            loader_calls += 1
            return ResolvedSshCredentials(
                user="debug-user",
                password="private-password",
                port=22,
                identity_file="/private/id_ed25519",
            )

        selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="OPENUBMC_TEST_PASSWORD",
            identity_file="/private/id_ed25519",
            environ={},
        )
        target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=selector.fingerprint,
        )
        task = OpenUBMCTaskRun(
            task_id="task-1",
            credential_resolver=CredentialResolver(load_credentials),
        )

        def collect(context) -> dict[str, object]:
            nonlocal remote_reads
            remote_reads += 1
            self.assertEqual(context.credentials.user, "debug-user")
            return {"remote_value": remote_reads}

        first_request = RemoteReadRequest.create(
            request_id="request-1",
            target=target,
            credential_selector=selector,
            collector_name="mdbctl",
            operation={"command": ["lsobj", "DiscreteSensor"]},
        )
        second_request = RemoteReadRequest.create(
            request_id="request-2",
            target=target,
            credential_selector=selector,
            collector_name="mdbctl",
            operation={"command": ["lsobj", "DiscreteSensor"]},
        )

        first = task.run_read(first_request, collect)
        second = task.run_read(second_request, collect)
        retry = task.run_read(first_request, collect)

        self.assertEqual(first.value, {"remote_value": 1})
        self.assertEqual(second.value, {"remote_value": 2})
        self.assertEqual(retry.value, first.value)
        self.assertTrue(retry.deduplicated)
        self.assertEqual(loader_calls, 1)
        self.assertEqual(remote_reads, 2)
        self.assertNotIn("private-password", repr(first))
        self.assertNotIn("/private/id_ed25519", repr(first))
        self.assertEqual(
            task.runtime_status()["metrics"],
            {
                "credential_resolutions": 1,
                "fresh_requests": 2,
                "remote_actions": 2,
                "retry_deduplications": 1,
            },
        )

        conflicting_request = RemoteReadRequest.create(
            request_id="request-1",
            target=target,
            credential_selector=selector,
            collector_name="mdbctl",
            operation={"command": ["lsclass"]},
        )
        with self.assertRaises(RequestIdConflict):
            task.run_read(conflicting_request, collect)
        self.assertEqual(remote_reads, 2)

    def test_failed_request_id_is_suppressed_instead_of_replayed(self) -> None:
        remote_actions = 0
        selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="",
            identity_file="",
            environ={},
        )
        target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=selector.fingerprint,
        )
        task = OpenUBMCTaskRun(
            task_id="task-failure",
            credential_resolver=CredentialResolver(
                lambda _selector: ResolvedSshCredentials(user="debug-user")
            ),
        )
        request = RemoteReadRequest.create(
            request_id="request-failure",
            target=target,
            credential_selector=selector,
            collector_name="mdbctl",
            operation={"command": ["lsclass"]},
        )

        def uncertain_failure(_context) -> dict[str, object]:
            nonlocal remote_actions
            remote_actions += 1
            raise RuntimeError("transport outcome unavailable")

        with self.assertRaisesRegex(RuntimeError, "outcome unavailable"):
            task.run_read(request, uncertain_failure)
        with self.assertRaises(DuplicateRequestSuppressed):
            task.run_read(request, uncertain_failure)
        self.assertEqual(remote_actions, 1)

    def test_completed_request_results_use_a_bounded_lru_cache(self) -> None:
        selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="",
            identity_file="",
            environ={},
        )
        target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=selector.fingerprint,
        )
        task = OpenUBMCTaskRun(
            task_id="bounded-request-task",
            credential_resolver=CredentialResolver(
                lambda _selector: ResolvedSshCredentials(
                    user="debug-user",
                    password="",
                    port=22,
                    identity_file="",
                )
            ),
            max_request_records=2,
        )
        for index in range(6):
            request = RemoteReadRequest.create(
                request_id=f"request-{index}",
                target=target,
                credential_selector=selector,
                collector_name="mdbctl",
                operation={"command": ["lsobj", str(index)]},
            )
            task.run_read(request, lambda _context, value=index: {"value": value})
        status = task.runtime_status()["request_cache"]
        self.assertEqual(status["count"], 2)
        self.assertEqual(status["limit"], 2)
        self.assertEqual(status["peak_count"], 2)
        self.assertEqual(status["evictions"], 4)


if __name__ == "__main__":
    unittest.main()
