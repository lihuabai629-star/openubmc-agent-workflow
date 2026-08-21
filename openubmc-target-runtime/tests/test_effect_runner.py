from __future__ import annotations

from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.capability import EffectClass  # noqa: E402
from openubmc_target_runtime.effect_runner import (  # noqa: E402
    EffectIntent,
    EffectRunMode,
    LocalEffectRunner,
)


class LocalEffectRunnerTests(unittest.TestCase):
    def test_reattach_reuses_a_settled_result_until_the_runtime_commits_it(self) -> None:
        calls: list[str] = []

        def execute(intent: EffectIntent) -> dict[str, object]:
            calls.append(intent.effect_id)
            return {"status": "completed"}

        runner = LocalEffectRunner(execute, execute, max_workers=1)
        intent = EffectIntent(
            run_id="run-settled",
            effect_id="effect-settled",
            operation="live_patch_run",
            effect_class=EffectClass.IRREVERSIBLE_MUTATION,
            request_fingerprint="a" * 64,
            arguments={},
        )
        try:
            first = runner.ensure(intent, mode=EffectRunMode.DISPATCH)
            self.assertEqual(first.future.result(timeout=1), {"status": "completed"})

            reattached = runner.ensure(intent, mode=EffectRunMode.REATTACH)

            self.assertIs(reattached, first)
            self.assertEqual(calls, ["effect-settled"])

            runner.acknowledge(
                intent,
                reattached,
                retain_for_reattach=False,
            )
            self.assertFalse(runner.has_seen(intent))
            next_attempt = runner.ensure(intent, mode=EffectRunMode.REATTACH)

            self.assertIsNot(next_attempt, first)
            self.assertEqual(
                next_attempt.future.result(timeout=1),
                {"status": "completed"},
            )
            self.assertEqual(calls, ["effect-settled", "effect-settled"])
        finally:
            runner.close()

    def test_reattach_preserves_the_original_recovery_settlement_mode(self) -> None:
        calls: list[str] = []

        def execute(_intent: EffectIntent) -> dict[str, object]:
            calls.append("dispatch")
            return {"status": "dispatched"}

        def recover(_intent: EffectIntent) -> dict[str, object]:
            calls.append("recover")
            return {"status": "recovered"}

        runner = LocalEffectRunner(
            execute,
            recover,
            max_workers=1,
        )
        intent = EffectIntent(
            run_id="run-recovery",
            effect_id="effect-recovery",
            operation="upgrade_run",
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            request_fingerprint="b" * 64,
            arguments={},
        )
        try:
            recovery = runner.ensure(intent, mode=EffectRunMode.RECOVER)
            reattached = runner.ensure(intent, mode=EffectRunMode.REATTACH)

            self.assertIs(reattached, recovery)
            self.assertIs(reattached.mode, EffectRunMode.RECOVER)
            self.assertEqual(
                reattached.future.result(timeout=1),
                {"status": "recovered"},
            )
            runner.acknowledge(
                intent,
                reattached,
                retain_for_reattach=True,
            )

            next_recovery = runner.ensure(intent, mode=EffectRunMode.REATTACH)

            self.assertIsNot(next_recovery, recovery)
            self.assertIs(next_recovery.mode, EffectRunMode.RECOVER)
            self.assertEqual(
                next_recovery.future.result(timeout=1),
                {"status": "recovered"},
            )
            self.assertEqual(calls, ["recover", "recover"])
            runner.acknowledge(
                intent,
                next_recovery,
                retain_for_reattach=False,
            )
            self.assertFalse(runner.has_seen(intent))
        finally:
            runner.close()

    def test_stale_acknowledgement_does_not_release_a_new_execution(self) -> None:
        runner = LocalEffectRunner(
            lambda _intent: {"status": "completed"},
            lambda _intent: {"status": "recovered"},
            max_workers=1,
        )
        intent = EffectIntent(
            run_id="run-stale-ack",
            effect_id="effect-stale-ack",
            operation="live_patch_run",
            effect_class=EffectClass.IRREVERSIBLE_MUTATION,
            request_fingerprint="e" * 64,
            arguments={},
        )
        try:
            first = runner.ensure(intent, mode=EffectRunMode.DISPATCH)
            first.future.result(timeout=1)
            runner.acknowledge(
                intent,
                first,
                retain_for_reattach=False,
            )

            replacement = runner.ensure(intent, mode=EffectRunMode.DISPATCH)
            runner.acknowledge(
                intent,
                first,
                retain_for_reattach=False,
            )

            self.assertTrue(runner.has_seen(intent))
            self.assertIs(
                runner.ensure(intent, mode=EffectRunMode.REATTACH),
                replacement,
            )
        finally:
            runner.close()

    def test_same_identity_rejects_a_conflicting_effect_intent(self) -> None:
        runner = LocalEffectRunner(
            lambda _intent: {"status": "completed"},
            lambda _intent: {"status": "recovered"},
            max_workers=1,
        )
        original = EffectIntent(
            run_id="run-conflict",
            effect_id="effect-conflict",
            operation="live_patch_run",
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            request_fingerprint="c" * 64,
            arguments={"artifact": "first"},
        )
        conflicting = EffectIntent(
            run_id=original.run_id,
            effect_id=original.effect_id,
            operation=original.operation,
            effect_class=original.effect_class,
            request_fingerprint="d" * 64,
            arguments={"artifact": "second"},
        )
        try:
            execution = runner.ensure(original, mode=EffectRunMode.DISPATCH)
            execution.future.result(timeout=1)

            with self.assertRaisesRegex(ValueError, "conflicting intent"):
                runner.ensure(conflicting, mode=EffectRunMode.REATTACH)

            runner.acknowledge(
                original,
                execution,
                retain_for_reattach=False,
            )
            self.assertFalse(runner.has_seen(original))
        finally:
            runner.close()


if __name__ == "__main__":
    unittest.main()
