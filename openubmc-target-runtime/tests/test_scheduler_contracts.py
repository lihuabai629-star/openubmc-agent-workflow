from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import FairTargetScheduler  # noqa: E402


class FairTargetSchedulerTests(unittest.TestCase):
    def test_explicit_budget_queues_without_rejecting_target_count(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()
        targets = list(range(40))

        def run_target(value: int, _context):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.002)
            with lock:
                active -= 1
            return value * 2

        result = FairTargetScheduler(concurrency=3).run(
            targets,
            run_target,
            context=None,
        )

        self.assertEqual(len(result.results), 40)
        self.assertEqual([item.value for item in result.results], [x * 2 for x in targets])
        self.assertLessEqual(max_active, 3)
        self.assertEqual(result.metrics["requested_policy"], "3")
        self.assertEqual(result.metrics["actual_concurrency_budget"], 3)
        self.assertEqual(result.metrics["peak_inflight_submissions"], 3)
        self.assertGreater(result.metrics["max_queue_delay_ms"], 0)

    def test_unbounded_uses_target_count_as_budget(self) -> None:
        result = FairTargetScheduler(concurrency="unbounded").run(
            list(range(6)),
            lambda value, _context: value,
            context=None,
        )

        self.assertEqual(result.metrics["requested_policy"], "unbounded")
        self.assertEqual(result.metrics["actual_concurrency_budget"], 6)

    def test_one_slow_target_does_not_block_fast_target_completion(self) -> None:
        def run_target(value: str, _context):
            time.sleep(0.15 if value == "slow" else 0.01)
            return value

        result = FairTargetScheduler(concurrency=2).run(
            ["slow", "fast-a", "fast-b"],
            run_target,
            context=None,
        )

        self.assertNotEqual(result.completion_order[0], 0)
        self.assertEqual(result.results[0].value, "slow")
        self.assertEqual(result.results[1].value, "fast-a")
        self.assertLess(
            result.results[1].completed_at_monotonic,
            result.results[0].completed_at_monotonic,
        )

    def test_target_local_failure_does_not_cancel_other_targets(self) -> None:
        def run_target(value: str, _context):
            if value == "bad":
                raise TimeoutError("target-local timeout")
            return value

        result = FairTargetScheduler(concurrency="auto").run(
            ["good-a", "bad", "good-b"],
            run_target,
            context=None,
        )

        self.assertEqual(result.results[0].value, "good-a")
        self.assertEqual(result.results[1].error_code, "TimeoutError")
        self.assertEqual(result.results[2].value, "good-b")
        self.assertEqual(result.metrics["completed_count"], 2)
        self.assertEqual(result.metrics["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
