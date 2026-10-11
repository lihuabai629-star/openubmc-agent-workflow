from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openubmc_target_runtime import (
    CredentialResolver, CredentialSelector, OpenUBMCTaskRun,
    ResolvedSshCredentials, ResolvedRedfishCredentials, TargetSpec,
)
from openubmc_target_runtime.configuration import LocalConfigurationStore
from openubmc_target_runtime import configuration as configuration_module
from openubmc_target_runtime.credential_memory import (
    VerifiedCredentialMemory, credential_memory_scope,
)
from test_ssh_lane_contracts import FakeSshTransport
from test_redfish_lane_contracts import FakeRedfishTransport


class CredentialMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "credentials.json"
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        self.credentials = ResolvedSshCredentials(user="fixture", password="fixture-secret")
        self.store = LocalConfigurationStore(self.path, kind="targets")

    def remember(self, **kwargs):
        return self.memory.remember(**{"host": "192.0.2.10", "purpose": "bmc",
                                       "transport": "ssh", "credentials": self.credentials, **kwargs})

    def selected(self, host="192.0.2.10", purpose="bmc", transport="ssh", port=None):
        return CredentialResolver(config_path=self.path, environ={}).resolve_local(
            task_id="new-task", host=host, purpose=purpose, transport=transport, port=port,
        ).credentials

    def lane(self, transport=None, *, port=22, credentials=None):
        selector = CredentialSelector.for_ssh(user="fixture", user_env="", password_env="",
                                              identity_file="", environ={})
        target = TargetSpec(host="192.0.2.10", ssh_port=port,
                            credential_selector_fingerprint=selector.fingerprint)
        task = OpenUBMCTaskRun(task_id="fixture", credential_resolver=CredentialResolver(
            ssh_loader=lambda _: credentials or self.credentials))
        self.addCleanup(task.close)
        with credential_memory_scope(self.memory):
            return task.ssh_lane(target=target, credential_selector=selector,
                                 lease_name="fixture", transport=transport or FakeSshTransport())

    def test_success_is_automatically_reused_by_new_tasks_and_writes_are_deduplicated(self):
        lane = self.lane()
        self.assertFalse(self.path.parent.joinpath(".credentials.json.active.json").exists())
        lane.run_channel("fixture read", replay_safe=True, timeout=1)
        self.assertEqual(self.selected().password, "fixture-secret")
        self.assertEqual(lane.status()["credential_persistence"], {"remembered": True, "code": "saved"})
        revision = self.store.status()["revision"]
        lane.run_channel("another read", replay_safe=True, timeout=1)
        self.assertEqual(self.store.status()["revision"], revision)
        self.assertEqual(self.remember()["code"], "already_available")
        self.assertEqual(self.store.status()["revision"], revision)
        self.assertEqual(lane.transport.opens, 1)
        self.assertNotIn("fixture-secret", json.dumps(lane.status()))

    def test_rejected_authentication_never_saves(self):
        transport = FakeSshTransport()
        with patch.object(transport, "open_master", side_effect=RuntimeError("authentication failed")):
            lane = self.lane(transport)
            with self.assertRaises(RuntimeError):
                lane.run_channel("read", replay_safe=True, timeout=1)
        self.assertIsNone(self.store.status()["revision"])

    def test_storage_failure_does_not_fail_the_authenticated_operation(self):
        with patch.object(VerifiedCredentialMemory, "remember", side_effect=OSError("fixture-secret")):
            lane = self.lane()
            self.assertEqual(lane.run_channel("read", replay_safe=True, timeout=1).returncode, 0)
            self.assertTrue(lane.status()["connected"])
            self.assertEqual(lane.status()["credential_persistence"]["code"], "storage_unavailable")
            self.assertNotIn("fixture-secret", json.dumps(lane.status()))

    def test_partial_marker_write_restores_previous_active_revision(self):
        config = {"schema_version": 1, "credentials": {
            "old": {"user": "old", "password": "old-secret"}},
            "defaults": {"bmc": {"ssh": "old"}}}
        saved = self.store.save(config, expected_revision=None)
        self.store.activate(saved["revision"], expected_active_revision=None)
        original_write = configuration_module._atomic_write

        def fail_active(path, content):
            if path.name == ".credentials.json.active.json":
                original_write(path, content)
                raise OSError("fixture write failure")
            return original_write(path, content)

        with patch.object(configuration_module, "_atomic_write", new=fail_active):
            self.assertEqual(self.remember()["code"], "storage_unavailable")
        self.assertEqual(self.store.status()["revision"], saved["revision"])
        self.assertEqual(self.store.status()["active_revision"], saved["revision"])
        self.assertEqual(self.selected().password, "old-secret")
        snapshots = list((self.path.parent / ".credentials.json.revisions").glob("*.json"))
        self.assertEqual(len(snapshots), 1)

    def test_partial_first_save_leaves_no_credential_snapshot(self):
        original_write = configuration_module._atomic_write

        def fail_active(path, content):
            if path.name == ".credentials.json.active.json":
                raise OSError("fixture write failure")
            return original_write(path, content)

        with patch.object(configuration_module, "_atomic_write", new=fail_active):
            self.assertEqual(self.remember()["code"], "storage_unavailable")
        self.assertIsNone(self.store.status()["revision"])
        self.assertIsNone(self.store.status()["active_revision"])
        self.assertFalse(list((self.path.parent / ".credentials.json.revisions").glob("*.json")))

    def test_pending_draft_is_not_overwritten_or_activated(self):
        draft = self.store.save({"schema_version": 1}, expected_revision=None)
        self.assertEqual(self.remember()["code"], "pending_configuration_edit")
        self.assertEqual(self.store.status()["revision"], draft["revision"])
        self.assertIsNone(self.store.status()["active_revision"])

    def test_busy_configuration_writer_does_not_wait_or_interrupt_connection(self):
        lane = self.lane()
        with self.store._locked():
            self.assertEqual(lane.run_channel("read", replay_safe=True, timeout=1).returncode, 0)
        self.assertEqual(lane.status()["credential_persistence"]["code"], "storage_unavailable")
        self.assertIsNone(self.store.status()["revision"])

    def test_new_configuration_wins_if_edited_during_authentication(self):
        lane = self.lane()
        saved = self.store.save({"schema_version": 1, "credentials": {
            "new": {"user": "new", "password": "new-password"}},
            "targets": {"192.0.2.10": {"bmc": {"ssh": "new"}}}}, expected_revision=None)
        self.store.activate(saved["revision"], expected_active_revision=None)
        self.assertEqual(lane.run_channel("read", replay_safe=True, timeout=1).returncode, 0)
        self.assertEqual(self.selected().password, "new-password")
        self.assertEqual(lane.status()["credential_persistence"]["code"], "configuration_changed")

    def test_defaults_and_other_targets_purposes_and_transports_are_preserved(self):
        config = {"schema_version": 1, "credentials": {
            "common": {"user": "default", "password": "default-secret"}},
            "defaults": {"bmc": {"ssh": "common", "redfish": "common"}, "os": {"ssh": "common"}}}
        saved = self.store.save(config, expected_revision=None)
        self.store.activate(saved["revision"], expected_active_revision=None)
        self.assertTrue(self.remember()["remembered"])
        self.assertEqual(self.selected().password, "fixture-secret")
        for kwargs in ({"host": "192.0.2.11"}, {"purpose": "os"}, {"transport": "redfish"}):
            self.assertEqual(self.selected(**kwargs).password, "default-secret")

    def test_legacy_overlay_preserves_source_and_environment_precedence(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = (b"# keep this source byte for byte\r\n"
                    b"OPENUBMC_SSH_USER=old\r\nOPENUBMC_SSH_PASSWORD=old-secret\r\n"
                    b"REDFISH_USERNAME=redfish-old\r\nREDFISH_PASSWORD=redfish-secret\r\n"
                    b"OPENUBMC_OS_IP=192.0.2.20\r\nOPENUBMC_OS_SSH_USER=os-user\r\n"
                    b"OPENUBMC_OS_SSH_PASSWORD=os-secret\r\nOPENUBMC_OS_SSH_PORT=2200\r\n"
                    b"OPENUBMC_TELNET_USER=telnet-user\r\nOPENUBMC_TELNET_PASSWORD=telnet-secret\r\n")
        self.path.write_bytes(original)
        self.path.chmod(0o600)
        environment = {"OPENUBMC_SSH_PASSWORD": "environment-secret",
                       "REDFISH_PASSWORD": "redfish-environment-secret"}
        before = CredentialResolver(config_path=self.path, environ=environment)
        scopes = (("192.0.2.11", "bmc", "ssh", 22),
                  ("192.0.2.11", "bmc", "redfish", 443),
                  ("192.0.2.20", "os", "ssh", 2200))
        baseline = [before.resolve_local(task_id="before", host=host, purpose=purpose,
                                         transport=transport, port=port).credentials
                    for host, purpose, transport, port in scopes]
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ=environment)
        self.assertEqual(self.remember()["code"], "saved")
        self.assertEqual(self.path.read_bytes(), original)
        after = CredentialResolver(config_path=self.path, environ=environment)
        selected = [after.resolve_local(task_id="after", host=host, purpose=purpose,
                                        transport=transport, port=port).credentials
                    for host, purpose, transport, port in scopes]
        self.assertEqual([(item.user, item.password, item.port) for item in selected],
                         [(item.user, item.password, item.port) for item in baseline])
        self.assertEqual(after.resolve_local(task_id="after", host="192.0.2.10",
                                             transport="ssh").credentials.password, "fixture-secret")
        projected = after.resolve_local_values(task_id="after", host="192.0.2.11",
                                               arguments={},
                                               transports=("ssh",))
        self.assertEqual(projected["OPENUBMC_TELNET_PASSWORD"], "telnet-secret")
        self.assertEqual(projected["OPENUBMC_OS_SSH_PORT"], "2200")
        self.assertEqual(projected["OPENUBMC_OS_SSH_PASSWORD"], "os-secret")
        self.assertEqual(after._local_source.status()["capabilities"]["telnet"], True)
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "openubmc-debug/scripts"))
        import _cli_common
        with patch.dict(os.environ, {**environment, "OPENUBMC_CREDENTIALS_FILE": str(self.path)},
                        clear=True):
            args = SimpleNamespace(ssh_port=22, telnet_port=23)
            original_debug = _cli_common.resolve_debug_credentials(args)
            overlaid_debug = _cli_common.resolve_debug_credentials(args, credentials=projected)
            self.assertEqual(original_debug, overlaid_debug)
            explicit_args = SimpleNamespace(ssh_user="explicit-user", ssh_port=22, telnet_port=23)
            explicit_values = after.resolve_local_values(task_id="explicit-task",
                                                         host="192.0.2.11",
                                                         arguments={"ssh_user": "explicit-user"},
                                                         transports=("ssh",))
            self.assertEqual(_cli_common.resolve_debug_credentials(explicit_args),
                             _cli_common.resolve_debug_credentials(
                                 explicit_args, credentials=explicit_values))
            original_os = _cli_common.resolve_os_access()
            self.assertEqual((original_os["ip"], original_os["user"], original_os["password"],
                              original_os["port"]),
                             ("192.0.2.20", "os-user", "os-secret", 2200))
        self.assertNotIn("fixture-secret", json.dumps(self.memory.remember(
            host="192.0.2.10", purpose="bmc", transport="ssh", credentials=self.credentials)))

    def test_new_automatic_store_preserves_existing_environment_access_elsewhere(self):
        self.assertTrue(self.remember()["remembered"])
        resolver = CredentialResolver(config_path=self.path, environ={
            "OPENUBMC_SSH_USER": "other", "OPENUBMC_SSH_PASSWORD": "other-secret",
            "OPENUBMC_REDFISH_USER": "redfish", "OPENUBMC_REDFISH_PASSWORD": "redfish-secret",
        })
        for host, transport, expected in (("192.0.2.10", "ssh", "fixture-secret"),
                                          ("192.0.2.11", "ssh", "other-secret"),
                                          ("192.0.2.10", "redfish", "redfish-secret")):
            chosen = resolver.resolve_local(task_id="next", host=host, transport=transport)
            self.assertEqual(chosen.credentials.password, expected)

    def test_custom_ssh_port_is_reused_only_at_the_same_endpoint(self):
        self.assertEqual(self.remember(host="bmc.example")["code"], "unsupported_scope")
        # OpenSSH connects to TargetSpec.ssh_port even if a legacy loader left
        # the credential object's optional port at its default.
        custom = ResolvedSshCredentials(user="fixture", password="custom-secret")
        lane = self.lane(port=2222, credentials=custom)
        lane.run_channel("read", replay_safe=True, timeout=1)
        self.assertEqual(lane.status()["credential_persistence"]["code"], "saved")
        self.assertEqual(self.selected(port=2222).password, "custom-secret")
        self.assertEqual(self.selected(port=2222).port, 2222)
        self.assertTrue(self.memory.source.status()["configured"])
        projected = CredentialResolver(config_path=self.path, environ={}).resolve_local_values(
            task_id="fresh-public-task", host="192.0.2.10",
            arguments={"ssh_port": 2222}, transports=("ssh",))
        self.assertEqual(projected["OPENUBMC_SSH_PASSWORD"], "custom-secret")
        for host, port in (("192.0.2.10", 22), ("192.0.2.10", 2200),
                           ("192.0.2.11", 2222)):
            with self.subTest(host=host, port=port), self.assertRaises(Exception) as missing:
                self.selected(host=host, port=port)
            self.assertEqual(missing.exception.code, "credentials_missing")
        self.assertNotIn("custom-secret", json.dumps(lane.status()))

    def test_custom_port_does_not_take_the_default_port_ip_override(self):
        config = {"schema_version": 1, "credentials": {
            "default": {"user": "default", "password": "default-secret"},
            "ip-default-port": {"user": "ip", "password": "ip-secret"}},
            "defaults": {"bmc": {"ssh": "default"}},
            "targets": {"192.0.2.10": {"bmc": {"ssh": "ip-default-port"}}}}
        saved = self.store.save(config, expected_revision=None)
        self.store.activate(saved["revision"], expected_active_revision=None)
        custom = ResolvedSshCredentials(user="custom", password="custom-secret", port=2222)
        self.assertEqual(self.remember(credentials=custom)["code"], "saved")
        self.assertEqual(self.selected(port=2222).password, "custom-secret")
        self.assertEqual(self.selected(port=22).password, "ip-secret")
        self.assertEqual(self.selected(port=2200).password, "default-secret")
        self.assertEqual(self.selected(host="192.0.2.11", port=2222).password, "default-secret")
        second = ResolvedSshCredentials(user="second", password="second-secret", port=2200)
        self.assertEqual(self.remember(credentials=second)["code"], "saved")
        self.assertEqual(self.selected(port=2222).password, "custom-secret")
        self.assertEqual(self.selected(port=2200).password, "second-secret")

    def test_partial_legacy_telnet_readiness_is_not_hidden_by_overlay(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = "OPENUBMC_SSH_USER=old\nOPENUBMC_SSH_PASSWORD=old-secret\nOPENUBMC_TELNET_PASSWORD=telnet-secret\n"
        self.path.write_text(original)
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        before = self.memory.source.status()
        self.assertEqual(self.remember()["code"], "saved")
        self.assertEqual(self.path.read_text(), original)
        after = self.memory.source.status()
        self.assertEqual((before["configured"], before["capabilities"]["telnet"]),
                         (after["configured"], after["capabilities"]["telnet"]))

    def test_ambiguous_legacy_environment_does_not_activate_overlay(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = b"REDFISH_USERNAME=fixture\nREDFISH_PASSWORD=fixture-secret\n"
        self.path.write_bytes(original)
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={
            "OPENUBMC_REDFISH_USER": "different", "REDFISH_USERNAME": "fixture"})
        self.assertEqual(self.memory.for_connection().remember(
            host="192.0.2.10", purpose="bmc", transport="ssh",
            credentials=self.credentials)["code"], "legacy_equivalence_unproven")
        self.assertIsNone(LocalConfigurationStore(self.path, kind="targets").status()["revision"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_overlay_can_save_a_custom_port_without_replacing_old_ports(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = b"OPENUBMC_SSH_USER=old\nOPENUBMC_SSH_PASSWORD=old-secret\n"
        self.path.write_bytes(original)
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        custom = ResolvedSshCredentials(user="custom", password="custom-secret", port=2222)
        self.assertEqual(self.remember(credentials=custom)["code"], "saved")
        self.assertEqual(self.selected(port=2222).password, "custom-secret")
        self.assertEqual(self.selected(port=22).password, "old-secret")
        self.assertEqual(self.selected(host="192.0.2.11", port=2222).password, "old-secret")
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_source_change_during_authentication_is_a_conflict(self):
        self.path = Path(self.temp.name) / "credentials.env"
        self.path.write_text("OPENUBMC_SSH_USER=old\nOPENUBMC_SSH_PASSWORD=old-secret\n")
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        bound = self.memory.for_connection()
        self.path.write_text("OPENUBMC_SSH_USER=changed\nOPENUBMC_SSH_PASSWORD=changed-secret\n")
        self.assertEqual(bound.remember(host="192.0.2.10", purpose="bmc", transport="ssh",
                                        credentials=self.credentials)["code"], "configuration_changed")
        self.assertIsNone(LocalConfigurationStore(self.path, kind="targets").status()["revision"])

    def test_legacy_source_change_while_saving_is_a_conflict(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = "OPENUBMC_SSH_USER=old\nOPENUBMC_SSH_PASSWORD=old-secret\n"
        self.path.write_text(original)
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        bound = self.memory.for_connection()
        actual_save = LocalConfigurationStore.save_and_activate

        def change_before_lock(store, config, **kwargs):
            self.path.write_text("OPENUBMC_SSH_USER=new\nOPENUBMC_SSH_PASSWORD=new-secret\n")
            return actual_save(store, config, **kwargs)

        with patch.object(LocalConfigurationStore, "save_and_activate", new=change_before_lock):
            result = bound.remember(host="192.0.2.10", purpose="bmc", transport="ssh",
                                    credentials=self.credentials)
        self.assertEqual(result["code"], "configuration_changed")
        self.assertIsNone(LocalConfigurationStore(self.path, kind="targets").status()["revision"])

    def test_only_authenticating_redfish_sessions_save(self):
        for authenticated in (False, True):
            selector = CredentialSelector.for_redfish(user="fixture", user_env="", password_env="", environ={})
            target = TargetSpec(host="192.0.2.10", redfish_port=8443,
                                credential_selector_fingerprint=selector.fingerprint)
            task = OpenUBMCTaskRun(task_id="redfish", credential_resolver=CredentialResolver(
                redfish_loader=lambda _: ResolvedRedfishCredentials(
                    user="fixture", password="fixture-secret", port=8443)))
            self.addCleanup(task.close)
            transport = FakeRedfishTransport()
            transport.authenticates_on_open = authenticated
            with credential_memory_scope(self.memory):
                lane = task.redfish_lane(target=target, credential_selector=selector,
                                         lease_name="fixture", transport=transport)
            lane.request("read", replay_safe=True)
            self.assertEqual(self.store.status()["revision"] is not None, authenticated)
            if authenticated:
                self.assertEqual(self.selected(transport="redfish", port=8443).password,
                                 "fixture-secret")
                projected = CredentialResolver(config_path=self.path, environ={}).resolve_local_values(
                    task_id="fresh-redfish-task", host="192.0.2.10",
                    arguments={"redfish_port": 8443}, transports=("redfish",))
                self.assertEqual(projected["OPENUBMC_REDFISH_PASSWORD"], "fixture-secret")
                for transport, port in (("redfish", 443), ("ssh", 8443)):
                    with self.assertRaises(Exception) as missing:
                        self.selected(transport=transport, port=port)
                    self.assertEqual(missing.exception.code, "credentials_missing")
                self.assertNotIn("fixture-secret", json.dumps(lane.status()))

    def test_public_mcp_request_enables_memory_in_its_worker_without_extra_probe(self):
        from concurrent.futures import ThreadPoolExecutor
        from openubmc_target_runtime import RuntimeMcpService
        from test_agent_gateway import SemanticBackend
        credentials = self.credentials
        transport = FakeSshTransport()

        class ConnectedBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                selector = CredentialSelector.for_ssh(user="fixture", user_env="", password_env="",
                                                      identity_file="", environ={})
                target = TargetSpec(host=arguments["ip"], credential_selector_fingerprint=selector.fingerprint)
                owner = OpenUBMCTaskRun(task_id=task.task_id, credential_resolver=CredentialResolver(
                    ssh_loader=lambda _: credentials))
                try:
                    lane = owner.ssh_lane(target=target, credential_selector=selector,
                                          lease_name="fixture", transport=transport)
                    lane.run_channel("fixture", replay_safe=True, timeout=1)
                    return super().observe_query(task, arguments, context)
                finally:
                    owner.close()

        service = RuntimeMcpService(ConnectedBackend(), credential_memory=self.memory)
        self.addCleanup(service.close)
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(service.call_exposed_tool, "observe", {
                "target": "192.0.2.10", "selectors": [{"kind": "capability", "names": ["ssh"]}],
            }, task_id="auto-remember", operation_id="one").result(timeout=5)
        self.assertEqual(self.selected().password, "fixture-secret")
        self.assertEqual(transport.opens, 1)
        self.assertNotIn("fixture-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
