"""Synthetic-secret checks at local process, task, and durable boundaries."""
from __future__ import annotations

from dataclasses import replace
from concurrent.futures import Future
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openubmc_target_runtime import (
    CredentialResolver, CredentialSelector, MutationRequest, OrchestratedMcpBackend,
    OpenUBMCTaskRun, RemoteReadRequest, ResolvedSshCredentials, TargetSpec,
)
from openubmc_target_runtime.host_continuity import HostContinuity
from openubmc_target_runtime.effect_runner import (
    EffectExecution, EffectIntent, EffectRunMode, EffectSettlementMode,
)
from openubmc_target_runtime.capability import EffectClass
from openubmc_target_runtime.mutation import TaskAuthorizationPolicy
from openubmc_target_runtime.run_engine import RunEngine
from openubmc_target_runtime.openssh import (
    OpenSshControlMasterTransport, OpenSshMaster, OpenSshMasterError, OpenSshUnavailable,
)
from openubmc_target_runtime.redaction import (
    SecretMaterialError, redact_text, register_secret_values, secret_redaction_request,
)
from openubmc_target_runtime.session_outcome import (
    InMemorySessionOutcomeRepository, SessionOutcomeRecord, SQLiteSessionOutcomeRepository,
)
from openubmc_target_runtime.terminal_delivery import TerminalAnswerStore
from openubmc_target_runtime.run_store import RunDecision, RunEvent, RunEventSchemaError
from openubmc_target_runtime.semantic_runtime import RunTurn, fingerprint


class CredentialContainmentTests(unittest.TestCase):
    def test_redaction_fast_path_keeps_plain_text_but_not_secret_forms(self) -> None:
        self.assertEqual(redact_text("run-123/source"), "run-123/source")
        self.assertEqual(redact_text("password=synthetic-secret"), "password=<redacted>")
        self.assertEqual(redact_text("Bearer synthetic-token"), "Bearer <redacted>")
        self.assertEqual(redact_text("ssh://user:synthetic@host"), "ssh://<redacted>@host")
        self.assertEqual(redact_text("--password synthetic-secret"), "--password <redacted>")
        with secret_redaction_request():
            register_secret_values({"password": "syntheticplain"})
            self.assertEqual(redact_text("prefixsyntheticplainsuffix"), "prefix<redacted>suffix")

    def test_selector_field_rejects_inline_value_before_fingerprinting(self) -> None:
        inline = "synthetic-inline-password"
        with self.assertRaises(ValueError) as failure:
            CredentialSelector.for_ssh(
                user="fixture", user_env="", password_env=inline,
                identity_file="", environ={},
            )
        self.assertNotIn(inline, str(failure.exception))

    def test_raw_credential_cannot_become_a_read_or_mutation_digest(self) -> None:
        selector = CredentialSelector.for_ssh(
            user="fixture", user_env="", password_env="", identity_file="",
            environ={},
        )
        target = TargetSpec("192.0.2.10", credential_selector_fingerprint=selector.fingerprint)
        with self.assertRaises(SecretMaterialError):
            RemoteReadRequest.create(
                request_id="read", target=target, credential_selector=selector,
                collector_name="fixture", operation={"password": "synthetic-digest-secret"},
            )
        with self.assertRaises(SecretMaterialError):
            MutationRequest.create(
                operation_id="mutate", target=target, credential_selector=selector,
                action="live_patch", operation={"password": "synthetic-digest-secret"},
            )

    def test_run_decision_rejects_inline_secret_before_effect_persistence(self) -> None:
        with self.assertRaises(SecretMaterialError):
            RunDecision(
                run_id="run", command_id="command", input_digest="a" * 64,
                expected_revision=0, events=(), turn=RunTurn(run_id="run", state="running"),
                effect_intent={"arguments": {"password": "synthetic-effect-secret"}},
            )

    def test_run_event_redacts_secret_before_public_or_durable_projection(self) -> None:
        secret = "synthetic-event-secret"
        event = RunEvent(
            "OperationTerminal",
            {"canonical_error": {"code": "token=" + secret, "message": "failed"}},
            "effect",
        )
        self.assertNotIn(secret, json.dumps(event.to_public_dict()))
        self.assertNotIn(secret, json.dumps(event.for_persistence().payload))

    def test_case_opened_preserves_only_validated_authorization_policy(self) -> None:
        policy = TaskAuthorizationPolicy.from_task_intent("diagnosis-only").to_public_dict()
        opened = RunEvent("CaseOpened", {"authorization": policy})
        self.assertEqual(opened.payload["authorization"], policy)
        with self.assertRaises(RunEventSchemaError):
            RunEvent(
                "CaseOpened",
                {"authorization": {**policy, "password": "synthetic-policy-secret"}},
            )

    def test_run_turn_rejects_inline_secret_before_decision_persistence(self) -> None:
        with self.assertRaises(SecretMaterialError):
            RunDecision(
                run_id="run", command_id="command", input_digest="a" * 64,
                expected_revision=0, events=(),
                turn=RunTurn(
                    run_id="run", state="running",
                    next_action="password=synthetic-turn-secret",
                ),
            )

    def test_effect_result_digest_excludes_exception_secret(self) -> None:
        secret = "synthetic-effect-error-secret"
        intent = EffectIntent(
            run_id="run", effect_id="effect", operation="observe",
            effect_class=EffectClass.READ_ONLY,
            request_fingerprint="a" * 64, arguments={},
        )
        error = RuntimeError("password=" + secret)
        error.code = "token=" + secret
        error.recovery_status = {"message": "Authorization: Bearer " + secret}
        future = Future()
        future.set_exception(error)
        execution = EffectExecution(
            future=future, mode=EffectRunMode.DISPATCH,
            settlement_generation=0,
        )
        engine = RunEngine.__new__(RunEngine)
        committed = object()
        engine._commit_run_decision = lambda **_kwargs: committed
        observed = []

        def checked_fingerprint(value):
            self.assertNotIn(secret, json.dumps(value))
            observed.append(value)
            return fingerprint(value)

        with patch(
            "openubmc_target_runtime.run_engine.fingerprint",
            side_effect=checked_fingerprint,
        ):
            self.assertIs(
                engine._commit_effect_result(
                    intent, execution, settlement_mode=EffectSettlementMode.DISPATCH,
                ),
                committed,
            )
        self.assertEqual(len(observed), 1)
        self.assertIn("<redacted>", json.dumps(observed[0]))

    def test_task_status_does_not_replay_exception_text_from_state_store(self) -> None:
        secret = "synthetic-store-error-secret"

        class FailingStore:
            def load(self, _task_id):
                raise RuntimeError("storage error echoed " + secret)

            def delete(self, _task_id):
                return False

        class Backend:
            def debug_run(self, *_args):
                return {}

        backend = OrchestratedMcpBackend({"debug_run": Backend()}, state_store=FailingStore())
        task = backend.open_task("task")
        try:
            status = backend.task_status(task)
            self.assertEqual(status["task_context"]["last_error"], "RuntimeError")
            self.assertNotIn(secret, json.dumps(status))
        finally:
            backend.close_task(task)

    def test_failed_lookup_pins_source_until_task_is_forgotten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            incomplete = root / "incomplete.env"
            complete = root / "complete.env"
            incomplete.write_text("OPENUBMC_SSH_USER=fixture\n", encoding="utf-8")
            complete.write_text(
                "OPENUBMC_SSH_USER=fixture\nOPENUBMC_SSH_PASSWORD=synthetic-file-secret\n",
                encoding="utf-8",
            )
            incomplete.chmod(0o600)
            complete.chmod(0o600)
            environment = {"OPENUBMC_CREDENTIALS_FILE": str(incomplete)}
            resolver = CredentialResolver(environ=environment)
            lookup = dict(task_id="task", host="192.0.2.10", transport="ssh")

            with self.assertRaises(Exception) as missing:
                resolver.resolve_local(**lookup)
            self.assertEqual(missing.exception.code, "credentials_missing")
            environment["OPENUBMC_CREDENTIALS_FILE"] = str(complete)
            with self.assertRaises(Exception) as still_missing:
                resolver.resolve_local(**lookup)
            self.assertEqual(still_missing.exception.code, "credentials_missing")

            resolver.forget_task("task")
            resolved = resolver.resolve_local(**lookup)
            self.assertEqual(resolved.credentials.password, "synthetic-file-secret")
            resolver.forget_task("task")
            self.assertFalse(any(key[0] == "task" for key in resolver._local_cache))

    def test_runtime_task_close_releases_direct_selector_cache(self) -> None:
        selector = CredentialSelector.for_ssh(
            user="fixture", user_env="", password_env="", identity_file="",
            environ={},
        )
        target = TargetSpec("192.0.2.10", credential_selector_fingerprint=selector.fingerprint)
        resolver = CredentialResolver(
            lambda _selector: ResolvedSshCredentials(user="fixture", password="synthetic-direct-secret")
        )
        task = OpenUBMCTaskRun(task_id="task", credential_resolver=resolver)
        resolver.resolve(task_id="task", target=target, selector=selector)
        self.assertTrue(any(key[0] == "task" for key in resolver._cache))
        task.close()
        self.assertFalse(any(key[0] == "task" for key in resolver._cache))

    def test_cached_local_resolution_registers_secret_for_exception_redaction(self) -> None:
        secret = "synthetic-task-cache-secret"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "credentials.env"
            path.write_text(
                f"OPENUBMC_SSH_USER=fixture\nOPENUBMC_SSH_PASSWORD={secret}\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            resolver = CredentialResolver(config_path=path, environ={})
            lookup = dict(task_id="task", host="192.0.2.10", transport="ssh")
            resolver.resolve_local(**lookup)
            with self.assertRaises(RuntimeError) as failure:
                with secret_redaction_request():
                    self.assertTrue(resolver.resolve_local(**lookup).cache_hit)
                    raise RuntimeError("transport echoed " + secret)
            self.assertNotIn(secret, str(failure.exception))

    def test_controlled_child_sees_password_only_on_stdin_and_cannot_echo_it(self) -> None:
        secret = "synthetic-ssh-stdin-secret"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "child-report.json"
            child = root / "sshpass"
            child.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\nfrom pathlib import Path\n"
                "received = sys.stdin.read()\n"
                f"Path({str(report)!r}).write_text(json.dumps({{'argv': sys.argv, 'env': dict(os.environ), 'stdin_length': len(received)}}))\n"
                "sys.stderr.write('child echoed ' + received)\n"
                "raise SystemExit(5)\n",
                encoding="utf-8",
            )
            child.chmod(0o700)
            ssh = root / "ssh"
            ssh.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            ssh.chmod(0o700)
            target = TargetSpec("192.0.2.10", credential_selector_fingerprint="0" * 64)
            credentials = ResolvedSshCredentials(user="fixture", password=secret)
            transport = OpenSshControlMasterTransport()
            with patch.dict(os.environ, {
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "OPENUBMC_SSH_PASSWORD": "synthetic-inherited-secret",
            }):
                with self.assertRaises(OpenSshMasterError) as failure:
                    transport.open_master(target=target, credentials=credentials)
            observed = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(observed["stdin_length"], len(secret) + 1)
            self.assertNotIn(secret, json.dumps(observed))
            self.assertNotIn("OPENUBMC_SSH_PASSWORD", observed["env"])
            self.assertNotIn(secret, str(failure.exception))
            self.assertNotIn(secret, repr(failure.exception.result))
            self.assertIn("<redacted>", str(failure.exception))
            self.assertEqual(failure.exception.result.returncode, 5)

    def test_child_command_cannot_embed_selected_password(self) -> None:
        secret = "synthetic-command-secret"
        with tempfile.TemporaryDirectory() as raw:
            temporary = tempfile.TemporaryDirectory(dir=raw)
            self.addCleanup(temporary.cleanup)
            master = OpenSshMaster(
                target=TargetSpec("192.0.2.10", credential_selector_fingerprint="0" * 64),
                credentials=ResolvedSshCredentials(user="fixture", password=secret),
                control_path=str(Path(raw) / "control.sock"), tempdir=temporary,
            )
            with patch("openubmc_target_runtime.openssh.subprocess.run") as run:
                with self.assertRaises(SecretMaterialError):
                    OpenSshControlMasterTransport().run_channel(master, "echo " + secret)
            run.assert_not_called()

    def test_launch_exception_does_not_echo_selected_password(self) -> None:
        secret = "synthetic-launch-error-secret"
        target = TargetSpec("192.0.2.10", credential_selector_fingerprint="0" * 64)
        credentials = ResolvedSshCredentials(user="fixture", password=secret)
        with (
            patch("openubmc_target_runtime.openssh.shutil.which", return_value="/usr/bin/fake"),
            patch("openubmc_target_runtime.openssh.subprocess.run", side_effect=OSError(secret)),
        ):
            with self.assertRaises(OpenSshUnavailable) as failure:
                OpenSshControlMasterTransport().open_master(
                    target=target, credentials=credentials,
                )
        self.assertNotIn(secret, str(failure.exception))

    def test_channel_child_output_is_redacted_without_request_context(self) -> None:
        secret = "synthetic-channel-echo-secret"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = root / "ssh"
            child.write_text(
                f"#!{sys.executable}\nimport sys\n"
                f"sys.stdout.write('child echoed ' + {secret!r})\n"
                f"sys.stderr.write('remote error ' + {secret!r})\n",
                encoding="utf-8",
            )
            child.chmod(0o700)
            temporary = tempfile.TemporaryDirectory(dir=raw)
            self.addCleanup(temporary.cleanup)
            master = OpenSshMaster(
                target=TargetSpec("192.0.2.10", credential_selector_fingerprint="0" * 64),
                credentials=ResolvedSshCredentials(user="fixture", password=secret),
                control_path=str(root / "control.sock"), tempdir=temporary,
            )
            with patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}):
                result = OpenSshControlMasterTransport().run_channel(master, "true")
            self.assertEqual(result.returncode, 0)
            self.assertNotIn(secret, repr(result))
            self.assertIn("<redacted>", result.stdout)

    def test_channel_launch_exception_does_not_echo_selected_password(self) -> None:
        secret = "synthetic-channel-launch-secret"
        with tempfile.TemporaryDirectory() as raw:
            temporary = tempfile.TemporaryDirectory(dir=raw)
            self.addCleanup(temporary.cleanup)
            master = OpenSshMaster(
                target=TargetSpec("192.0.2.10", credential_selector_fingerprint="0" * 64),
                credentials=ResolvedSshCredentials(user="fixture", password=secret),
                control_path=str(Path(raw) / "control.sock"), tempdir=temporary,
            )
            with patch(
                "openubmc_target_runtime.openssh.subprocess.run",
                side_effect=OSError(secret),
            ):
                with self.assertRaises(OpenSshUnavailable) as failure:
                    OpenSshControlMasterTransport().run_channel(master, "true")
            self.assertNotIn(secret, str(failure.exception))

    def test_host_notes_reject_secret_before_creating_store(self) -> None:
        secret = "synthetic-note-secret"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "host"
            store = HostContinuity(root)
            with self.assertRaises(SecretMaterialError):
                store.save_notes("task", {"goal": "password=" + secret})
            with self.assertRaises(SecretMaterialError):
                store.save_notes("task", {"hypotheses": [{"api_key": secret}]})
            self.assertFalse(root.exists())

    def test_session_outcome_repositories_reject_unredacted_direct_save(self) -> None:
        secret = "synthetic-outcome-secret"
        safe = SessionOutcomeRecord(
            outcome_id="outcome", session_id="task", case_id="case",
            replay_fingerprint="fixture", workflow="diagnosis", domain="debug",
            outcome="completed", gap_type="", summary="safe", details={},
            architecture_decision=False, review_state="recorded", reviewer="",
            approver="", promotion=None, created_at=1.0, updated_at=1.0,
        )
        with tempfile.TemporaryDirectory() as raw:
            for repository in (
                InMemorySessionOutcomeRepository(),
                SQLiteSessionOutcomeRepository(Path(raw) / "outcomes.sqlite3"),
            ):
                with self.subTest(repository=type(repository).__name__):
                    with self.assertRaises(SecretMaterialError):
                        repository.save(replace(safe, details={"password": secret}))
                    self.assertEqual(repository.list(), ())

    def test_terminal_answer_rejects_secret_before_hash_or_write(self) -> None:
        secret = "synthetic-terminal-secret"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "answer.json"
            store = TerminalAnswerStore(path)
            safe_outcome = {"status": "completed", "summary": "safe"}
            with self.assertRaises(SecretMaterialError):
                store.prepare(
                    task_id="task", run_id="run",
                    outcome={**safe_outcome, "password": secret},
                    delivery_stage="diagnosed", text="safe",
                )
            with self.assertRaises(SecretMaterialError):
                store.prepare(
                    task_id="task", run_id="run", outcome=safe_outcome,
                    delivery_stage="diagnosed", text="Authorization: Bearer " + secret,
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
