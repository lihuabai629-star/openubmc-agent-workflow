from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    MutationAuthorization,
    MutationAuthorizationDenied,
    MutationEffectsRejected,
    MutationJournal,
    MutationJournalCorrupt,
    MutationJournalStore,
    MutationRequest,
    MutationVerificationTerminalFailure,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    ResolvedSshCredentials,
    TargetIdentity,
    TargetSpec,
    UnfinishedMutationExists,
)


class MutationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.authorization = MutationAuthorization.from_task_intent(
            "diagnose-and-fix",
            delivery_strategy="live-patch",
        )

    def request(self, operation_id: str = "patch-1") -> MutationRequest:
        return MutationRequest.create(
            operation_id=operation_id,
            target=self.target,
            credential_selector=self.selector,
            action="live_patch",
            operation={
                "remote": "/opt/bmc/apps/demo/unit.lua",
                "sha256": "b" * 64,
            },
        )

    def read_request(self, request_id: str) -> RemoteReadRequest:
        return RemoteReadRequest.create(
            request_id=request_id,
            target=self.target,
            credential_selector=self.selector,
            collector_name="debug-freshness",
            operation={"request": request_id},
        )

    def task(self, store: MutationJournalStore) -> OpenUBMCTaskRun:
        return OpenUBMCTaskRun(
            task_id="recovery-task",
            credential_resolver=CredentialResolver(
                lambda _selector: ResolvedSshCredentials(
                    user="root",
                    password="must-not-be-persisted",
                )
            ),
            mutation_journal_store=store,
        )

    def test_sigkill_crash_cuts_preserve_identity_and_never_repeat_the_mutation(self) -> None:
        helper = RUNTIME_ROOT / "tests" / "helpers" / "mutation_crash_worker.py"
        for cut in ("effect_started", "result_persisted", "terminal_committed"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                process = subprocess.Popen(
                    [sys.executable, str(helper), str(root), cut],
                    cwd=RUNTIME_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.monotonic() + 5
                marker = root / "marker"
                while not marker.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail(f"crash worker did not reach {cut}")
                    time.sleep(0.01)
                self.assertIsNone(process.poll())
                process.kill()
                process.wait(timeout=5)
                process.communicate(timeout=1)
                self.assertLess(process.returncode, 0)

                store = MutationJournalStore(root / "journals")
                journal = store.load("crash-task", "crash-effect-1")
                self.assertIsNotNone(journal)
                self.assertEqual(journal.operation_id, "crash-effect-1")
                self.assertEqual((root / "apply-count").read_text(), "1")
                restarted = OpenUBMCTaskRun(
                    task_id="crash-task",
                    credential_resolver=CredentialResolver(
                        lambda _selector: ResolvedSshCredentials(
                            user="root",
                            password="test",
                        )
                    ),
                    mutation_journal_store=store,
                )
                if cut == "terminal_committed":
                    replay = restarted.run_mutation(
                        self.request("crash-effect-1"),
                        authorization=self.authorization,
                        apply=lambda _context: self.fail(
                            "terminal crash replay must not repeat the mutation"
                        ),
                        verify=lambda _context: self.fail(
                            "terminal crash replay must not verify again"
                        ),
                    )
                    self.assertTrue(replay.idempotent_replay)
                    self.assertEqual(replay.journal.stage, "verified")
                else:
                    recovery = restarted.recover_mutation(
                        self.request("crash-effect-1"),
                        authorization=self.authorization,
                        inspect=lambda _context: {
                            "remote_checksum": "b" * 64,
                            "root_mount_restored": True,
                            "restart_observed": False,
                        },
                        verify=lambda context: context.run_read(
                            self.read_request(f"crash-{cut}-verify"),
                            lambda read_context: read_context.epochs.target_epoch,
                        ),
                    )
                    if cut == "effect_started":
                        self.assertEqual(recovery.decision, "manual")
                        self.assertEqual(recovery.journal.stage, "recovery_blocked")
                    else:
                        self.assertEqual(recovery.decision, "verify")
                        self.assertEqual(recovery.journal.stage, "verified")
                self.assertEqual((root / "apply-count").read_text(), "1")

    def test_journal_rejects_invalid_authorized_recovery_actions(self) -> None:
        journal = MutationJournal(
            task_id="recovery-task",
            operation_id="patch-1",
            operation_fingerprint=self.request().fingerprint,
            action="live_patch",
            original_intent="diagnose-and-fix",
            target_fingerprint=self.target.fingerprint,
            target_identity=None,
            epoch_before=0,
        ).to_public_dict()

        for actions in ([""], [1], ["live_patch"]):
            with self.subTest(actions=actions):
                serialized = dict(journal)
                serialized["authorized_recovery_actions"] = actions
                with self.assertRaises(MutationJournalCorrupt):
                    MutationJournal.from_public_dict(serialized)

    def test_completed_operation_retry_is_idempotent_and_does_not_apply_twice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            task = self.task(store)
            apply_count = 0

            def apply(_context):
                nonlocal apply_count
                apply_count += 1
                return {
                    "remote_before_sha256": "a" * 64,
                    "remote_after_sha256": "b" * 64,
                    "root_mount_restored": True,
                    "backup": "/tmp/unit.lua.bak",
                }

            first = task.run_mutation(
                self.request(),
                authorization=self.authorization,
                apply=apply,
                verify=lambda context: context.run_read(
                    self.read_request("verify-first"),
                    lambda read_context: read_context.epochs.target_epoch,
                ),
            )
            restarted = self.task(store)
            retry = restarted.run_mutation(
                self.request(),
                authorization=self.authorization,
                apply=apply,
                verify=lambda _context: self.fail("retry must not verify again"),
            )

            self.assertEqual(first.journal.stage, "verified")
            self.assertEqual(apply_count, 1)
            self.assertTrue(retry.idempotent_replay)
            self.assertIsNone(retry.mutation)
            self.assertIsNone(retry.verification)

    def test_pre_effect_failure_replans_and_same_operation_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            first = self.task(store)
            with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                first.run_mutation(
                    self.request(),
                    authorization=self.authorization,
                    apply=lambda _context: (_ for _ in ()).throw(
                        RuntimeError("preflight failed")
                    ),
                    verify=lambda _context: self.fail("must not verify"),
                )

            failed = store.load("recovery-task", "patch-1")
            self.assertIsNotNone(failed)
            self.assertEqual(failed.stage, "replan_required")
            self.assertFalse(failed.effects_started)
            self.assertFalse(failed.blocks_target)

            restarted = self.task(store)
            result = restarted.run_mutation(
                self.request(),
                authorization=self.authorization,
                apply=lambda _context: {"remote_after_sha256": "b" * 64},
                verify=lambda context: context.run_read(
                    self.read_request("replan-retry-verify"),
                    lambda read_context: read_context.epochs.target_epoch,
                ),
            )

            self.assertEqual(result.journal.stage, "verified")
            self.assertFalse(result.idempotent_replay)

    def test_effect_started_failure_remains_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            task = self.task(store)

            def fail_after_effect(context):
                context.mark_effects_started()
                raise RuntimeError("mutation failed")

            with self.assertRaisesRegex(RuntimeError, "mutation failed"):
                task.run_mutation(
                    self.request(),
                    authorization=self.authorization,
                    apply=fail_after_effect,
                    verify=lambda _context: self.fail("must not verify"),
                )

            failed = store.load("recovery-task", "patch-1")
            self.assertIsNotNone(failed)
            self.assertEqual(failed.stage, "mutation_failed")
            self.assertTrue(failed.effects_started)
            self.assertTrue(failed.blocks_target)

    def test_explicit_rejection_after_effect_boundary_allows_replan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            first = self.task(store)

            def reject_after_send(context):
                context.mark_effects_started()
                raise MutationEffectsRejected("HTTP 400 rejected")

            with self.assertRaisesRegex(MutationEffectsRejected, "HTTP 400"):
                first.run_mutation(
                    self.request(),
                    authorization=self.authorization,
                    apply=reject_after_send,
                    verify=lambda _context: self.fail("must not verify"),
                )

            failed = store.load("recovery-task", "patch-1")
            self.assertIsNotNone(failed)
            self.assertEqual(failed.stage, "replan_required")
            self.assertFalse(failed.effects_started)
            self.assertFalse(failed.blocks_target)
            self.assertEqual(failed.last_known_state, "mutation-explicitly-rejected")

    def test_terminal_verification_failure_replays_without_blocking_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            apply_count = 0

            def apply(_context):
                nonlocal apply_count
                apply_count += 1
                return {"remote_after_sha256": "b" * 64}

            def reject_verification(context):
                context.run_read(
                    self.read_request("terminal-failure-evidence"),
                    lambda read_context: read_context.epochs.target_epoch,
                )
                raise MutationVerificationTerminalFailure(
                    "fresh evidence proved the requested state was reverted",
                    outcome="activation-fallback",
                )

            first = self.task(store)
            with self.assertRaisesRegex(
                MutationVerificationTerminalFailure,
                "requested state was reverted",
            ):
                first.run_mutation(
                    self.request(),
                    authorization=self.authorization,
                    apply=apply,
                    verify=reject_verification,
                )

            failed = store.load("recovery-task", "patch-1")
            self.assertIsNotNone(failed)
            self.assertEqual(failed.stage, "verification_failed_terminal")
            self.assertEqual(failed.last_known_state, "activation-fallback")
            self.assertEqual(failed.recovery_decision, "none")
            self.assertTrue(failed.terminal)
            self.assertFalse(failed.blocks_target)
            self.assertIsNone(
                store.find_unfinished_target(self.target.fingerprint)
            )

            restarted = self.task(store)
            replay = restarted.run_mutation(
                self.request(),
                authorization=self.authorization,
                apply=apply,
                verify=lambda _context: self.fail("terminal replay must not verify"),
            )
            self.assertTrue(replay.idempotent_replay)
            self.assertEqual(apply_count, 1)

            next_operation = restarted.run_mutation(
                self.request("patch-2"),
                authorization=self.authorization,
                apply=apply,
                verify=lambda context: context.run_read(
                    self.read_request("next-operation-evidence"),
                    lambda read_context: read_context.epochs.target_epoch,
                ),
            )
            self.assertEqual(next_operation.journal.stage, "verified")
            self.assertEqual(apply_count, 2)

    def test_replan_reset_preserves_identity_and_clears_execution_evidence(self) -> None:
        journal = MutationJournal(
            task_id="recovery-task",
            operation_id="patch-1",
            operation_fingerprint=self.request().fingerprint,
            action="live_patch",
            original_intent="diagnose-and-fix",
            target_fingerprint=self.target.fingerprint,
            target_identity=None,
            epoch_before=1,
            stage="replan_required",
            effects_started=False,
            epoch_after=2,
            rollback_epoch=3,
            backup_reference="/tmp/unit.lua.bak",
            artifact_reference="/tmp/unit.lua",
            before_checksum="a" * 64,
            expected_checksum="b" * 64,
            observed_checksum="c" * 64,
            root_mount_mode="ro",
            root_mount_restored=True,
            restart_state="completed:none",
            verification_state="not_started",
            recovery_decision="replan",
        )
        created_at = journal.created_at

        journal.reset_for_replan(7)

        self.assertEqual(journal.task_id, "recovery-task")
        self.assertEqual(journal.operation_id, "patch-1")
        self.assertEqual(journal.created_at, created_at)
        self.assertEqual(journal.epoch_before, 7)
        self.assertEqual(journal.stage, "planned")
        self.assertFalse(journal.effects_started)
        self.assertIsNone(journal.epoch_after)
        self.assertIsNone(journal.rollback_epoch)
        self.assertEqual(journal.backup_reference, "")
        self.assertEqual(journal.artifact_reference, "")
        self.assertEqual(journal.before_checksum, "")
        self.assertEqual(journal.expected_checksum, "")
        self.assertEqual(journal.observed_checksum, "")
        self.assertEqual(journal.root_mount_mode, "unknown")
        self.assertIsNone(journal.root_mount_restored)
        self.assertEqual(journal.restart_state, "unknown")
        self.assertEqual(journal.verification_state, "pending")
        self.assertEqual(journal.recovery_decision, "")

    def test_restart_inspects_then_verifies_completed_but_unverified_operation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            first_task = self.task(store)
            with self.assertRaisesRegex(RuntimeError, "verification interrupted"):
                first_task.run_mutation(
                    self.request(),
                    authorization=self.authorization,
                    apply=lambda _context: {
                        "remote_before_sha256": "a" * 64,
                        "remote_after_sha256": "b" * 64,
                        "root_mount_restored": True,
                        "backup": "/tmp/unit.lua.bak",
                    },
                    verify=lambda _context: (_ for _ in ()).throw(
                        RuntimeError("verification interrupted")
                    ),
                )

            restarted = self.task(store)
            order: list[str] = []
            status = restarted.recover_mutation(
                self.request(),
                authorization=self.authorization,
                inspect=lambda _context: (
                    order.append("inspect"),
                    {
                        "target_identity": TargetIdentity(
                            product_id="product-a",
                            machine_id="machine-a",
                        ),
                        "remote_checksum": "b" * 64,
                        "backup_exists": True,
                        "root_mount_mode": "ro",
                        "root_mount_restored": True,
                        "restart_observed": False,
                    },
                )[-1],
                verify=lambda context: (
                    order.append("verify"),
                    context.run_read(
                        self.read_request("recovery-verify"),
                        lambda read_context: read_context.epochs.target_epoch,
                    ),
                )[-1],
                rollback=lambda *_args: self.fail("verified apply must not rollback"),
            )

            self.assertEqual(order, ["inspect", "verify"])
            self.assertEqual(status.decision, "verify")
            self.assertEqual(status.journal.stage, "verified")
            self.assertEqual(status.verification.target_epoch, 1)

    def test_intermediate_state_never_auto_rolls_back_without_new_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            journal = MutationJournal(
                task_id="recovery-task",
                operation_id="patch-1",
                operation_fingerprint=self.request().fingerprint,
                action="live_patch",
                original_intent="diagnose-and-fix",
                target_fingerprint=self.target.fingerprint,
                target_identity=TargetIdentity(
                    product_id="product-a",
                    machine_id="machine-a",
                ),
                epoch_before=0,
                stage="applying",
                before_checksum="a" * 64,
                expected_checksum="b" * 64,
                backup_reference="/tmp/unit.lua.bak",
                root_mount_restored=False,
            )
            store.create(journal)
            restarted = self.task(store)
            order: list[str] = []

            with self.assertRaises(MutationAuthorizationDenied):
                restarted.recover_mutation(
                    self.request(),
                    authorization=self.authorization,
                    inspect=lambda _context: (
                        order.append("inspect"),
                        {
                            "target_identity": TargetIdentity(
                                product_id="product-a",
                                machine_id="machine-a",
                            ),
                            "remote_checksum": "b" * 64,
                            "backup_exists": True,
                            "backup_checksum": "a" * 64,
                            "root_mount_mode": "rw",
                            "root_mount_restored": False,
                            "restart_observed": False,
                        },
                    )[-1],
                    rollback=lambda *_args: order.append("rollback"),
                    verify=lambda *_args: order.append("verify"),
                )

            self.assertEqual(order, ["inspect"])
            blocked = store.load("recovery-task", "patch-1")
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked.stage, "recovery_blocked")
            self.assertEqual(blocked.recovery_decision, "rollback")

    def test_explicit_rollback_authorization_resumes_the_same_blocked_journal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            store.create(
                MutationJournal(
                    task_id="recovery-task",
                    operation_id="patch-1",
                    operation_fingerprint=self.request().fingerprint,
                    action="live_patch",
                    original_intent="diagnose-and-fix",
                    target_fingerprint=self.target.fingerprint,
                    target_identity=TargetIdentity(
                        product_id="product-a",
                        machine_id="machine-a",
                    ),
                    epoch_before=0,
                    stage="recovery_blocked",
                    effects_started=True,
                    before_checksum="a" * 64,
                    expected_checksum="b" * 64,
                    backup_reference="/tmp/unit.lua.bak",
                    root_mount_restored=False,
                    recovery_decision="rollback",
                )
            )
            restarted = self.task(store)
            order: list[str] = []
            status = restarted.recover_mutation(
                self.request(),
                authorization=MutationAuthorization.from_original_intent(
                    "rollback"
                ),
                inspect=lambda _context: (
                    order.append("inspect"),
                    {
                        "target_identity": TargetIdentity(
                            product_id="product-a",
                            machine_id="machine-a",
                        ),
                        "remote_checksum": "b" * 64,
                        "backup_exists": True,
                        "backup_checksum": "a" * 64,
                        "root_mount_mode": "rw",
                        "root_mount_restored": False,
                        "restart_observed": False,
                    },
                )[-1],
                rollback=lambda _context, _evidence: (
                    order.append("rollback"),
                    {
                        "remote_after_sha256": "a" * 64,
                        "root_mount_restored": True,
                    },
                )[-1],
                verify=lambda context: (
                    order.append("verify"),
                    context.run_read(
                        self.read_request("rollback-recovery-verify"),
                        lambda read_context: read_context.epochs.target_epoch,
                    ),
                )[-1],
            )

            persisted = store.load("recovery-task", "patch-1")

            self.assertEqual(order, ["inspect", "rollback", "verify"])
            self.assertEqual(status.decision, "rollback")
            self.assertEqual(status.journal.stage, "rollback_verified")
            self.assertEqual(status.journal.original_intent, "diagnose-and-fix")
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.authorized_recovery_actions, ("rollback",))

    def test_unfinished_journal_blocks_stacked_mutation_with_recovery_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            store.create(
                MutationJournal(
                    task_id="other-task",
                    operation_id="patch-1",
                    operation_fingerprint=self.request().fingerprint,
                    action="live_patch",
                    original_intent="diagnose-and-fix",
                    target_fingerprint=self.target.fingerprint,
                    target_identity=None,
                    epoch_before=0,
                    stage="applying",
                )
            )
            task = self.task(store)

            with self.assertRaises(UnfinishedMutationExists) as captured:
                task.run_mutation(
                    self.request("patch-2"),
                    authorization=self.authorization,
                    apply=lambda _context: self.fail("must not apply"),
                    verify=lambda _context: self.fail("must not verify"),
                )

            self.assertEqual(captured.exception.recovery_status["operation_id"], "patch-1")
            self.assertEqual(captured.exception.recovery_status["stage"], "applying")

    def test_planned_operation_is_replanned_after_read_only_no_change_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MutationJournalStore(Path(raw) / "journals")
            store.create(
                MutationJournal(
                    task_id="recovery-task",
                    operation_id="patch-1",
                    operation_fingerprint=self.request().fingerprint,
                    action="live_patch",
                    original_intent="diagnose-and-fix",
                    target_fingerprint=self.target.fingerprint,
                    target_identity=None,
                    epoch_before=0,
                    stage="planned",
                    before_checksum="a" * 64,
                    expected_checksum="b" * 64,
                )
            )
            restarted = self.task(store)
            inspected: list[str] = []

            status = restarted.recover_mutation(
                self.request(),
                authorization=self.authorization,
                inspect=lambda _context: (
                    inspected.append("read-only"),
                    {
                        "remote_checksum": "a" * 64,
                        "backup_exists": False,
                        "root_mount_mode": "ro",
                        "root_mount_restored": True,
                    },
                )[-1],
                verify=lambda _context: self.fail("replan must not verify"),
                rollback=lambda *_args: self.fail("replan must not rollback"),
            )

            self.assertEqual(inspected, ["read-only"])
            self.assertEqual(status.decision, "replan")
            self.assertEqual(status.journal.stage, "replan_required")

    def test_journal_is_secret_free_and_artifact_reference_stays_in_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            store = MutationJournalStore(
                root / "journals",
                artifact_roots=(artifacts,),
            )
            task = self.task(store)
            inside = artifacts / "candidate.bin"
            inside.write_bytes(b"candidate")

            result = task.run_mutation(
                self.request(),
                authorization=self.authorization,
                apply=lambda context: (
                    context.record_artifact(str(inside)),
                    {"remote_after_sha256": "b" * 64},
                )[-1],
                verify=lambda context: context.run_read(
                    self.read_request("artifact-verify"),
                    lambda read_context: read_context.epochs.target_epoch,
                ),
            )
            journal_files = list((root / "journals").glob("*.json"))
            serialized = journal_files[0].read_text(encoding="utf-8")

            self.assertEqual(result.journal.artifact_reference, str(inside.resolve()))
            self.assertNotIn("must-not-be-persisted", serialized)
            self.assertNotIn("password", serialized.casefold())
            self.assertIsInstance(json.loads(serialized), dict)

            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            with self.assertRaises(ValueError):
                result.journal.record_artifact(str(outside))


if __name__ == "__main__":
    unittest.main()
