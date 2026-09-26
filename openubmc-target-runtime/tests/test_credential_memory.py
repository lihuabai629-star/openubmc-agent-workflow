from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openubmc_target_runtime import (
    CredentialResolver, CredentialSelector, OpenUBMCTaskRun,
    ResolvedSshCredentials, ResolvedRedfishCredentials, TargetSpec,
)
from openubmc_target_runtime.configuration import LocalConfigurationStore
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

    def selected(self, host="192.0.2.10", purpose="bmc", transport="ssh"):
        return CredentialResolver(config_path=self.path, environ={}).resolve_local(
            task_id="new-task", host=host, purpose=purpose, transport=transport,
        ).credentials

    def lane(self, transport=None):
        selector = CredentialSelector.for_ssh(user="fixture", user_env="", password_env="",
                                              identity_file="", environ={})
        target = TargetSpec(host="192.0.2.10", credential_selector_fingerprint=selector.fingerprint)
        task = OpenUBMCTaskRun(task_id="fixture", credential_resolver=CredentialResolver(
            ssh_loader=lambda _: self.credentials))
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

    def test_legacy_file_and_its_precedence_are_not_silently_migrated(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = "OPENUBMC_SSH_USER=old\nOPENUBMC_SSH_PASSWORD=old-secret\n"
        self.path.write_text(original)
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        self.assertEqual(self.remember()["code"], "legacy_source_not_migrated")
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(self.selected().password, "old-secret")
        self.assertEqual(self.selected(host="192.0.2.11").password, "old-secret")

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

    def test_hostname_and_custom_port_do_not_overwrite_an_ip_default(self):
        for kwargs in ({"host": "bmc.example"}, {"credentials": ResolvedSshCredentials(
                user="fixture", password="fixture-secret", port=2222)}):
            self.assertEqual(self.remember(**kwargs)["code"], "unsupported_scope")
        self.assertIsNone(self.store.status()["revision"])

    def test_legacy_telnet_settings_are_not_hidden_by_automatic_migration(self):
        self.path = Path(self.temp.name) / "credentials.env"
        original = "OPENUBMC_SSH_USER=old\nOPENUBMC_SSH_PASSWORD=old-secret\nOPENUBMC_TELNET_PASSWORD=telnet-secret\n"
        self.path.write_text(original)
        self.path.chmod(0o600)
        self.memory = VerifiedCredentialMemory(config_path=self.path, environ={})
        self.assertEqual(self.remember()["code"], "legacy_source_not_migrated")
        self.assertEqual(self.path.read_text(), original)
        self.assertIsNone(LocalConfigurationStore(self.path, kind="targets").status()["revision"])

    def test_only_authenticating_redfish_sessions_save(self):
        for authenticated in (False, True):
            selector = CredentialSelector.for_redfish(user="fixture", user_env="", password_env="", environ={})
            target = TargetSpec(host="192.0.2.10", credential_selector_fingerprint=selector.fingerprint)
            task = OpenUBMCTaskRun(task_id="redfish", credential_resolver=CredentialResolver(
                redfish_loader=lambda _: ResolvedRedfishCredentials(user="fixture", password="fixture-secret")))
            self.addCleanup(task.close)
            transport = FakeRedfishTransport()
            transport.authenticates_on_open = authenticated
            with credential_memory_scope(self.memory):
                lane = task.redfish_lane(target=target, credential_selector=selector,
                                         lease_name="fixture", transport=transport)
            lane.request("read", replay_safe=True)
            self.assertEqual(self.store.status()["revision"] is not None, authenticated)

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
