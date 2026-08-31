from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CancellationToken,
    EngineUnavailable,
    EngineSwitchProhibited,
    OperationCancelled,
    OperationContext,
    OperationDeadlineExceeded,
    RequestEngineDecision,
    TaskRunRegistry,
    select_request_engine,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeResource:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.calls: list[str] = []
        self.closed = False
        self.maintenance_runs = 0

    def close(self) -> None:
        self.closed = True

    def status(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "calls": list(self.calls),
            "closed": self.closed,
        }

    def maintain(self) -> int:
        self.maintenance_runs += 1
        return self.maintenance_runs


class TaskRunRegistryTests(unittest.TestCase):
    def make_registry(self, **kwargs) -> tuple[TaskRunRegistry[FakeResource], list[FakeResource]]:
        created: list[FakeResource] = []

        def factory(task_id: str) -> FakeResource:
            resource = FakeResource(task_id)
            created.append(resource)
            return resource

        registry = TaskRunRegistry(
            factory=factory,
            closer=lambda resource: resource.close(),
            status_reader=lambda resource: resource.status(),
            maintenance=lambda resource: resource.maintain(),
            **kwargs,
        )
        return registry, created

    def test_operation_context_derives_a_child_without_exposing_its_clock(self) -> None:
        clock = FakeClock()
        cancellation = CancellationToken()
        parent = OperationContext(
            task_id="task-a",
            operation_id="outer",
            deadline_at=130.0,
            cancellation=cancellation,
            _clock=clock,
        )

        child = parent.derive("inner")

        self.assertEqual(child.task_id, "task-a")
        self.assertEqual(child.operation_id, "inner")
        self.assertEqual(child.deadline_at, 130.0)
        self.assertIs(child.cancellation, cancellation)
        clock.advance(5)
        self.assertEqual(child.remaining(), 25.0)

    def test_same_task_reuses_resource_and_different_tasks_are_isolated(self) -> None:
        registry, created = self.make_registry()

        first = registry.execute(
            task_id="task-a",
            operation_id="run-1",
            timeout_seconds=1,
            callback=lambda resource, _context: id(resource),
        )
        second = registry.execute(
            task_id="task-a",
            operation_id="collect-1",
            timeout_seconds=1,
            callback=lambda resource, _context: id(resource),
        )
        other = registry.execute(
            task_id="task-b",
            operation_id="run-1",
            timeout_seconds=1,
            callback=lambda resource, _context: id(resource),
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual([resource.task_id for resource in created], ["task-a", "task-b"])
        status = registry.status()
        self.assertEqual(status["task_count"], 2)
        self.assertEqual(
            {task["task_id"] for task in status["tasks"]}, {"task-a", "task-b"}
        )

    def test_completion_closes_resource_and_next_request_rebuilds(self) -> None:
        registry, created = self.make_registry()
        first = registry.execute(
            task_id="task-a",
            operation_id="run-1",
            timeout_seconds=1,
            callback=lambda resource, _context: id(resource),
        )

        self.assertTrue(registry.complete("task-a"))
        self.assertTrue(created[0].closed)
        second = registry.execute(
            task_id="task-a",
            operation_id="run-2",
            timeout_seconds=1,
            callback=lambda resource, _context: id(resource),
        )

        self.assertNotEqual(first, second)
        self.assertEqual(len(created), 2)

    def test_completion_drains_existing_work_and_rejects_new_operations(self) -> None:
        registry, created = self.make_registry()
        running_started = threading.Event()
        release_running = threading.Event()
        queued_started = threading.Event()

        def running_callback(_resource, context):
            running_started.set()
            while not release_running.is_set():
                context.wait(0.02)
            return "running-completed"

        def queued_callback(_resource, context):
            context.raise_if_stopped()
            queued_started.set()
            return "queued-completed"

        with ThreadPoolExecutor(max_workers=3) as executor:
            running = executor.submit(
                registry.execute,
                task_id="task-a",
                operation_id="running",
                timeout_seconds=2,
                callback=running_callback,
            )
            self.assertTrue(running_started.wait(1))
            queued = executor.submit(
                registry.execute,
                task_id="task-a",
                operation_id="queued",
                timeout_seconds=2,
                callback=queued_callback,
            )
            for _ in range(100):
                status = registry.status()["tasks"][0]
                if status["queued_operations"] == 1:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(status["queued_operations"], 1)

            self.assertTrue(registry.complete("task-a"))
            with self.assertRaisesRegex(OperationCancelled, "cannot accept new work"):
                registry.execute(
                    task_id="task-a",
                    operation_id="late",
                    timeout_seconds=1,
                    callback=lambda _resource, _context: "unexpected",
                )

            release_running.set()
            self.assertEqual(running.result(timeout=1), "running-completed")
            self.assertEqual(queued.result(timeout=1), "queued-completed")

        self.assertTrue(queued_started.is_set())
        self.assertTrue(created[0].closed)
        self.assertEqual(registry.status()["task_count"], 0)

    def test_idle_timeout_and_lru_pressure_reclaim_only_idle_tasks(self) -> None:
        clock = FakeClock()
        registry, created = self.make_registry(
            max_tasks=2,
            idle_timeout_seconds=30,
            clock=clock,
        )
        for task_id in ("task-a", "task-b"):
            registry.execute(
                task_id=task_id,
                operation_id="run",
                timeout_seconds=1,
                callback=lambda _resource, _context: None,
            )
        clock.advance(1)
        registry.execute(
            task_id="task-a",
            operation_id="touch",
            timeout_seconds=1,
            callback=lambda _resource, _context: None,
        )
        registry.execute(
            task_id="task-c",
            operation_id="run",
            timeout_seconds=1,
            callback=lambda _resource, _context: None,
        )

        self.assertFalse(created[0].closed)
        self.assertTrue(created[1].closed)
        clock.advance(31)
        reaped = registry.reap()
        self.assertEqual(reaped, 2)
        self.assertTrue(created[0].closed)
        self.assertTrue(created[2].closed)

    def test_maintenance_runs_before_reuse_for_dead_connection_cleanup(self) -> None:
        registry, created = self.make_registry()
        for operation_id in ("run", "collect"):
            registry.execute(
                task_id="task-a",
                operation_id=operation_id,
                timeout_seconds=1,
                callback=lambda _resource, _context: None,
            )

        self.assertEqual(created[0].maintenance_runs, 2)

    def test_cancellation_stops_queued_and_cooperative_running_work(self) -> None:
        registry, _created = self.make_registry()
        running_started = threading.Event()
        release_running = threading.Event()
        queued_callback_called = threading.Event()

        def first_callback(_resource, context):
            running_started.set()
            while not release_running.is_set():
                context.wait(0.02)
            return "done"

        def queued_callback(_resource, _context):
            queued_callback_called.set()
            return "unexpected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                registry.execute,
                task_id="task-a",
                operation_id="running",
                timeout_seconds=2,
                callback=first_callback,
            )
            self.assertTrue(running_started.wait(1))
            queued = executor.submit(
                registry.execute,
                task_id="task-a",
                operation_id="queued",
                timeout_seconds=2,
                callback=queued_callback,
            )
            self.assertTrue(registry.cancel_operation("task-a", "queued"))
            with self.assertRaises(OperationCancelled):
                queued.result(timeout=1)
            self.assertFalse(queued_callback_called.is_set())

            self.assertTrue(registry.cancel_operation("task-a", "running"))
            with self.assertRaises(OperationCancelled):
                first.result(timeout=1)
            release_running.set()

    def test_deadline_stops_work_while_waiting_for_task_slot(self) -> None:
        registry, _created = self.make_registry()
        running_started = threading.Event()
        release_running = threading.Event()

        def first_callback(_resource, context):
            running_started.set()
            while not release_running.is_set():
                context.wait(0.02)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                registry.execute,
                task_id="task-a",
                operation_id="running",
                timeout_seconds=2,
                callback=first_callback,
            )
            self.assertTrue(running_started.wait(1))
            with self.assertRaises(OperationDeadlineExceeded):
                registry.execute(
                    task_id="task-a",
                    operation_id="queued",
                    timeout_seconds=0.05,
                    callback=lambda _resource, _context: None,
                )
            release_running.set()
            first.result(timeout=1)


class EngineSelectionTests(unittest.TestCase):
    def test_auto_prefers_mcp_and_falls_back_only_before_remote_start(self) -> None:
        selected = select_request_engine(
            preference="auto",
            mcp_available=False,
            one_shot_available=True,
        )
        self.assertIsInstance(selected, RequestEngineDecision)
        self.assertEqual(selected.engine, "one-shot")
        self.assertTrue(selected.fallback_used)
        selected.mark_remote_started()
        with self.assertRaises(EngineSwitchProhibited):
            selected.require_engine("mcp")

    def test_selection_fails_when_no_matching_engine_is_ready(self) -> None:
        with self.assertRaises(EngineUnavailable):
            select_request_engine(
                preference="auto",
                mcp_available=False,
                one_shot_available=False,
            )


if __name__ == "__main__":
    unittest.main()
