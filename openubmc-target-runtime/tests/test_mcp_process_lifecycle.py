from __future__ import annotations

import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    McpProcessLifecycle,
    StdioMcpServer,
    cleanup_confirmed_orphaned_mcp_processes,
    cleanup_retired_orphaned_mcp_processes,
    inspect_mcp_process_records,
)


class FakeClock:
    def __init__(self) -> None:
        self.monotonic = 100.0
        self.wall = 1_787_872_800.0

    def monotonic_time(self) -> float:
        return self.monotonic

    def wall_time(self) -> float:
        return self.wall


class ExitedPidfdPoll:
    def register(self, process_handle: int, _events: int) -> None:
        self.process_handle = process_handle

    def poll(self, _timeout_ms: int):
        return [(self.process_handle, select.POLLIN)]


class McpProcessLifecycleTests(unittest.TestCase):
    def make_lifecycle(
        self,
        raw: str,
        clock: FakeClock,
        *,
        process_alive,
        idle_timeout_seconds: float = 300,
    ) -> McpProcessLifecycle:
        return McpProcessLifecycle(
            component="target-runtime",
            version="openubmc.target-runtime.v1",
            client="codex",
            task_id="task-100",
            session_id="session-100",
            source_commit="a" * 40,
            model_identity={"model": "gpt-5.6-sol"},
            codex_identity={
                "version": "codex-cli 0.150.0",
                "executable_sha256": "sha256:" + "b" * 64,
            },
            formal_run=True,
            parent_pid=1200,
            process_id=1201,
            state_path=Path(raw) / "runtime-state",
            lifecycle_root=Path(raw) / "mcp-processes",
            idle_timeout_seconds=idle_timeout_seconds,
            monotonic_clock=clock.monotonic_time,
            wall_clock=clock.wall_time,
            process_alive=process_alive,
            process_identity=lambda pid: f"process-{pid}-start",
        )

    def test_startup_records_attributable_idle_process_metadata(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
            )

            status = lifecycle.status()
            recorded = json.loads(lifecycle.record_path.read_text(encoding="utf-8"))

        self.assertEqual(status["lifecycle_state"], "idle")
        self.assertEqual(status["active_requests"], 0)
        self.assertEqual(recorded["client"], "codex")
        self.assertEqual(recorded["task_id"], "task-100")
        self.assertEqual(recorded["session_id"], "session-100")
        self.assertEqual(recorded["source_commit"], "a" * 40)
        self.assertTrue(recorded["formal_run"])
        self.assertEqual(recorded["model_identity"], {"model": "gpt-5.6-sol"})
        self.assertEqual(
            recorded["codex_identity"],
            {
                "version": "codex-cli 0.150.0",
                "executable_sha256": "sha256:" + "b" * 64,
            },
        )
        self.assertEqual(recorded["parent_pid"], 1200)
        self.assertEqual(recorded["parent_identity"], "process-1200-start")
        self.assertTrue(recorded["parent_identity_verified"])
        self.assertTrue(recorded["parent_identity_currently_verified"])
        self.assertEqual(recorded["process_id"], 1201)
        self.assertEqual(recorded["process_identity"], "process-1201-start")
        self.assertEqual(recorded["component"], "target-runtime")
        self.assertEqual(recorded["version"], "openubmc.target-runtime.v1")
        self.assertEqual(recorded["state_path"], str(Path(raw) / "runtime-state"))
        self.assertEqual(
            recorded["runtime_state_root"], str(Path(raw) / "runtime-state")
        )
        self.assertIsNone(recorded["exit_reason"])
        self.assertTrue(
            lifecycle.record_path.name.endswith(
                "-1201-process-1201-start.json"
            )
        )

    def test_idle_timeout_waits_for_the_active_request_to_finish(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
                idle_timeout_seconds=30,
            )

            with lifecycle.request():
                clock.monotonic += 60
                self.assertEqual(lifecycle.status()["lifecycle_state"], "active")
                self.assertIsNone(lifecycle.exit_reason_if_due())

            self.assertEqual(lifecycle.status()["idle_seconds"], 0)
            clock.monotonic += 30
            self.assertEqual(lifecycle.exit_reason_if_due(), "idle-timeout")
            recorded = json.loads(lifecycle.record_path.read_text(encoding="utf-8"))

        self.assertEqual(recorded["exit_reason"], "idle-timeout")
        self.assertEqual(recorded["active_requests"], 0)

    def test_status_distinguishes_orphaned_and_unknown_owner_records(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201, 1301}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)
            self.assertEqual(lifecycle.status()["lifecycle_state"], "orphaned")

            unknown = lifecycle.status()
            unknown["process_id"] = 1301
            unknown["parent_pid"] = 1
            unknown["process_identity"] = "process-1301-start"
            unknown_path = lifecycle.lifecycle_root / "unknown-1301.json"
            unknown_path.write_text(json.dumps(unknown), encoding="utf-8")

            statuses = inspect_mcp_process_records(
                lifecycle.lifecycle_root,
                process_alive=lambda pid: pid in alive,
                process_identity=lambda pid: f"process-{pid}-start",
            )

        by_pid = {item["process_id"]: item for item in statuses}
        self.assertEqual(by_pid[1201]["lifecycle_state"], "orphaned")
        self.assertTrue(by_pid[1201]["ownership_identity_bound"])
        self.assertEqual(by_pid[1301]["lifecycle_state"], "unknown-owner")
        self.assertFalse(by_pid[1301]["ownership_identity_bound"])

    def test_first_request_can_attribute_an_initially_unknown_client_task(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = McpProcessLifecycle(
                component="target-runtime",
                version="openubmc.target-runtime.v1",
                client="unknown-client",
                task_id="unknown-task",
                session_id="unknown-session",
                parent_pid=1200,
                process_id=1201,
                state_path=Path(raw) / "runtime-state",
                lifecycle_root=Path(raw) / "mcp-processes",
                idle_timeout_seconds=300,
                monotonic_clock=clock.monotonic_time,
                wall_clock=clock.wall_time,
                process_alive=lambda pid: pid == 1200,
                process_identity=lambda pid: f"process-{pid}-start",
            )
            self.assertEqual(lifecycle.status()["lifecycle_state"], "unknown-owner")

            lifecycle.attribute(
                client="codex",
                task_id="request-task",
                session_id="request-session",
            )
            recorded = json.loads(lifecycle.record_path.read_text(encoding="utf-8"))

        self.assertEqual(recorded["client"], "codex")
        self.assertEqual(recorded["task_id"], "request-task")
        self.assertEqual(recorded["session_id"], "request-session")
        self.assertEqual(recorded["lifecycle_state"], "idle")

    def test_unknown_owner_still_exits_after_confirmed_parent_loss(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = McpProcessLifecycle(
                component="target-runtime",
                version="openubmc.target-runtime.v1",
                client="unknown-client",
                task_id="unknown-task",
                session_id="unknown-session",
                parent_pid=1200,
                process_id=1201,
                state_path=Path(raw) / "runtime-state",
                lifecycle_root=Path(raw) / "mcp-processes",
                idle_timeout_seconds=300,
                monotonic_clock=clock.monotonic_time,
                wall_clock=clock.wall_time,
                process_alive=lambda pid: pid == 1201,
                process_identity=lambda pid: f"process-{pid}-start",
            )

            self.assertEqual(lifecycle.status()["lifecycle_state"], "orphaned")
            self.assertEqual(lifecycle.exit_reason_if_due(), "parent-exited")

    def test_unreadable_live_parent_identity_is_not_confirmed_orphaned(self) -> None:
        clock = FakeClock()
        identities = {1200: "process-1200-start", 1201: "process-1201-start"}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = McpProcessLifecycle(
                component="target-runtime",
                version="openubmc.target-runtime.v1",
                client="codex",
                task_id="task-100",
                session_id="session-100",
                parent_pid=1200,
                process_id=1201,
                state_path=Path(raw) / "runtime-state",
                lifecycle_root=Path(raw) / "mcp-processes",
                idle_timeout_seconds=300,
                monotonic_clock=clock.monotonic_time,
                wall_clock=clock.wall_time,
                process_alive=lambda pid: pid in {1200, 1201},
                process_identity=lambda pid: identities[pid],
            )
            identities[1200] = "unknown"

            self.assertEqual(lifecycle.status()["lifecycle_state"], "unknown-owner")
            statuses = inspect_mcp_process_records(
                lifecycle.lifecycle_root,
                process_alive=lambda pid: pid in {1200, 1201},
                process_identity=lambda pid: identities[pid],
            )

        self.assertEqual(statuses[0]["lifecycle_state"], "unknown-owner")
        self.assertTrue(statuses[0]["parent_identity_verified"])
        self.assertFalse(statuses[0]["parent_identity_currently_verified"])

    def test_verified_parent_identity_remains_bound_after_parent_exit(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)

            status = lifecycle.status()
            inspected = inspect_mcp_process_records(
                lifecycle.lifecycle_root,
                process_alive=lambda pid: pid in alive,
                process_identity=lambda pid: f"process-{pid}-start",
            )[0]

        self.assertEqual(status["lifecycle_state"], "orphaned")
        self.assertTrue(status["parent_identity_verified"])
        self.assertFalse(status["parent_identity_currently_verified"])
        self.assertTrue(inspected["parent_identity_verified"])
        self.assertFalse(inspected["parent_identity_currently_verified"])
        self.assertTrue(inspected["ownership_identity_bound"])

    def test_startup_parent_identity_stays_unknown_until_it_can_be_verified(self) -> None:
        clock = FakeClock()
        parent_identity = "unknown"
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = McpProcessLifecycle(
                component="target-runtime",
                version="openubmc.target-runtime.v1",
                client="codex",
                task_id="task-100",
                session_id="session-100",
                parent_pid=1200,
                process_id=1201,
                state_path=Path(raw) / "runtime-state",
                lifecycle_root=Path(raw) / "mcp-processes",
                idle_timeout_seconds=300,
                monotonic_clock=clock.monotonic_time,
                wall_clock=clock.wall_time,
                process_alive=lambda pid: pid == 1200,
                process_identity=lambda pid: (
                    parent_identity if pid == 1200 else "process-1201-start"
                ),
            )
            self.assertEqual(lifecycle.status()["lifecycle_state"], "unknown-owner")

            parent_identity = "process-1200-start"
            recovered = lifecycle.status()

        self.assertEqual(recovered["parent_identity"], "process-1200-start")
        self.assertTrue(recovered["parent_identity_verified"])
        self.assertEqual(recovered["lifecycle_state"], "idle")

    def test_explicit_task_closeout_drains_before_recording_exit(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
            )

            with lifecycle.request():
                lifecycle.request_task_closeout()
                self.assertIsNone(lifecycle.exit_reason_if_due())
                self.assertEqual(
                    lifecycle.status()["shutdown_requested"], "task-closeout"
                )

            self.assertEqual(lifecycle.exit_reason_if_due(), "task-closeout")
            recorded = json.loads(lifecycle.record_path.read_text(encoding="utf-8"))

        self.assertEqual(recorded["active_requests"], 0)
        self.assertEqual(recorded["exit_reason"], "task-closeout")

    def test_requested_shutdown_waits_for_the_active_request_to_finish(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
            )

            with lifecycle.request():
                lifecycle.request_exit("client-terminated")
                self.assertIsNone(lifecycle.exit_reason_if_due())
                self.assertIsNone(lifecycle.status()["exit_reason"])

            self.assertEqual(
                lifecycle.exit_reason_if_due(),
                "client-terminated",
            )

    def test_requested_shutdown_rejects_new_requests(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
            )

            lifecycle.request_exit("client-terminated")

            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                lifecycle.begin_request()

    def test_ownership_identifiers_must_be_strings(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "task_id must be a string"):
                McpProcessLifecycle(
                    component="target-runtime",
                    version="test",
                    client="codex",
                    task_id=None,  # type: ignore[arg-type]
                    session_id="session",
                    parent_pid=1200,
                    process_id=1201,
                    state_path=Path(raw) / "state",
                    lifecycle_root=Path(raw) / "processes",
                    idle_timeout_seconds=30,
                    monotonic_clock=clock.monotonic_time,
                    wall_clock=clock.wall_time,
                    process_alive=lambda pid: pid == 1200,
                    process_identity=lambda pid: f"process-{pid}-start",
                )
            for parent_pid in (True, -1, 1.5):
                with self.subTest(parent_pid=parent_pid), self.assertRaisesRegex(
                    ValueError, "parent_pid"
                ):
                    McpProcessLifecycle(
                        component="target-runtime",
                        version="test",
                        client="codex",
                        task_id="task",
                        session_id="session",
                        parent_pid=parent_pid,  # type: ignore[arg-type]
                        process_id=1201,
                        state_path=Path(raw) / "state",
                        lifecycle_root=Path(raw) / "processes-invalid",
                        idle_timeout_seconds=30,
                    )

    def test_idle_timeout_must_be_finite(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            for invalid in (float("nan"), float("inf")):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    self.make_lifecycle(
                        raw,
                        clock,
                        process_alive=lambda pid: pid == 1200,
                        idle_timeout_seconds=invalid,
                    )

    def test_cleanup_terminates_only_confirmed_orphaned_processes(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201, 1301, 1401}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)
            base = lifecycle.status()
            for process_id, parent_pid, identity in (
                (1301, 1300, "stale-identity"),
                (1401, 1, "process-1401-start"),
            ):
                record = {
                    **base,
                    "process_id": process_id,
                    "parent_pid": parent_pid,
                    "process_identity": identity,
                    "exit_reason": None,
                }
                (lifecycle.lifecycle_root / f"record-{process_id}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )

            with (
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True) as open_pidfd,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.select.poll",
                    return_value=ExitedPidfdPoll(),
                ),
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close"),
            ):
                open_pidfd.return_value = 17
                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda pid: f"process-{pid}-start",
                )
            inspected_after_signal = inspect_mcp_process_records(
                lifecycle.lifecycle_root,
                process_alive=lambda pid: pid in alive,
                process_identity=lambda pid: f"process-{pid}-start",
            )

        self.assertEqual(cleaned, [1201])
        send_signal.assert_called_once_with(17, signal.SIGTERM)
        cleaned_status = next(
            item for item in inspected_after_signal if item["process_id"] == 1201
        )
        self.assertIsNone(cleaned_status["exit_reason"])
        self.assertEqual(cleaned_status["lifecycle_state"], "orphaned")

    def test_upgrade_cleanup_retires_only_identity_bound_orphans_from_an_old_source(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201, 1301, 1401}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(raw, clock, process_alive=lambda pid: pid in alive)
            current = {
                **lifecycle.status(),
                "process_id": 1301,
                "process_identity": "process-1301-start",
                "source_commit": "b" * 40,
            }
            unrelated = {
                **lifecycle.status(),
                "process_id": 1401,
                "process_identity": "process-1401-start",
                "source_commit": "unknown-source-commit",
            }
            (lifecycle.lifecycle_root / "current.json").write_text(json.dumps(current))
            (lifecycle.lifecycle_root / "unrelated.json").write_text(json.dumps(unrelated))
            alive.remove(1200)
            handles = iter((17,))
            with (
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", side_effect=lambda *_: next(handles), create=True) as open_pidfd,
                mock.patch("openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True) as send_signal,
                mock.patch("openubmc_target_runtime.mcp_lifecycle.select.poll", return_value=ExitedPidfdPoll()),
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close"),
            ):
                cleaned = cleanup_retired_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    current_source_commit="b" * 40,
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda pid: f"process-{pid}-start",
                )

        self.assertEqual(cleaned, [1201])
        open_pidfd.assert_called_once_with(1201, 0)
        send_signal.assert_called_once_with(17, signal.SIGTERM)

    def test_cleanup_preserves_orphan_without_verified_ownership_binding(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1201,
            )
            with (
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True
                ) as open_pidfd,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
            ):
                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid == 1201,
                    process_identity=lambda pid: f"process-{pid}-start",
                )

        self.assertEqual(cleaned, [])
        open_pidfd.assert_not_called()
        send_signal.assert_not_called()

    def test_cleanup_scopes_owned_orphans_to_one_task_and_session(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201, 1301}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            second = {
                **lifecycle.status(),
                "task_id": "task-200",
                "session_id": "session-200",
                "process_id": 1301,
                "process_identity": "process-1301-start",
            }
            (lifecycle.lifecycle_root / "target-runtime-1301.json").write_text(
                json.dumps(second), encoding="utf-8"
            )
            alive.remove(1200)
            with (
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True,
                    return_value=17,
                ) as open_pidfd,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.select.poll",
                    return_value=ExitedPidfdPoll(),
                ),
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close"),
            ):
                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda pid: f"process-{pid}-start",
                )

        self.assertEqual(cleaned, [1201])
        open_pidfd.assert_called_once_with(1201, 0)
        send_signal.assert_called_once_with(17, signal.SIGTERM)

    def test_cleanup_signals_the_identity_bound_process_handle(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)
            with (
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True) as open_pidfd,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.select.poll",
                    return_value=ExitedPidfdPoll(),
                ),
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close") as close_pidfd,
            ):
                open_pidfd.return_value = 17

                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda pid: f"process-{pid}-start",
                )

        self.assertEqual(cleaned, [1201])
        open_pidfd.assert_called_once_with(1201, 0)
        send_signal.assert_called_once_with(17, signal.SIGTERM)
        close_pidfd.assert_called_once_with(17)

    def test_cleanup_does_not_report_cleaned_until_pidfd_confirms_exit(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)
            pending_poll = mock.Mock()
            pending_poll.poll.return_value = []
            with (
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True,
                    return_value=29,
                ),
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.select.poll",
                    return_value=pending_poll,
                ),
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close"),
            ):
                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda pid: f"process-{pid}-start",
                )

        self.assertEqual(cleaned, [])
        send_signal.assert_called_once_with(29, signal.SIGTERM)

    def test_cleanup_abandons_a_pid_reused_after_opening_the_process_handle(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201}
        identities = iter(
            (
                "process-1201-start",
                "process-1201-start",
                "reused-process-start",
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)
            with (
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True) as open_pidfd,
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close") as close_pidfd,
            ):
                open_pidfd.return_value = 19

                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda _pid: next(identities),
                )

        self.assertEqual(cleaned, [])
        send_signal.assert_not_called()
        close_pidfd.assert_called_once_with(19)

    def test_cleanup_rechecks_the_live_active_request_record_before_signal(self) -> None:
        clock = FakeClock()
        alive = {1200, 1201}
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid in alive,
            )
            alive.remove(1200)

            def open_and_mark_active(_process_id, _flags):
                record = json.loads(lifecycle.record_path.read_text(encoding="utf-8"))
                record["active_requests"] = 1
                lifecycle.record_path.write_text(json.dumps(record), encoding="utf-8")
                return 23

            with (
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.os.pidfd_open", create=True,
                    side_effect=open_and_mark_active,
                ),
                mock.patch(
                    "openubmc_target_runtime.mcp_lifecycle.signal.pidfd_send_signal", create=True
                ) as send_signal,
                mock.patch("openubmc_target_runtime.mcp_lifecycle.os.close") as close_pidfd,
            ):
                cleaned = cleanup_confirmed_orphaned_mcp_processes(
                    lifecycle.lifecycle_root,
                    task_id="task-100",
                    session_id="session-100",
                    process_alive=lambda pid: pid in alive,
                    process_identity=lambda pid: f"process-{pid}-start",
                )

        self.assertEqual(cleaned, [])
        send_signal.assert_not_called()
        close_pidfd.assert_called_once_with(23)

    def test_status_normalizes_stale_terminal_fields_for_a_live_process(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
            )
            stale = lifecycle.status()
            stale["exit_reason"] = "idle-timeout"
            stale["shutdown_requested"] = None
            lifecycle.record_path.write_text(json.dumps(stale), encoding="utf-8")

            status = inspect_mcp_process_records(
                lifecycle.lifecycle_root,
                process_alive=lambda pid: pid in {1200, 1201},
                process_identity=lambda pid: f"process-{pid}-start",
            )[0]

        self.assertEqual(status["lifecycle_state"], "idle")
        self.assertIsNone(status["exit_reason"])
        self.assertEqual(status["shutdown_requested"], "idle-timeout")

    def test_stdio_unexpected_writer_failure_records_server_error(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            lifecycle = self.make_lifecycle(
                raw,
                clock,
                process_alive=lambda pid: pid == 1200,
            )

            class Service:
                @staticmethod
                def close():
                    pass

            class Endpoint:
                service = Service()
                session_task_id = "error-session"

                @staticmethod
                def task_id_for_params(_params):
                    return "error-task"

                @staticmethod
                def handle(message):
                    return {"jsonrpc": "2.0", "id": message["id"], "result": {}}

            class BrokenWriter:
                @staticmethod
                def write(_value):
                    raise OSError("writer failed")

                @staticmethod
                def flush():
                    pass

            reader = io.StringIO(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                )
                + "\n"
            )
            with self.assertRaisesRegex(OSError, "writer failed"):
                StdioMcpServer(Endpoint(), process_lifecycle=lifecycle).serve(
                    reader=reader,
                    writer=BrokenWriter(),
                )
            recorded = json.loads(lifecycle.record_path.read_text(encoding="utf-8"))

        self.assertEqual(recorded["exit_reason"], "server-error")

    def test_stdio_processes_buffered_request_burst_without_waiting_for_eof(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lifecycle_root = Path(raw) / "mcp-processes"
            child_program = textwrap.dedent(
                """
                import os
                from pathlib import Path
                import sys

                from openubmc_target_runtime import McpProcessLifecycle, StdioMcpServer

                class Service:
                    def cancel_operation(self, *_args):
                        pass

                    def close(self):
                        pass

                class Endpoint:
                    session_task_id = "burst-session"

                    def __init__(self):
                        self.service = Service()

                    def task_id_for_params(self, _params):
                        return "burst-task"

                    def operation_id_for_params(self, _params, request_id):
                        return str(request_id)

                    def handle(self, message):
                        if "id" not in message:
                            return None
                        return {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {"method": message["method"]},
                        }

                root = Path(sys.argv[1])
                lifecycle = McpProcessLifecycle(
                    component="target-runtime",
                    version="test",
                    client="test-client",
                    task_id="burst-task",
                    session_id="burst-session",
                    parent_pid=os.getppid(),
                    state_path=root / "state",
                    lifecycle_root=root,
                    idle_timeout_seconds=30,
                )
                StdioMcpServer(
                    Endpoint(),
                    process_lifecycle=lifecycle,
                    lifecycle_poll_seconds=0.01,
                ).serve()
                """
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_program, str(lifecycle_root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(RUNTIME_ROOT)},
            )
            try:
                assert child.stdin is not None
                assert child.stdout is not None
                requests = (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {},
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        }
                    )
                    + "\n"
                ).encode()
                child.stdin.write(requests)
                child.stdin.flush()

                response_bytes = b""
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([child.stdout], [], [], 0.05)
                    if readable:
                        response_bytes += os.read(child.stdout.fileno(), 65536)
                    responses = [
                        json.loads(line)
                        for line in response_bytes.splitlines()
                        if line.strip()
                    ]
                    if {response["id"] for response in responses} == {1, 2}:
                        break

                self.assertEqual(
                    {response["id"] for response in responses},
                    {1, 2},
                    "buffered tools/list request waited for stdin EOF",
                )
            finally:
                if child.stdin is not None and not child.stdin.closed:
                    child.stdin.close()
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
                child.stdout.close()
                assert child.stderr is not None
                child.stderr.close()

    def test_stdio_task_closeout_drains_overlapping_responses_and_rejects_new_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lifecycle_root = Path(raw) / "mcp-processes"
            started_path = Path(raw) / "request-started"
            release_path = Path(raw) / "release-request"
            resource_count_path = Path(raw) / "resource-count"
            child_program = textwrap.dedent(
                """
                import os
                from pathlib import Path
                import sys
                import time

                from openubmc_target_runtime import (
                    JsonRpcMcpEndpoint,
                    McpProcessLifecycle,
                    RuntimeMcpService,
                    StdioMcpServer,
                )

                class Task:
                    def __init__(self, task_id, generation):
                        self.task_id = task_id
                        self.generation = generation

                class Backend:
                    created = 0

                    def open_task(self, task_id):
                        type(self).created += 1
                        Path(sys.argv[4]).write_text(
                            str(type(self).created), encoding="utf-8"
                        )
                        return Task(task_id, type(self).created)

                    def close_task(self, _task):
                        pass

                    def maintain_task(self, _task):
                        return 0

                    def task_status(self, task):
                        return {"task_id": task.task_id}

                    def debug_run(self, task, _arguments, context):
                        Path(sys.argv[2]).write_text("started", encoding="utf-8")
                        deadline = time.monotonic() + 2
                        while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        context.raise_if_stopped()
                        return {
                            "schema": "openubmc-debug.v1",
                            "task": task.task_id,
                            "root_cause": f"drained-{task.generation}",
                            "observed_at": "2026-09-01T00:00:00Z",
                            "freshness": {"status": "fresh"},
                        }

                class Service(RuntimeMcpService):
                    def call_exposed_tool(
                        self,
                        _name,
                        arguments,
                        *,
                        task_id,
                        operation_id,
                    ):
                        return self.registry.execute(
                            task_id=task_id,
                            operation_id=operation_id,
                            timeout_seconds=2,
                            callback=lambda task, context: self.backend.debug_run(
                                task, arguments, context
                            ),
                        )

                root = Path(sys.argv[1])
                lifecycle = McpProcessLifecycle(
                    component="target-runtime",
                    version="test",
                    client="codex",
                    task_id="closeout-task",
                    session_id="closeout-session",
                    parent_pid=os.getppid(),
                    state_path=root / "state",
                    lifecycle_root=root,
                    idle_timeout_seconds=30,
                )
                service = Service(Backend())
                StdioMcpServer(
                    JsonRpcMcpEndpoint(
                        service,
                        session_task_id="closeout-task",
                    ),
                    max_workers=1,
                    process_lifecycle=lifecycle,
                    lifecycle_poll_seconds=0.01,
                ).serve()
                """
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_program,
                    str(lifecycle_root),
                    str(started_path),
                    str(release_path),
                    str(resource_count_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(RUNTIME_ROOT)},
                text=True,
            )
            assert child.stdin is not None
            child.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "execute",
                            "arguments": {
                                "kind": "start",
                                "target": "192.0.2.10",
                                "intent": "diagnosis-only",
                                "entry_operation": "debug_run",
                                "entry_arguments": {},
                                "deadline": 2,
                            },
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            child.stdin.flush()
            deadline = time.monotonic() + 2
            while not started_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not started_path.exists():
                child.terminate()
                stdout, stderr = child.communicate(timeout=5)
                self.fail(
                    "real Runtime service did not start the request: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            for message in (
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "execute",
                        "arguments": {
                            "kind": "start",
                            "target": "192.0.2.10",
                            "intent": "diagnosis-only",
                            "entry_operation": "debug_run",
                            "entry_arguments": {},
                            "deadline": 2,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/openubmc-task-complete",
                    "params": {},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                    "params": {},
                },
            ):
                child.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            child.stdin.flush()
            time.sleep(0.1)
            release_path.write_text("release", encoding="utf-8")
            child.stdin.close()
            child.stdin = None

            stdout, stderr = child.communicate(timeout=5)
            responses = {
                document["id"]: document
                for document in (
                    json.loads(line) for line in stdout.splitlines() if line.strip()
                )
            }
            records = sorted(lifecycle_root.glob("*.json"))
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(set(responses), {1, 2, 3})
            self.assertFalse(responses[1]["result"]["isError"], responses[1])
            self.assertFalse(responses[2]["result"]["isError"], responses[2])
            self.assertEqual(responses[3]["error"]["code"], -32000)
            self.assertEqual(
                resource_count_path.read_text(encoding="utf-8"),
                "1",
                responses,
            )
            self.assertEqual(len(records), 1)
            recorded = json.loads(records[0].read_text(encoding="utf-8"))

        self.assertEqual(recorded["active_requests"], 0)
        self.assertEqual(recorded["exit_reason"], "task-closeout")

    def _assert_stdio_signal_drains_the_active_response_before_exit(
        self,
        termination_signal: signal.Signals,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lifecycle_root = Path(raw) / "mcp-processes"
            child_program = textwrap.dedent(
                """
                import os
                from pathlib import Path
                import sys
                import time

                from openubmc_target_runtime import McpProcessLifecycle, StdioMcpServer

                class Service:
                    def cancel_operation(self, *_args):
                        pass

                    def close(self):
                        pass

                class Endpoint:
                    session_task_id = "signal-session"

                    def __init__(self):
                        self.service = Service()

                    def task_id_for_params(self, _params):
                        return "signal-task"

                    def operation_id_for_params(self, _params, _request_id):
                        return "signal-operation"

                    def handle(self, message):
                        deadline = time.monotonic() + 10
                        while not (root / "release-response").exists():
                            if time.monotonic() >= deadline:
                                raise TimeoutError("parent did not release the active response")
                            time.sleep(0.01)
                        return {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "result": {"ok": True},
                        }

                root = Path(sys.argv[1])
                lifecycle = McpProcessLifecycle(
                    component="target-runtime",
                    version="test",
                    client="test-client",
                    task_id="signal-task",
                    session_id="signal-session",
                    parent_pid=os.getppid(),
                    state_path=root / "state",
                    lifecycle_root=root,
                    idle_timeout_seconds=30,
                )
                StdioMcpServer(
                    Endpoint(),
                    process_lifecycle=lifecycle,
                    lifecycle_poll_seconds=0.01,
                ).serve()
                """
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_program, str(lifecycle_root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(RUNTIME_ROOT),
                },
            )
            try:
                assert child.stdin is not None
                first_request = (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "observe", "arguments": {}},
                        }
                    )
                    + "\n"
                )
                child.stdin.write(first_request)
                child.stdin.flush()

                deadline = time.monotonic() + 5
                record_path = None
                while time.monotonic() < deadline:
                    records = list(lifecycle_root.glob("*.json"))
                    if records:
                        record = json.loads(records[0].read_text(encoding="utf-8"))
                        if record["active_requests"] == 1:
                            record_path = records[0]
                            break
                    time.sleep(0.01)
                self.assertIsNotNone(record_path, "request never became active")

                child.send_signal(termination_signal)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if record["shutdown_requested"] == "client-terminated":
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    record["shutdown_requested"],
                    "client-terminated",
                    "signal shutdown was not persisted",
                )
                child.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/progress",
                            "params": {"progressToken": "ignored", "progress": 1},
                        }
                    )
                    + "\n"
                )
                child.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        }
                    )
                    + "\n"
                )
                child.stdin.flush()
                assert child.stdout is not None
                self.assertTrue(
                    select.select([child.stdout], [], [], 5)[0],
                    "late request did not receive a shutdown response",
                )
                rejected_response = child.stdout.readline()
                self.assertEqual(json.loads(rejected_response)["id"], 2)
                (lifecycle_root / "release-response").touch()
                stdout, stderr = child.communicate(timeout=5)
                responses = [
                    json.loads(line)
                    for line in (rejected_response + stdout).splitlines()
                ]
                self.assertEqual(child.returncode, 0, stderr)
                by_id = {response["id"]: response for response in responses}
                self.assertEqual(set(by_id), {1, 2})
                self.assertEqual(by_id[1]["result"], {"ok": True})
                self.assertEqual(by_id[2]["error"]["code"], -32000)
                terminal = json.loads(record_path.read_text(encoding="utf-8"))
                self.assertEqual(terminal["active_requests"], 0)
                self.assertEqual(terminal["exit_reason"], "client-terminated")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_stdio_sigterm_drains_the_active_response_before_exit(self) -> None:
        self._assert_stdio_signal_drains_the_active_response_before_exit(
            signal.SIGTERM
        )

    def test_stdio_sigint_drains_the_active_response_before_exit(self) -> None:
        self._assert_stdio_signal_drains_the_active_response_before_exit(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
