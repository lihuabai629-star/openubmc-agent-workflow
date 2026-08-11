from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    MutationAuthorization,
    MutationAuthorizationDenied,
    MutationAuthorizedExceptions,
    MutationRequest,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    ResolvedSshCredentials,
    StaleEvidenceRejected,
    TaskAuthorizationPolicy,
    TargetSpec,
    mutation_journal_operation_status,
)


def runtime_fixture() -> tuple[
    OpenUBMCTaskRun,
    TargetSpec,
    CredentialSelector,
]:
    selector = CredentialSelector.for_ssh(
        user="root",
        user_env="",
        password_env="",
        identity_file="",
        environ={},
    )
    target = TargetSpec(
        host="bmc.example",
        credential_selector_fingerprint=selector.fingerprint,
    )
    resolver = CredentialResolver(
        lambda _selector: ResolvedSshCredentials(
            user="root",
            password="secret",
        )
    )
    return (
        OpenUBMCTaskRun(task_id="task-mutation", credential_resolver=resolver),
        target,
        selector,
    )


def read_request(
    request_id: str,
    target: TargetSpec,
    selector: CredentialSelector,
) -> RemoteReadRequest:
    return RemoteReadRequest.create(
        request_id=request_id,
        target=target,
        credential_selector=selector,
        collector_name="debug-freshness",
        operation={"read": request_id},
    )


class MutationTransactionTests(unittest.TestCase):
    def test_journal_stage_classifier_is_fail_closed(self) -> None:
        cases = (
            ({"stage": "verified", "action": "live_patch"}, "completed"),
            ({"stage": "rollback_verified", "action": "rollback"}, "completed"),
            ({"stage": "rollback_verified", "action": "live_patch"}, "failed"),
            ({"stage": "replan_required"}, "failed"),
            ({"stage": "recovery_blocked"}, "blocked"),
            ({"stage": "mutation_failed"}, "mutation_outcome_unknown"),
            ({"stage": "rollback_failed"}, "mutation_outcome_unknown"),
            ({"stage": "rolling_back"}, "mutation_outcome_unknown"),
            ({"stage": "planned", "effects_started": False}, "blocked"),
            (
                {"stage": "planned", "effects_started": True},
                "mutation_outcome_unknown",
            ),
            ({"stage": "rollback_verifying"}, "blocked"),
            ({"stage": "future_stage"}, "blocked"),
        )
        for journal, expected in cases:
            with self.subTest(journal=journal):
                self.assertEqual(
                    mutation_journal_operation_status(journal),
                    expected,
                )

    def test_original_intent_and_delivery_are_projected_once(self) -> None:
        diagnosis = MutationAuthorization.from_original_intent("diagnosis-only")
        source_only = MutationAuthorization.from_original_intent(
            "diagnose-and-fix",
        )
        diagnose_and_fix = MutationAuthorization.from_task_intent(
            "diagnose-and-fix",
            delivery_strategy="live-patch",
        )

        with self.assertRaises(MutationAuthorizationDenied):
            diagnosis.require("live_patch")
        with self.assertRaises(MutationAuthorizationDenied):
            source_only.require("live_patch")

        self.assertEqual(
            diagnose_and_fix.require("live_patch").original_intent,
            "diagnose-and-fix",
        )
        self.assertEqual(diagnose_and_fix.parse_count, 1)

    def test_task_authorization_policy_round_trips_and_rejects_tampering(self) -> None:
        policy = TaskAuthorizationPolicy.from_task_intent(
            "diagnose-and-fix",
            delivery_strategy="build-upgrade",
            authorized_exceptions={"no_backup": True},
            allow_insecure_tls=True,
        )

        restored = TaskAuthorizationPolicy.from_public_dict(
            policy.to_public_dict()
        )

        self.assertEqual(restored, policy)
        restored.require("upgrade").require_insecure_tls()
        restored.require_exception("no_backup")

        for field, replacement in (
            ("allowed_actions", ["rollback"]),
            ("delivery_strategy", "live-patch"),
            ("parse_count", 2),
        ):
            with self.subTest(field=field):
                tampered = policy.to_public_dict()
                tampered[field] = replacement
                with self.assertRaises(ValueError):
                    TaskAuthorizationPolicy.from_public_dict(tampered)

        missing = policy.to_public_dict()
        missing.pop("allow_insecure_tls")
        with self.assertRaises(ValueError):
            TaskAuthorizationPolicy.from_public_dict(missing)

        unknown = policy.to_public_dict()
        unknown["grant_all"] = True
        with self.assertRaises(ValueError):
            TaskAuthorizationPolicy.from_public_dict(unknown)

    def test_apply_intents_do_not_authorize_rollback(self) -> None:
        direct_apply = MutationAuthorization.from_original_intent("live-patch")
        diagnose_and_fix = MutationAuthorization.from_task_intent(
            "diagnose-and-fix",
            delivery_strategy="live-patch",
        )
        direct_rollback = MutationAuthorization.from_original_intent("rollback")

        direct_apply.require("live_patch")
        diagnose_and_fix.require("live_patch")
        direct_rollback.require("rollback")

        with self.assertRaises(MutationAuthorizationDenied):
            direct_apply.require("rollback")
        with self.assertRaises(MutationAuthorizationDenied):
            diagnose_and_fix.require("rollback")
        with self.assertRaises(MutationAuthorizationDenied):
            direct_rollback.require("live_patch")

    def test_mutation_exceptions_are_typed_and_authorized_at_task_level(self) -> None:
        authorization = MutationAuthorization.from_task_intent(
            "diagnose-and-fix",
            delivery_strategy="live-patch",
            authorized_exceptions={
                "force_path": False,
                "no_backup": True,
                "no_remount": False,
            },
        )

        self.assertIsInstance(
            authorization.authorized_exceptions,
            MutationAuthorizedExceptions,
        )
        authorization.require_exception("no_backup")
        with self.assertRaises(MutationAuthorizationDenied):
            authorization.require_exception("force_path")
        self.assertEqual(
            authorization.to_public_dict()["authorized_exceptions"],
            {
                "force_path": False,
                "no_backup": True,
                "no_remount": False,
            },
        )

        with self.assertRaises(TypeError):
            MutationAuthorization.from_task_intent(
                "diagnose-and-fix",
                delivery_strategy="live-patch",
                authorized_exceptions={"no_backup": "true"},
            )

    def test_mutation_waits_for_running_read_blocks_new_read_and_verifies_first(self) -> None:
        task, target, selector = runtime_fixture()
        first_started = threading.Event()
        release_first = threading.Event()
        mutation_started = threading.Event()
        verification_started = threading.Event()
        release_verification = threading.Event()
        second_started = threading.Event()
        order: list[str] = []
        errors: list[BaseException] = []

        def first_read() -> None:
            try:
                task.run_read(
                    read_request("read-before", target, selector),
                    lambda _context: (
                        first_started.set(),
                        release_first.wait(2),
                        order.append("read-before"),
                    )[-1],
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def mutate() -> None:
            try:
                request = MutationRequest.create(
                    operation_id="patch-1",
                    target=target,
                    credential_selector=selector,
                    action="live_patch",
                    operation={"remote": "/opt/bmc/apps/demo/unit.lua"},
                )

                def apply(context):
                    self.assertEqual(context.journal.stage, "applying")
                    self.assertEqual(context.journal.epoch_before, 0)
                    mutation_started.set()
                    order.append("apply")
                    return {"backup": "/tmp/unit.lua.bak"}

                def verify(context):
                    result = context.run_read(
                        read_request("verify-after", target, selector),
                        lambda read_context: {
                            "epoch": read_context.epochs.target_epoch,
                            "fresh": True,
                        },
                    )
                    order.append("verify")
                    verification_started.set()
                    release_verification.wait(2)
                    return result

                result = task.run_mutation(
                    request,
                    authorization=MutationAuthorization.from_task_intent(
                        "diagnose-and-fix",
                        delivery_strategy="live-patch",
                    ),
                    apply=apply,
                    verify=verify,
                )
                self.assertEqual(result.epoch_before, 0)
                self.assertEqual(result.epoch_after, 1)
                self.assertEqual(result.verification.target_epoch, 1)
                self.assertEqual(result.journal.stage, "verified")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def second_read() -> None:
            try:
                task.run_read(
                    read_request("read-after", target, selector),
                    lambda context: (
                        second_started.set(),
                        order.append(f"read-after:{context.epochs.target_epoch}"),
                    )[-1],
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        first = threading.Thread(target=first_read)
        mutation = threading.Thread(target=mutate)
        second = threading.Thread(target=second_read)
        first.start()
        self.assertTrue(first_started.wait(1))
        mutation.start()
        time.sleep(0.05)
        second.start()
        time.sleep(0.05)

        self.assertFalse(mutation_started.is_set())
        self.assertFalse(second_started.is_set())
        release_first.set()

        self.assertTrue(verification_started.wait(2))
        self.assertFalse(second_started.is_set())
        release_verification.set()
        first.join(2)
        mutation.join(2)
        second.join(2)

        self.assertFalse(errors, errors)
        self.assertEqual(order, ["read-before", "apply", "verify", "read-after:1"])

    def test_pre_mutation_request_cannot_satisfy_fresh_verification(self) -> None:
        task, target, selector = runtime_fixture()
        old = task.run_read(
            read_request("same-request", target, selector),
            lambda _context: "old",
        )
        self.assertEqual(old.target_epoch, 0)

        request = MutationRequest.create(
            operation_id="patch-2",
            target=target,
            credential_selector=selector,
            action="live_patch",
            operation={"remote": "/opt/bmc/apps/demo/unit.lua"},
        )

        with self.assertRaises(StaleEvidenceRejected):
            task.run_mutation(
                request,
                authorization=MutationAuthorization.from_task_intent(
                    "diagnose-and-fix",
                    delivery_strategy="live-patch",
                ),
                apply=lambda _context: {"ok": True},
                verify=lambda context: context.run_read(
                    read_request("same-request", target, selector),
                    lambda _read_context: "must-not-run",
                ),
            )

        status = task.runtime_status()
        journal = status["mutation_journals"][0]
        self.assertEqual(journal["stage"], "verification_failed")
        self.assertEqual(journal["epoch_before"], 0)
        self.assertEqual(journal["epoch_after"], 1)

    def test_mutation_errors_expose_whether_remote_effects_may_have_started(self) -> None:
        task, target, selector = runtime_fixture()
        request = MutationRequest.create(
            operation_id="patch-outcome-marker",
            target=target,
            credential_selector=selector,
            action="live_patch",
            operation={"remote": "/opt/bmc/apps/demo/unit.lua"},
        )

        with self.assertRaisesRegex(ValueError, "local validation failed") as rejected:
            task.run_mutation(
                request,
                authorization=MutationAuthorization.from_original_intent(
                    "live-patch"
                ),
                apply=lambda _context: (_ for _ in ()).throw(
                    ValueError("local validation failed")
                ),
                verify=lambda _context: None,
            )

        self.assertEqual(rejected.exception.mutation_outcome, "not_started")

        uncertain_request = MutationRequest.create(
            operation_id="patch-uncertain-marker",
            target=target,
            credential_selector=selector,
            action="live_patch",
            operation={"remote": "/opt/bmc/apps/demo/other.lua"},
        )

        def uncertain_apply(context):
            context.mark_effects_started()
            raise OSError("connection lost")

        with self.assertRaisesRegex(OSError, "connection lost") as uncertain:
            task.run_mutation(
                uncertain_request,
                authorization=MutationAuthorization.from_original_intent(
                    "live-patch"
                ),
                apply=uncertain_apply,
                verify=lambda _context: None,
            )

        self.assertEqual(uncertain.exception.mutation_outcome, "unknown")


if __name__ == "__main__":
    unittest.main()
