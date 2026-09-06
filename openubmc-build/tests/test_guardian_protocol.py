from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


def init_repo(path: Path) -> None:
    run("git", "init", "-q", str(path))
    run("git", "-C", str(path), "config", "user.email", "test@example.com")
    run("git", "-C", str(path), "config", "user.name", "Guardian Test")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    run("git", "-C", str(path), "add", "tracked.txt")
    committed = run("git", "-C", str(path), "commit", "-qm", "baseline")
    if committed.returncode:
        raise RuntimeError(committed.stderr)


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class GuardianProtocolTests(unittest.TestCase):
    def make_plan(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        init_repo(repo)
        records = root / "records.jsonl"
        program = root / "build.py"
        program.write_text(
            "import json, os, pathlib, signal, sys, time\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "existing = path.read_text(encoding='utf-8').splitlines() if path.exists() else []\n"
            "with path.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}) + '\\n')\n"
            "    handle.flush()\n"
            "if not existing:\n"
            "    time.sleep(30)\n",
            encoding="utf-8",
        )
        plan_path = root / "plan.json"
        run_root = root / "runs"
        planned = run(
            sys.executable,
            str(BUILD_ROOT / "scripts" / "create_build_plan.py"),
            "--mode",
            "validate",
            "--workspace",
            f"component={repo}",
            "--cwd",
            str(repo),
            "--output",
            str(plan_path),
            "--run-root",
            str(run_root),
            "--",
            sys.executable,
            str(program),
            str(records),
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        return plan_path, run_root, records

    def test_guardian_death_before_prepared_handoff_never_executes_user_argv(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            executed = root / "executed"
            program = root / "quick-build.py"
            program.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(BUILD_ROOT / "scripts" / "create_build_plan.py"),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(program),
                str(executed),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            hook_root = root / "hook"
            hook_root.mkdir()
            guardian_marker = root / "guardian.pid"
            (hook_root / "sitecustomize.py").write_text(
                "import os, pathlib, signal\n"
                "from multiprocessing.connection import Connection\n"
                "_send_original = Connection.send\n"
                "def _send(self, message):\n"
                "    marker = os.environ.get('GUARDIAN_STOP_MARKER')\n"
                "    if marker and isinstance(message, dict) and message.get('event') == 'prepared':\n"
                "        pathlib.Path(marker).write_text(str(os.getpid()), encoding='utf-8')\n"
                "        os.kill(os.getpid(), signal.SIGSTOP)\n"
                "    return _send_original(self, message)\n"
                "Connection.send = _send\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(hook_root)
            environment["GUARDIAN_STOP_MARKER"] = str(guardian_marker)
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                self.assertTrue(wait_for(guardian_marker.exists, 5))
                self.assertFalse(executed.exists())
                os.kill(int(guardian_marker.read_text(encoding="utf-8")), signal.SIGKILL)
                first.communicate(timeout=15)
                self.assertFalse(executed.exists())

                environment.pop("GUARDIAN_STOP_MARKER")
                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                    env=environment,
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertEqual(executed.read_text(encoding="utf-8"), "executed")
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate(timeout=5)

    def test_guardian_death_at_started_handoff_cannot_leave_an_unlocked_writer(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan_path, run_root, records = self.make_plan(root)
            hook_root = root / "hook"
            hook_root.mkdir()
            guardian_marker = root / "guardian.pid"
            (hook_root / "sitecustomize.py").write_text(
                "import os, pathlib, signal\n"
                "from multiprocessing.connection import Connection\n"
                "_send_original = Connection.send\n"
                "def _send(self, message):\n"
                "    marker = os.environ.get('GUARDIAN_STOP_MARKER')\n"
                "    if marker and isinstance(message, dict) and message.get('event') == 'started':\n"
                "        pathlib.Path(marker).write_text(str(os.getpid()), encoding='utf-8')\n"
                "        os.kill(os.getpid(), signal.SIGSTOP)\n"
                "    return _send_original(self, message)\n"
                "Connection.send = _send\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(hook_root)
            environment["GUARDIAN_STOP_MARKER"] = str(guardian_marker)
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            first_record: dict[str, int] | None = None
            try:
                self.assertTrue(
                    wait_for(lambda: records.exists() and guardian_marker.exists(), 5),
                    "guardian did not reach the started handoff seam",
                )
                first_record = json.loads(
                    records.read_text(encoding="utf-8").splitlines()[0]
                )
                os.kill(int(guardian_marker.read_text(encoding="utf-8")), signal.SIGKILL)
                _stdout, _stderr = first.communicate(timeout=15)

                self.assertFalse(process_alive(int(first_record["pid"])))
                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertEqual(len(records.read_text(encoding="utf-8").splitlines()), 2)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate(timeout=5)
                if first_record and process_alive(int(first_record["pid"])):
                    try:
                        os.killpg(int(first_record["pgid"]), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_runner_sigkill_keeps_lock_until_guardian_finishes_cleanup(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan_path, run_root, records = self.make_plan(root)
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_record: dict[str, int] | None = None
            try:
                self.assertTrue(wait_for(records.exists, 5))
                first_record = json.loads(
                    records.read_text(encoding="utf-8").splitlines()[0]
                )
                first.kill()
                first.wait(timeout=3)
                if first.stdout is not None:
                    first.stdout.close()
                if first.stderr is not None:
                    first.stderr.close()

                blocked = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("workspace_or_plan_already_running", blocked.stderr)

                states = list(run_root.glob("plans/*/attempts/*/state.json"))
                self.assertEqual(len(states), 1)
                self.assertTrue(
                    wait_for(
                        lambda: (
                            not process_alive(int(first_record["pid"]))
                            and json.loads(states[0].read_text(encoding="utf-8")).get("status")
                            == "interrupted"
                        ),
                        12,
                    ),
                    "guardian did not finish fail-closed cleanup",
                )

                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertEqual(len(records.read_text(encoding="utf-8").splitlines()), 2)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate(timeout=5)
                if first_record and process_alive(int(first_record["pid"])):
                    try:
                        os.killpg(int(first_record["pgid"]), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_setsid_descendant_is_reaped_before_success_and_lock_release(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            records = root / "records.jsonl"
            escaped_pid_path = root / "escaped.pid"
            heartbeat = root / "escaped.heartbeat"
            program = root / "setsid-build.py"
            program.write_text(
                "import json, os, pathlib, signal, sys, time\n"
                "records, pid_path, heartbeat = map(pathlib.Path, sys.argv[1:4])\n"
                "existing = records.read_text(encoding='utf-8').splitlines() if records.exists() else []\n"
                "if existing:\n"
                "    escaped_pid = int(pid_path.read_text(encoding='utf-8'))\n"
                "    try:\n"
                "        raw = pathlib.Path(f'/proc/{escaped_pid}/stat').read_text(encoding='utf-8')\n"
                "    except FileNotFoundError:\n"
                "        overlap = False\n"
                "    else:\n"
                "        closing = raw.rfind(')')\n"
                "        overlap = closing >= 0 and raw[closing + 2:].split()[0] != 'Z'\n"
                "    with records.open('a', encoding='utf-8') as handle:\n"
                "        handle.write(json.dumps({'role': 'retry', 'overlap': overlap}) + '\\n')\n"
                "    raise SystemExit(0)\n"
                "escaped_pid = os.fork()\n"
                "if escaped_pid == 0:\n"
                "    os.setsid()\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    pid_path.write_text(str(os.getpid()), encoding='utf-8')\n"
                "    while True:\n"
                "        heartbeat.write_text(str(time.monotonic_ns()), encoding='utf-8')\n"
                "        time.sleep(0.02)\n"
                "deadline = time.monotonic() + 5\n"
                "while not pid_path.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.01)\n"
                "with records.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps({'role': 'first', 'escaped_pid': escaped_pid}) + '\\n')\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(BUILD_ROOT / "scripts" / "create_build_plan.py"),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(program),
                str(records),
                str(escaped_pid_path),
                str(heartbeat),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            escaped_pid: int | None = None
            try:
                first = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertTrue(first.stdout, first.stderr)
                first_result = json.loads(first.stdout)
                first_state = json.loads(
                    Path(first_result["state_path"]).read_text(encoding="utf-8")
                )
                self.assertTrue(escaped_pid_path.exists(), first.stderr)
                escaped_pid = int(escaped_pid_path.read_text(encoding="utf-8"))
                self.assertNotEqual(first.returncode, 0)
                self.assertEqual(first_state["status"], "failed")
                self.assertTrue(first_state["signal_escalated"])

                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                recorded = [
                    json.loads(line)
                    for line in records.read_text(encoding="utf-8").splitlines()
                ]

                self.assertFalse(
                    process_alive(escaped_pid),
                    f"attempt ended as {first_state['status']} while setsid descendant lived",
                )
                self.assertEqual([entry["role"] for entry in recorded], ["first", "retry"])
                self.assertFalse(recorded[1]["overlap"])
            finally:
                if escaped_pid is not None and process_alive(escaped_pid):
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    wait_for(lambda: not process_alive(escaped_pid), 3)

    def test_runner_sigkill_keeps_lock_until_setsid_descendant_is_reaped(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            records = root / "records.jsonl"
            escaped_pid_path = root / "escaped.pid"
            program = root / "setsid-running-build.py"
            program.write_text(
                "import json, os, pathlib, signal, sys, time\n"
                "records, pid_path = map(pathlib.Path, sys.argv[1:3])\n"
                "existing = records.read_text(encoding='utf-8').splitlines() if records.exists() else []\n"
                "if existing:\n"
                "    escaped_pid = int(pid_path.read_text(encoding='utf-8'))\n"
                "    try:\n"
                "        raw = pathlib.Path(f'/proc/{escaped_pid}/stat').read_text(encoding='utf-8')\n"
                "    except FileNotFoundError:\n"
                "        overlap = False\n"
                "    else:\n"
                "        closing = raw.rfind(')')\n"
                "        overlap = closing >= 0 and raw[closing + 2:].split()[0] != 'Z'\n"
                "    with records.open('a', encoding='utf-8') as handle:\n"
                "        handle.write(json.dumps({'role': 'retry', 'overlap': overlap}) + '\\n')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "escaped_pid = os.fork()\n"
                "if escaped_pid == 0:\n"
                "    os.setsid()\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    pid_path.write_text(str(os.getpid()), encoding='utf-8')\n"
                "    while True:\n"
                "        time.sleep(1)\n"
                "deadline = time.monotonic() + 5\n"
                "while not pid_path.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.01)\n"
                "with records.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps({'role': 'first', 'pid': os.getpid(), 'escaped_pid': escaped_pid}) + '\\n')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(BUILD_ROOT / "scripts" / "create_build_plan.py"),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(program),
                str(records),
                str(escaped_pid_path),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_record: dict[str, int] | None = None
            try:
                self.assertTrue(wait_for(records.exists, 5))
                first_record = json.loads(
                    records.read_text(encoding="utf-8").splitlines()[0]
                )
                first.kill()
                first.wait(timeout=3)
                if first.stdout is not None:
                    first.stdout.close()
                if first.stderr is not None:
                    first.stderr.close()

                blocked = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("workspace_or_plan_already_running", blocked.stderr)

                escaped_pid = int(first_record["escaped_pid"])
                states = list(run_root.glob("plans/*/attempts/*/state.json"))
                self.assertEqual(len(states), 1)
                self.assertTrue(
                    wait_for(
                        lambda: (
                            not process_alive(escaped_pid)
                            and json.loads(states[0].read_text(encoding="utf-8")).get("status")
                            == "interrupted"
                        ),
                        12,
                    ),
                    "guardian released neither the setsid descendant nor the interrupted Attempt",
                )

                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                recorded = [
                    json.loads(line)
                    for line in records.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual([entry["role"] for entry in recorded], ["first", "retry"])
                self.assertFalse(recorded[1]["overlap"])
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate(timeout=5)
                if first_record:
                    for key in ("pid", "escaped_pid"):
                        pid = int(first_record[key])
                        if process_alive(pid):
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

    def test_guardian_sigkill_leaves_runner_holding_lock_until_setsid_descendant_is_reaped(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            records = root / "records.jsonl"
            escaped_pid_path = root / "escaped.pid"
            program = root / "setsid-guardian-loss-build.py"
            program.write_text(
                "import json, os, pathlib, signal, sys, time\n"
                "records, pid_path = map(pathlib.Path, sys.argv[1:3])\n"
                "existing = records.read_text(encoding='utf-8').splitlines() if records.exists() else []\n"
                "if existing:\n"
                "    escaped_pid = int(pid_path.read_text(encoding='utf-8'))\n"
                "    try:\n"
                "        raw = pathlib.Path(f'/proc/{escaped_pid}/stat').read_text(encoding='utf-8')\n"
                "    except FileNotFoundError:\n"
                "        overlap = False\n"
                "    else:\n"
                "        closing = raw.rfind(')')\n"
                "        overlap = closing >= 0 and raw[closing + 2:].split()[0] != 'Z'\n"
                "    with records.open('a', encoding='utf-8') as handle:\n"
                "        handle.write(json.dumps({'role': 'retry', 'overlap': overlap}) + '\\n')\n"
                "    raise SystemExit(0)\n"
                "escaped_pid = os.fork()\n"
                "if escaped_pid == 0:\n"
                "    os.setsid()\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    pid_path.write_text(str(os.getpid()), encoding='utf-8')\n"
                "    while True:\n"
                "        time.sleep(1)\n"
                "deadline = time.monotonic() + 5\n"
                "while not pid_path.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.01)\n"
                "with records.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps({'role': 'first', 'pid': os.getpid(), 'escaped_pid': escaped_pid}) + '\\n')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(BUILD_ROOT / "scripts" / "create_build_plan.py"),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(program),
                str(records),
                str(escaped_pid_path),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            hook_root = root / "hook"
            hook_root.mkdir()
            block_scope = root / "block-runner-proc-scope"
            (hook_root / "sitecustomize.py").write_text(
                "import os, pathlib\n"
                "_iterdir_original = pathlib.Path.iterdir\n"
                "def _iterdir(self):\n"
                "    marker = os.environ.get('PROC_BLOCK_MARKER')\n"
                "    if marker and str(self) == '/proc' and pathlib.Path(marker).exists():\n"
                "        raise PermissionError(13, 'injected unobservable runner scope')\n"
                "    return _iterdir_original(self)\n"
                "pathlib.Path.iterdir = _iterdir\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(hook_root)
            environment["PROC_BLOCK_MARKER"] = str(block_scope)
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            first_record: dict[str, int] | None = None
            try:
                def running_state() -> dict[str, object] | None:
                    state_paths = list(run_root.glob("plans/*/attempts/*/state.json"))
                    if len(state_paths) != 1 or not records.exists():
                        return None
                    state = json.loads(state_paths[0].read_text(encoding="utf-8"))
                    return state if state.get("status") == "running" else None

                self.assertTrue(wait_for(lambda: running_state() is not None, 5))
                state = running_state()
                self.assertIsNotNone(state)
                first_record = json.loads(
                    records.read_text(encoding="utf-8").splitlines()[0]
                )
                block_scope.write_text("blocked", encoding="utf-8")
                os.kill(int(state["lock_guardian_pid"]), signal.SIGKILL)
                time.sleep(0.2)
                self.assertIsNone(first.poll())
                blocked = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("workspace_or_plan_already_running", blocked.stderr)

                block_scope.unlink()
                _stdout, stderr = first.communicate(timeout=12)
                self.assertNotEqual(first.returncode, 0, stderr)

                escaped_pid = int(first_record["escaped_pid"])
                self.assertFalse(
                    process_alive(escaped_pid),
                    "runner returned after guardian loss while the setsid descendant lived",
                )
                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                recorded = [
                    json.loads(line)
                    for line in records.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual([entry["role"] for entry in recorded], ["first", "retry"])
                self.assertFalse(recorded[1]["overlap"])
            finally:
                if block_scope.exists():
                    block_scope.unlink()
                if first.poll() is None:
                    first.kill()
                    first.communicate(timeout=5)
                if first_record:
                    for key in ("pid", "escaped_pid"):
                        pid = int(first_record[key])
                        if process_alive(pid):
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

    def test_unobservable_descendant_scope_keeps_lock_until_cleanup_can_be_proven(self) -> None:
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            executed = root / "executed"
            program = root / "quick-build.py"
            program.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(BUILD_ROOT / "scripts" / "create_build_plan.py"),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(program),
                str(executed),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            hook_root = root / "hook"
            hook_root.mkdir()
            block_scope = root / "block-proc-scope"
            block_scope.write_text("blocked", encoding="utf-8")
            (hook_root / "sitecustomize.py").write_text(
                "import os, pathlib\n"
                "_iterdir_original = pathlib.Path.iterdir\n"
                "def _iterdir(self):\n"
                "    marker = os.environ.get('PROC_BLOCK_MARKER')\n"
                "    if marker and str(self) == '/proc' and pathlib.Path(marker).exists():\n"
                "        raise PermissionError(13, 'injected unobservable process scope')\n"
                "    return _iterdir_original(self)\n"
                "pathlib.Path.iterdir = _iterdir\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(hook_root)
            environment["PROC_BLOCK_MARKER"] = str(block_scope)
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                self.assertTrue(wait_for(executed.exists, 5))
                time.sleep(0.2)
                self.assertIsNone(first.poll())

                blocked = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("workspace_or_plan_already_running", blocked.stderr)
                states = list(run_root.glob("plans/*/attempts/*/state.json"))
                self.assertEqual(len(states), 1)
                self.assertEqual(
                    json.loads(states[0].read_text(encoding="utf-8"))["status"],
                    "running",
                )

                block_scope.unlink()
                stdout, stderr = first.communicate(timeout=10)
                self.assertEqual(first.returncode, 0, f"{stderr}\nstdout={stdout}")
                self.assertEqual(json.loads(stdout)["status"], "succeeded")

                retry = run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
            finally:
                if block_scope.exists():
                    block_scope.unlink()
                if first.poll() is None:
                    first.kill()
                    first.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
