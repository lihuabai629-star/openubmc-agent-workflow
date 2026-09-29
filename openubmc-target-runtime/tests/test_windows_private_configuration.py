"""Native Windows checks at the public local-configuration boundary."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class WindowsPrivateConfigurationTests(unittest.TestCase):
    def test_private_revision_resolves_bmc_and_associated_os_without_leaking_secrets(self):
        from openubmc_target_runtime import CredentialResolver
        from openubmc_target_runtime.configuration import LocalConfigurationStore

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            source = Path(raw) / "openubmc" / "credentials.json"
            store = LocalConfigurationStore(source, kind="targets")
            config = {
                "schema_version": 1,
                "credentials": {
                    "common": {"user": "admin", "password": "fixture-private-bmc"},
                    "os": {"user": "operator", "password": "fixture-private-os"},
                    "override": {"user": "special", "password": "fixture-private-override"},
                },
                "defaults": {"bmc": {"ssh": "common", "redfish": "common"}, "os": {"ssh": "os"}},
                "targets": {"192.0.2.10": {"bmc": {"ssh": "override"}}},
                "devices": {"192.0.2.10": {"os_ip": "192.0.2.20"}},
            }
            saved = store.save(config, expected_revision=None)
            activated = store.activate(saved["revision"], expected_active_revision=None)
            resolver = CredentialResolver(config_path=source, environ={})

            self.assertEqual(activated["active_revision"], saved["revision"])
            self.assertEqual(store.read_active(), config)
            self.assertEqual(resolver.associated_os(task_id="native", bmc_host="192.0.2.10"), "192.0.2.20")
            self.assertEqual(resolver.resolve_local(task_id="native", host="192.0.2.10", transport="ssh").credentials.password,
                             "fixture-private-override")
            self.assertEqual(resolver.resolve_local(task_id="native", host="192.0.2.11", transport="redfish").credentials.password,
                             "fixture-private-bmc")
            self.assertEqual(resolver.resolve_local(task_id="native", host="192.0.2.20", purpose="os", transport="ssh").credentials.password,
                             "fixture-private-os")
            self.assertNotIn("fixture-private", json.dumps(store.status()))

    def test_default_source_uses_native_local_app_data_without_wsl(self):
        from openubmc_target_runtime.credential_file import configuration_home
        from openubmc_target_runtime.credentials import LocalCredentialSource

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            source = Path(raw) / "openubmc" / "credentials.json"
            self.assertEqual(configuration_home({"LOCALAPPDATA": raw}), Path(raw))
            self.assertIsNone(LocalCredentialSource(environ={"LOCALAPPDATA": raw}).select_path())
            source.parent.mkdir()
            source.write_text('{"schema_version":1,"credentials":{}}', encoding="utf-8")
            self.assertEqual(LocalCredentialSource(environ={"LOCALAPPDATA": raw}).select_path(), source)

    def test_permissive_windows_acl_cannot_become_an_active_credential_source(self):
        from openubmc_target_runtime.configuration import ConfigurationError, LocalConfigurationStore

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            source = Path(raw) / "openubmc" / "credentials.json"
            store = LocalConfigurationStore(source, kind="targets")
            saved = store.save({"schema_version": 1, "credentials": {}}, expected_revision=None)
            snapshot = source.parent / ".credentials.json.revisions" / (saved["revision"] + ".json")
            changed = subprocess.run(["icacls.exe", str(snapshot), "/grant", "*S-1-5-32-545:(R)"],
                                     capture_output=True, text=True)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            with self.assertRaises(ConfigurationError):
                store.activate(saved["revision"], expected_active_revision=None)
            self.assertIsNone(store.status()["active_revision"])

    def test_second_process_lock_blocks_activation_without_changing_the_active_revision(self):
        from openubmc_target_runtime.configuration import ConfigurationError, LocalConfigurationStore

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            source = Path(raw) / "openubmc" / "credentials.json"
            store = LocalConfigurationStore(source, kind="targets")
            first = store.save({"schema_version": 1, "credentials": {}}, expected_revision=None)
            store.activate(first["revision"], expected_active_revision=None)
            lock = source.with_name(".credentials.json.lock")
            holder = subprocess.Popen(
                [sys.executable, "-B", "-c",
                 "import msvcrt,os,sys; f=os.open(sys.argv[1],os.O_RDWR); "
                 "os.lseek(f,0,0); msvcrt.locking(f,msvcrt.LK_LOCK,1); "
                 "print('locked',flush=True); sys.stdin.read(1); "
                 "os.lseek(f,0,0); msvcrt.locking(f,msvcrt.LK_UNLCK,1); os.close(f)",
                 str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, creationflags=0x08000000,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaises(ConfigurationError):
                    store.save_and_activate({"schema_version": 1, "credentials": {}},
                                            expected_revision=first["revision"],
                                            expected_active_revision=first["revision"],
                                            expected_source_text=None, blocking=False)
                self.assertEqual(store.status()["active_revision"], first["revision"])
            finally:
                holder.communicate("x", timeout=5)

    def test_failed_second_marker_write_rolls_back_both_revisions(self):
        import openubmc_target_runtime.configuration as module
        from openubmc_target_runtime.configuration import LocalConfigurationStore

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            source = Path(raw) / "openubmc" / "credentials.json"
            store = LocalConfigurationStore(source, kind="targets")
            original = {"schema_version": 1, "credentials": {"first": {"user": "a", "password": "private-first"}}}
            first = store.save(original, expected_revision=None)
            store.activate(first["revision"], expected_active_revision=None)
            actual_write = module._atomic_write

            def interrupted(path, content):
                if path.name == ".credentials.json.active.json":
                    raise OSError("fixture interruption")
                return actual_write(path, content)

            with patch.object(module, "_atomic_write", side_effect=interrupted):
                with self.assertRaises(OSError):
                    store.save_and_activate({"schema_version": 1, "credentials": {}},
                                            expected_revision=first["revision"],
                                            expected_active_revision=first["revision"],
                                            expected_source_text=None)
            self.assertEqual(store.status()["revision"], first["revision"])
            self.assertEqual(store.status()["active_revision"], first["revision"])
            self.assertEqual(store.read_active(), original)

    def test_mutation_journal_rejects_a_permissive_windows_access_list(self):
        from openubmc_target_runtime.mutation import MutationJournal, MutationJournalCorrupt, MutationJournalStore
        from openubmc_target_runtime.windows_private import verify_private_path

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            store = MutationJournalStore(Path(raw)/"journals")
            journal = MutationJournal(
                task_id="fixture-task", operation_id="fixture-effect",
                operation_fingerprint="a" * 64, action="live_patch",
                original_intent="live-patch", target_fingerprint="b" * 64,
                target_identity=None, epoch_before=0, stage="planned",
                effects_started=False,
            )
            store.create(journal)
            path = store._path(journal.task_id, journal.operation_id)
            verify_private_path(store.root)
            verify_private_path(path)
            changed = subprocess.run(["icacls.exe", str(path), "/grant", "*S-1-5-32-545:(R)"],
                                     capture_output=True, text=True)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            with self.assertRaises(MutationJournalCorrupt):
                store.load(journal.task_id, journal.operation_id)

    def test_run_and_host_state_reject_permissive_windows_access_lists(self):
        from openubmc_target_runtime.context_runtime import SQLiteRuntimeRepository
        from openubmc_target_runtime.host_continuity import HostContinuity
        from openubmc_target_runtime.terminal_delivery import TerminalAnswerError, TerminalAnswerStore
        from openubmc_target_runtime.windows_private import WindowsPrivateError, verify_private_path

        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            root = Path(raw)
            run_path = root/"run"/"runtime.sqlite3"
            repository = SQLiteRuntimeRepository(run_path)
            try:
                verify_private_path(run_path)
                changed = subprocess.run(["icacls.exe", str(run_path), "/grant", "*S-1-5-32-545:(R)"],
                                         capture_output=True, text=True)
                self.assertEqual(changed.returncode, 0, changed.stderr)
                with self.assertRaises(WindowsPrivateError):
                    repository.current_revision("fixture-run")
            finally:
                repository.close()

            host = HostContinuity(root/"host")
            with host._database():
                pass
            host_path = host.root/"bookmarks.sqlite3"
            verify_private_path(host_path)
            changed = subprocess.run(["icacls.exe", str(host_path), "/grant", "*S-1-5-32-545:(R)"],
                                     capture_output=True, text=True)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            with self.assertRaises(WindowsPrivateError):
                with host._database():
                    pass

            answer_path = root/"answer"/"delivery.json"
            answers = TerminalAnswerStore(answer_path)
            answers.prepare(task_id="fixture-task", run_id="fixture-run",
                            outcome={"status": "completed", "summary": "fixture"},
                            delivery_stage="unverified", text="fixture result")
            verify_private_path(answer_path)
            changed = subprocess.run(["icacls.exe", str(answer_path), "/grant", "*S-1-5-32-545:(R)"],
                                     capture_output=True, text=True)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            with self.assertRaises(TerminalAnswerError):
                answers.get("fixture-task")


if __name__ == "__main__":
    unittest.main()
