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
    MutationRequest,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    ResolvedSshCredentials,
    StaleEvidenceRejected,
    TargetSpec,
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
    def test_original_intent_is_projected_once_and_diagnosis_only_is_denied(self) -> None:
        diagnosis = MutationAuthorization.from_original_intent("diagnosis-only")
        diagnose_and_fix = MutationAuthorization.from_original_intent(
            "diagnose-and-fix"
        )

        with self.assertRaises(MutationAuthorizationDenied):
            diagnosis.require("live_patch")

        self.assertEqual(
            diagnose_and_fix.require("live_patch").original_intent,
            "diagnose-and-fix",
        )
        self.assertEqual(diagnose_and_fix.parse_count, 1)

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
                    authorization=MutationAuthorization.from_original_intent(
                        "diagnose-and-fix"
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
                authorization=MutationAuthorization.from_original_intent(
                    "diagnose-and-fix"
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


if __name__ == "__main__":
    unittest.main()
