from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CLI_SUBPROCESS_TIMEOUT = 60
sys.path.insert(0, str(SCRIPTS))
from deploy_live_file import (  # type: ignore  # noqa: E402
    atomic_backup_command,
    atomic_install_command,
    atomic_restore_command,
    remote_path_guard_command,
)


class LivePatchCliTests(unittest.TestCase):
    maxDiff = None

    def test_direct_clis_load_one_runtime_backend_adapter(self) -> None:
        code = textwrap.dedent(
            f"""
            import sys

            sys.path.insert(0, {str(SCRIPTS)!r})
            import deploy_live_file
            import rollback_live_file
            from openubmc_live_patch import runtime_backend

            assert (
                deploy_live_file.run_runtime_mutation
                is rollback_live_file.run_runtime_mutation
            )
            assert "target_runtime_adapter" not in sys.modules
            assert runtime_backend._adapter is not None
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=SKILL_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=CLI_SUBPROCESS_TIMEOUT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def run_cli(
        self,
        script: str,
        *args: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env:
            command_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=cwd,
            env=command_env,
            text=True,
            capture_output=True,
            timeout=CLI_SUBPROCESS_TIMEOUT,
        )

    def write_local_file(self, directory: Path) -> Path:
        local = directory / "unit_manager.lua"
        local.write_text("return { enabled = true }\n", encoding="utf-8")
        return local

    def make_fake_runtime(self, directory: Path, local: Path) -> dict[str, str]:
        command_log = directory / "remote-commands.jsonl"
        ssh_log = directory / "ssh.json"
        sshpass_log = directory / "sshpass.json"
        runtime_package = directory / "openubmc_target_runtime"
        shutil.copytree(
            SKILL_ROOT.parent
            / "openubmc-target-runtime"
            / "openubmc_target_runtime",
            runtime_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        with (runtime_package / "__init__.py").open("a", encoding="utf-8") as stream:
            stream.write(
                textwrap.dedent(
                    """

                    import base64 as _fake_base64
                    import gzip as _fake_gzip
                    import json as _fake_json
                    import os as _fake_os
                    from pathlib import Path as _FakePath
                    import re as _fake_re

                    _fake_current_mode = "644"
                    _fake_current_uid = 0
                    _fake_current_gid = 0

                    def _fake_decoded_script(command):
                        match = _fake_re.search(
                            r"printf %s '?([A-Za-z0-9+/=]+)'?\\|busybox base64 -d",
                            command,
                        )
                        if match is None:
                            raise RuntimeError("compressed script unavailable")
                        return _fake_gzip.decompress(
                            _fake_base64.b64decode(match.group(1))
                        ).decode("utf-8")

                    def load_selected_credentials_file():
                        return {}

                    def telnet_connect(*args, **kwargs):
                        return object()

                    def close_telnet(_tn):
                        return None

                    def run_telnet_command(_tn, command, **_kwargs):
                        global _fake_current_mode, _fake_current_uid, _fake_current_gid
                        log = _FakePath(_fake_os.environ["FAKE_REMOTE_LOG"])
                        with log.open("a", encoding="utf-8") as output:
                            output.write(_fake_json.dumps({"command": command}) + "\\n")
                        returncode = 0
                        if "live_patch_paths_safe" in command:
                            if _fake_os.environ.get("FAKE_PATH_GUARD_FAILURE") == "1":
                                text = "live_patch_paths_unsafe"
                                returncode = 1
                            else:
                                text = "live_patch_paths_safe"
                        elif "live_patch_codec_ready" in command:
                            text = "live_patch_codec_ready"
                        elif "/proc/mounts" in command:
                            text = _fake_os.environ.get("FAKE_ROOT_MOUNT_OPTIONS", "ro,relatime")
                        elif "remount_rw_ok" in command:
                            if _fake_os.environ.get("FAKE_REMOUNT_RW_FAILURE") == "1":
                                text = "remount_rw_failed"
                                returncode = 1
                            else:
                                text = "remount_rw_ok"
                        elif "remount_ro_ok" in command:
                            text = "remount_ro_ok"
                        elif "target_exists" in command:
                            if _fake_os.environ.get("FAKE_TARGET_EXISTS") == "1":
                                text = (
                                    _fake_os.environ["FAKE_LOCAL_SHA256"]
                                    + "  /remote/file\\n"
                                    + "target_mode=440 target_uid=104 target_gid=104\\n"
                                    + "target_exists"
                                )
                            else:
                                text = "target_missing"
                        elif "p=b;" in command:
                            if _fake_os.environ.get("FAKE_BACKUP_FAILURE") == "1":
                                text = "backup_failed"
                                returncode = 1
                            else:
                                text = (
                                    "backup_sha256="
                                    + _fake_os.environ["FAKE_LOCAL_SHA256"]
                                    + "\\nbackup_mode=440\\nbackup_uid=104\\nbackup_gid=104\\nbackup_ok"
                                )
                        elif "p=i;" in command:
                            script = _fake_decoded_script(command)
                            mode = _fake_re.search(r"chmod ([0-7]{3,4}) ", script)
                            owner = _fake_re.search(r"chown ([0-9]+):([0-9]+) ", script)
                            _fake_current_mode = mode.group(1) if mode else "644"
                            if owner:
                                _fake_current_uid = int(owner.group(1))
                                _fake_current_gid = int(owner.group(2))
                            digest = (
                                "0" * 64
                                if _fake_os.environ.get("FAKE_SHA_MISMATCH") == "1"
                                else _fake_os.environ["FAKE_LOCAL_SHA256"]
                            )
                            text = (
                                f"remote_sha256={digest}\\n"
                                f"remote_mode={_fake_current_mode}\\n"
                                f"remote_uid={_fake_current_uid}\\n"
                                f"remote_gid={_fake_current_gid}\\ndeploy_ok"
                            )
                        elif "p=r;" in command:
                            script = _fake_decoded_script(command)
                            mode = _fake_re.search(r"chmod ([0-7]{3,4}) ", script)
                            _fake_current_mode = mode.group(1) if mode else "440"
                            _fake_current_uid = 104
                            _fake_current_gid = 104
                            backup_sha = _fake_os.environ["FAKE_LOCAL_SHA256"]
                            remote_sha = (
                                "0" * 64
                                if _fake_os.environ.get("FAKE_ROLLBACK_SHA_MISMATCH") == "1"
                                else backup_sha
                            )
                            text = (
                                f"backup_sha256={backup_sha}\\n"
                                f"remote_sha256={remote_sha}\\n"
                                "backup_mode=440\\n"
                                f"remote_mode={_fake_current_mode}\\n"
                                "backup_uid=104\\n"
                                f"remote_uid={_fake_current_uid}\\n"
                                "backup_gid=104\\n"
                                f"remote_gid={_fake_current_gid}\\nrestore_ok"
                            )
                        elif "verify_sha256" in command:
                            digest = _fake_os.environ["FAKE_LOCAL_SHA256"]
                            text = (
                                f"remote_sha256={digest}\\n"
                                f"remote_mode={_fake_current_mode}\\n"
                                f"remote_uid={_fake_current_uid}\\n"
                                f"remote_gid={_fake_current_gid}\\nverify_sha256"
                            )
                        elif "restart_ok" in command:
                            text = "restart_ok"
                        else:
                            text = "ok"
                        return TelnetCommandResult(
                            stdout=text,
                            returncode=returncode,
                            framing_complete=True,
                            timed_out=False,
                            connection_closed=False,
                            raw=text.encode(),
                        )

                    def run_telnet_command_text(tn, command, **kwargs):
                        result = run_telnet_command(tn, command, **kwargs)
                        if not result.ok:
                            raise RuntimeError(result.stdout)
                        return result.stdout
                    """
                )
            )

        ssh = directory / "ssh"
        ssh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import sys

                argv = sys.argv[1:]
                control_path = ""
                for index, value in enumerate(argv[:-1]):
                    if value == "-o" and argv[index + 1].startswith("ControlPath="):
                        control_path = argv[index + 1].split("=", 1)[1]
                if "-M" in argv:
                    Path(control_path).touch()
                    raise SystemExit(0)
                if "-O" in argv:
                    operation = argv[argv.index("-O") + 1]
                    success = bool(control_path and Path(control_path).exists())
                    if operation == "exit":
                        Path(control_path).unlink(missing_ok=True)
                        success = True
                    raise SystemExit(0 if success else 1)
                if any("cat >" in value for value in argv):
                    sys.stdin.buffer.read()
                    Path(os.environ["FAKE_SSH_LOG"]).write_text(
                        json.dumps({
                            "argv": (["-e"] if os.environ.get("FAKE_SSHPASS_USED") else []) + argv,
                            "sspass_present": bool(os.environ.get("SSHPASS")),
                            "unrelated_passwords_present": any(
                                os.environ.get(name)
                                for name in ("OPENUBMC_SSH_PASSWORD", "OPENUBMC_TELNET_PASSWORD")
                            ),
                        }),
                        encoding="utf-8",
                    )
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        sshpass = directory / "sshpass"
        sshpass.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import sys

                Path(os.environ["FAKE_SSHPASS_LOG"]).write_text(
                    json.dumps({
                        "argv": sys.argv[1:],
                        "sspass_present": bool(os.environ.get("SSHPASS")),
                        "unrelated_passwords_present": any(
                            os.environ.get(name)
                            for name in ("OPENUBMC_SSH_PASSWORD", "OPENUBMC_TELNET_PASSWORD")
                        ),
                    }),
                    encoding="utf-8",
                )
                os.environ["FAKE_SSHPASS_USED"] = "1"
                os.execvp(sys.argv[2], sys.argv[2:])
                """
            ),
            encoding="utf-8",
        )
        sshpass.chmod(0o755)

        debug_scripts = directory / "debug-scripts"
        debug_scripts.mkdir()
        for name, body in {
            "preflight_remote.py": "import os; raise SystemExit(int(os.environ.get('FAKE_PREFLIGHT_RC', '0')))\n",
            "collect_logs.py": (
                "import os\n"
                "print(os.environ.get('FAKE_LOG_TEXT', "
                "'check startup status completely, total components count: 4, normal count: 4'))\n"
                "raise SystemExit(int(os.environ.get('FAKE_LOG_RC', '0')))\n"
            ),
            "mdbctl_remote.py": "import os; raise SystemExit(int(os.environ.get('FAKE_MDBCTL_RC', '0')))\n",
        }.items():
            (debug_scripts / name).write_text(body, encoding="utf-8")

        return {
            "OPENUBMC_DEBUG_SCRIPTS": str(debug_scripts),
            "OPENUBMC_SSH_USER": "Administrator",
            "OPENUBMC_SSH_PASSWORD": "super-secret-password",
            "OPENUBMC_TELNET_USER": "Administrator",
            "OPENUBMC_TELNET_PASSWORD": "telnet-secret-password",
            "FAKE_REMOTE_LOG": str(command_log),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_SSHPASS_LOG": str(sshpass_log),
            "FAKE_LOCAL_SHA256": hashlib.sha256(local.read_bytes()).hexdigest(),
            "OPENUBMC_TARGET_RUNTIME_STATE_DIR": str(directory / "runtime-state"),
            "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(directory)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }

    def read_commands(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [json.loads(line)["command"] for line in path.read_text(encoding="utf-8").splitlines()]

    def test_direct_deploy_is_plan_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--json",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["authorization_live_patch"])
        self.assertFalse(payload["will_restart"])

    def test_apply_requires_intent_authorization_and_explicit_restart_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            common = (
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--json",
            )
            missing_intent = self.run_cli("deploy_live_file.py", *common)
            missing_scope = self.run_cli(
                "deploy_live_file.py",
                *common,
                "--intent",
                "live_patch",
                "--authorize-live-patch",
            )
            missing_authorization = self.run_cli(
                "deploy_live_file.py",
                *common,
                "--intent",
                "live_patch",
                "--restart-scope",
                "none",
            )

        self.assertEqual(missing_intent.returncode, 2)
        self.assertIn("--intent live_patch", json.loads(missing_intent.stdout)["error"])
        self.assertEqual(missing_scope.returncode, 2)
        self.assertIn("--restart-scope", json.loads(missing_scope.stdout)["error"])
        self.assertEqual(missing_authorization.returncode, 2)
        self.assertIn("authorization.live_patch", json.loads(missing_authorization.stdout)["error"])

    def test_remote_path_traversal_is_rejected_before_contact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/../../etc/passwd",
                "--json",
            )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_deploy_symlink_guard_blocks_external_target_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            allowed = root / "allowed"
            stage_dir = root / "stage"
            allowed.mkdir()
            stage_dir.mkdir()
            outside = root / "outside"
            outside.write_text("outside-original", encoding="utf-8")
            target = allowed / "unit.lua"
            target.symlink_to(outside)
            staging_payload = stage_dir / "payload"
            staging_payload.write_text("replacement", encoding="utf-8")
            expected_sha = hashlib.sha256(staging_payload.read_bytes()).hexdigest()
            command = remote_path_guard_command(
                files=[(str(target), str(allowed), False)],
                directories=[],
            ) + " && " + atomic_install_command(
                str(staging_payload),
                str(stage_dir),
                str(target),
                "644",
                expected_sha,
                "deploy-symlink-test",
            )

            completed = subprocess.run(
                ["sh", "-c", command], capture_output=True, text=True, check=False
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(target.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside-original")

    def test_parent_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (allowed / "linked").symlink_to(outside, target_is_directory=True)
            target = allowed / "linked" / "unit.lua"

            completed = subprocess.run(
                [
                    "sh",
                    "-c",
                    remote_path_guard_command(
                        files=[(str(target), str(allowed), False)],
                        directories=[],
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((outside / "unit.lua").exists())

    def test_atomic_backups_are_unique_and_do_not_overwrite_prior_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "unit.lua"
            target.write_text("first", encoding="utf-8")
            first_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            first_backup = root / "unit.lua.bak.same-second.first"
            first = subprocess.run(
                [
                    "sh",
                    "-c",
                    atomic_backup_command(
                        str(target), str(first_backup), first_sha, "first"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            target.write_text("second", encoding="utf-8")
            second_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            second_backup = root / "unit.lua.bak.same-second.second"
            second = subprocess.run(
                [
                    "sh",
                    "-c",
                    atomic_backup_command(
                        str(target), str(second_backup), second_sha, "second"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first_backup.read_text(encoding="utf-8"), "first")
            self.assertEqual(second_backup.read_text(encoding="utf-8"), "second")
            self.assertNotEqual(first_backup, second_backup)

    def test_apply_fails_when_existing_target_cannot_be_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env.update({"FAKE_TARGET_EXISTS": "1", "FAKE_BACKUP_FAILURE": "1"})
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("backup", payload["error"].lower())

    def test_apply_stops_before_staging_when_remote_guard_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env["FAKE_PATH_GUARD_FAILURE"] = "1"
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))
            ssh_log_exists = Path(env["FAKE_SSH_LOG"]).exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink guard", json.loads(result.stdout)["error"])
        self.assertFalse(ssh_log_exists)
        self.assertFalse(any("echo deploy_ok" in command for command in commands))

    def test_apply_uses_internal_development_ssh_and_restores_mount(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env["OPENUBMC_SSH_PASSWORD"] = ""
            env["OPENUBMC_TELNET_PASSWORD"] = ""
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--ssh-password",
                "super-secret-password",
                "--telnet-password",
                "telnet-secret-password",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))
            ssh = json.loads(Path(env["FAKE_SSH_LOG"]).read_text(encoding="utf-8"))
            sshpass = json.loads(
                Path(env["FAKE_SSHPASS_LOG"]).read_text(encoding="utf-8")
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["authorization_live_patch"])
        self.assertIn("--remove-created", applied["rollback_plan_command"])
        self.assertNotIn("--no-remount", applied["rollback_plan_command"])
        self.assertIn(
            "--host-key-policy insecure",
            applied["rollback_plan_command"],
        )
        self.assertIn("--ssh-password super-secret-password", applied["rollback_plan_command"])
        self.assertIn("--telnet-password telnet-secret-password", applied["rollback_plan_command"])
        self.assertIn(
            applied["local_sha256"],
            applied["rollback_plan_command"],
        )
        self.assertTrue(any("mount -o remount,rw /" in command for command in commands))
        self.assertTrue(any("mount -o remount,ro /" in command for command in commands))
        self.assertFalse(any("killall" in command for command in commands))
        port_index = ssh["argv"].index("-p")
        self.assertEqual(ssh["argv"][port_index + 1], "22")
        self.assertNotIn("super-secret-password", " ".join(ssh["argv"]))
        self.assertIn("StrictHostKeyChecking=no", ssh["argv"])
        self.assertIn("UserKnownHostsFile=/dev/null", ssh["argv"])
        self.assertFalse(ssh["sspass_present"])
        self.assertFalse(ssh["unrelated_passwords_present"])
        self.assertEqual(sshpass["argv"][0], "-e")
        self.assertTrue(sshpass["sspass_present"])
        self.assertFalse(sshpass["unrelated_passwords_present"])

    def test_generated_rollback_inherits_transport_selectors_and_no_remount(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            identity = directory / "live-patch-key"
            known_hosts = directory / "known-hosts"
            identity.write_text("test-key\n", encoding="utf-8")
            known_hosts.write_text("test-host-key\n", encoding="utf-8")
            env = self.make_fake_runtime(directory, local)
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--no-remount",
                "--ssh-user",
                "debug-user",
                "--ssh-identity",
                str(identity),
                "--known-hosts",
                str(known_hosts),
                "--host-key-policy",
                "accept-new",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))

        self.assertEqual(result.returncode, 0, result.stderr)
        rollback = json.loads(result.stdout)["rollback_plan_command"]
        self.assertIn("--remove-created", rollback)
        self.assertIn("--no-remount", rollback)
        self.assertIn("--ssh-user debug-user", rollback)
        self.assertIn(f"--ssh-identity {identity}", rollback)
        self.assertIn(f"--known-hosts {known_hosts}", rollback)
        self.assertIn("--host-key-policy accept-new", rollback)
        self.assertFalse(any("mount -o remount" in command for command in commands))

    def test_mount_state_is_restored_when_checksum_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env["FAKE_SHA_MISMATCH"] = "1"
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["root_mount_restored"])
        self.assertTrue(any("mount -o remount,ro /" in command for command in commands))

    def test_mount_restore_is_attempted_when_rw_remount_result_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env["FAKE_REMOUNT_RW_FAILURE"] = "1"
            result = self.run_cli(
                "deploy_live_file.py",
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("mount -o remount,ro /" in command for command in commands))

    def test_health_and_business_verification_failures_are_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            base = (
                "--ip",
                "192.0.2.10",
                "--local",
                str(local),
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit_manager.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--health-check",
                "--health-timeout",
                "0",
                "--health-interval",
                "0",
                "--json",
            )
            health_env = {**env, "FAKE_PREFLIGHT_RC": "3"}
            health_failure = self.run_cli("deploy_live_file.py", *base, env=health_env)

            verify_env = {**env, "FAKE_MDBCTL_RC": "4"}
            verification_failure = self.run_cli(
                "deploy_live_file.py",
                *base,
                "--verify-mdbctl",
                "lsobj UnitConfiguration",
                env=verify_env,
            )

        self.assertNotEqual(health_failure.returncode, 0)
        self.assertFalse(json.loads(health_failure.stdout)["ok"])
        self.assertNotEqual(verification_failure.returncode, 0)
        self.assertFalse(json.loads(verification_failure.stdout)["ok"])

    def test_lua_mapping_requires_explicit_app_instead_of_repo_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "arbitrary-repository-name"
            source = repo / "src" / "lualib" / "feature.lua"
            source.parent.mkdir(parents=True)
            source.write_text("return {}\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            commit = [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "base",
            ]
            subprocess.run(commit, check=True)
            source.write_text("return { changed = true }\n", encoding="utf-8")

            inferred = self.run_cli("infer_live_patch.py", "--cwd", str(repo), "--json")
            explicit = self.run_cli(
                "infer_live_patch.py",
                "--cwd",
                str(repo),
                "--app",
                "runtime-app",
                "--json",
            )

        self.assertEqual(inferred.returncode, 0, inferred.stderr)
        without_app = json.loads(inferred.stdout)["candidates"][0]
        self.assertFalse(without_app["supported"])
        self.assertIn("--app", without_app["reason"])
        with_app = json.loads(explicit.stdout)["candidates"][0]
        self.assertTrue(with_app["supported"])
        self.assertEqual(with_app["remote"], "/opt/bmc/apps/runtime-app/lualib/feature.lua")

    def test_current_patch_wrapper_surfaces_mapping_and_apply_gates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "component"
            source = repo / "src" / "lualib" / "feature.lua"
            source.parent.mkdir(parents=True)
            source.write_text("return {}\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            commit = [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "base",
            ]
            subprocess.run(commit, check=True)
            source.write_text("return { changed = true }\n", encoding="utf-8")

            missing_mapping = self.run_cli(
                "deploy_current_patch.py",
                "--cwd",
                str(repo),
                "--ip",
                "192.0.2.10",
                "--json",
            )
            planned = self.run_cli(
                "deploy_current_patch.py",
                "--cwd",
                str(repo),
                "--app",
                "runtime-app",
                "--ip",
                "192.0.2.10",
                "--json",
            )
            missing_intent = self.run_cli(
                "deploy_current_patch.py",
                "--cwd",
                str(repo),
                "--app",
                "runtime-app",
                "--ip",
                "192.0.2.10",
                "--apply",
                "--restart-scope",
                "none",
                "--json",
            )
            missing_authorization = self.run_cli(
                "deploy_current_patch.py",
                "--cwd",
                str(repo),
                "--app",
                "runtime-app",
                "--ip",
                "192.0.2.10",
                "--apply",
                "--intent",
                "live_patch",
                "--restart-scope",
                "none",
                "--json",
            )

        self.assertEqual(missing_mapping.returncode, 2)
        self.assertIn("--app", json.loads(missing_mapping.stdout)["unsupported_candidates"][0]["reason"])
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertTrue(json.loads(planned.stdout)["dry_run"])
        self.assertEqual(missing_intent.returncode, 2)
        self.assertIn("--intent live_patch", json.loads(missing_intent.stdout)["error"])
        self.assertEqual(missing_authorization.returncode, 2)
        self.assertIn(
            "authorization.live_patch",
            json.loads(missing_authorization.stdout)["error"],
        )

    def test_current_patch_does_not_hide_an_unresolved_runtime_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "component"
            lua = repo / "src" / "lualib" / "feature.lua"
            sr = repo / "schema.sr"
            lua.parent.mkdir(parents=True)
            lua.write_text("return {}\n", encoding="utf-8")
            sr.write_text("schema\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            commit = [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "base",
            ]
            subprocess.run(commit, check=True)
            lua.write_text("return { changed = true }\n", encoding="utf-8")
            sr.write_text("schema changed\n", encoding="utf-8")

            ambiguous = self.run_cli(
                "deploy_current_patch.py",
                "--cwd",
                str(repo),
                "--ip",
                "192.0.2.10",
                "--json",
            )
            selected = self.run_cli(
                "deploy_current_patch.py",
                "--cwd",
                str(repo),
                "--ip",
                "192.0.2.10",
                "--select",
                "schema.sr",
                "--json",
            )

        self.assertEqual(ambiguous.returncode, 2)
        self.assertIn("--app", json.loads(ambiguous.stdout)["unsupported_candidates"][0]["reason"])
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(json.loads(selected.stdout)["remote"], "/opt/bmc/sr/schema.sr")

    def test_rollback_is_plan_only_and_uses_the_same_mutation_gates(self) -> None:
        common = (
            "--ip",
            "192.0.2.10",
            "--backup",
            "/tmp/unit.lua.bak.1",
            "--remote",
            "/opt/bmc/apps/demo/lualib/unit.lua",
            "--json",
        )
        planned = self.run_cli("rollback_live_file.py", *common)
        missing_intent = self.run_cli("rollback_live_file.py", *common, "--apply")
        missing_scope = self.run_cli(
            "rollback_live_file.py",
            *common,
            "--apply",
            "--intent",
            "live_patch",
            "--authorize-live-patch",
        )
        missing_authorization = self.run_cli(
            "rollback_live_file.py",
            *common,
            "--apply",
            "--intent",
            "live_patch",
            "--restart-scope",
            "none",
        )

        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertTrue(json.loads(planned.stdout)["dry_run"])
        self.assertEqual(missing_intent.returncode, 2)
        self.assertIn("--intent live_patch", json.loads(missing_intent.stdout)["error"])
        self.assertEqual(missing_scope.returncode, 2)
        self.assertIn("--restart-scope", json.loads(missing_scope.stdout)["error"])
        self.assertEqual(missing_authorization.returncode, 2)
        self.assertIn("authorization.live_patch", json.loads(missing_authorization.stdout)["error"])

    def test_remove_created_rollback_is_plan_only_and_checksum_guarded(self) -> None:
        digest = "d" * 64
        common = (
            "--ip",
            "192.0.2.10",
            "--remove-created",
            "--remote",
            "/tmp/openubmc-live-patch-smoke",
            "--json",
        )
        missing_digest = self.run_cli("rollback_live_file.py", *common)
        planned = self.run_cli(
            "rollback_live_file.py",
            *common,
            "--expected-current-sha256",
            digest,
        )
        conflicting = self.run_cli(
            "rollback_live_file.py",
            *common,
            "--expected-current-sha256",
            digest,
            "--backup",
            "/tmp/unit.lua.bak.1",
        )

        self.assertEqual(missing_digest.returncode, 2)
        self.assertIn(
            "expected-current-sha256",
            json.loads(missing_digest.stdout)["error"],
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        payload = json.loads(planned.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["remove_created"])
        self.assertEqual(payload["expected_current_sha256"], digest)
        self.assertEqual(conflicting.returncode, 2)

    def test_nonstandard_rollback_target_requires_force_path(self) -> None:
        common = (
            "--ip",
            "192.0.2.10",
            "--backup",
            "/tmp/unit.lua.bak.1",
            "--remote",
            "/srv/openubmc/unit.lua",
            "--json",
        )
        refused = self.run_cli("rollback_live_file.py", *common)
        planned = self.run_cli("rollback_live_file.py", *common, "--force-path")

        self.assertEqual(refused.returncode, 2)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertTrue(json.loads(planned.stdout)["dry_run"])

    def test_rollback_apply_restores_mount_without_implicit_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            result = self.run_cli(
                "rollback_live_file.py",
                "--ip",
                "192.0.2.10",
                "--backup",
                "/tmp/unit.lua.bak.1",
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertTrue(any("mount -o remount,rw /" in command for command in commands))
        self.assertTrue(any("mount -o remount,ro /" in command for command in commands))
        self.assertFalse(any("killall" in command for command in commands))

    def test_rollback_symlink_guard_blocks_external_target_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            allowed = root / "allowed"
            allowed.mkdir()
            backup = root / "backup.lua"
            backup.write_text("restored", encoding="utf-8")
            outside = root / "outside.lua"
            outside.write_text("outside-original", encoding="utf-8")
            target = allowed / "unit.lua"
            target.symlink_to(outside)
            command = remote_path_guard_command(
                files=[
                    (str(target), str(allowed), False),
                    (str(backup), str(root), True),
                ],
                directories=[],
            ) + " && " + atomic_restore_command(
                str(backup), str(target), "644", "rollback-symlink-test"
            )

            completed = subprocess.run(
                ["sh", "-c", command], capture_output=True, text=True, check=False
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(target.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside-original")

    def test_rollback_stops_before_copy_when_remote_guard_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env["FAKE_PATH_GUARD_FAILURE"] = "1"
            result = self.run_cli(
                "rollback_live_file.py",
                "--ip",
                "192.0.2.10",
                "--backup",
                "/tmp/unit.lua.bak.1",
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )
            commands = self.read_commands(Path(env["FAKE_REMOTE_LOG"]))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink guard", json.loads(result.stdout)["error"])
        self.assertFalse(any("echo restore_ok" in command for command in commands))

    def test_rollback_checksum_mismatch_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            local = self.write_local_file(directory)
            env = self.make_fake_runtime(directory, local)
            env["FAKE_ROLLBACK_SHA_MISMATCH"] = "1"
            result = self.run_cli(
                "rollback_live_file.py",
                "--ip",
                "192.0.2.10",
                "--backup",
                "/tmp/unit.lua.bak.1",
                "--remote",
                "/opt/bmc/apps/demo/lualib/unit.lua",
                "--apply",
                "--intent",
                "live_patch",
                "--authorize-live-patch",
                "--restart-scope",
                "none",
                "--json",
                env=env,
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("checksum", payload["error"].lower())
        self.assertTrue(payload["root_mount_restored"])

    def test_skill_contains_no_author_machine_or_insecure_ssh_fallbacks(self) -> None:
        forbidden = (
            "/root/" + ".agents",
            "10.121." + "177.159",
            "/home/workspace/" + "source",
            '"-p",\n        password',
        )
        checked = [
            SKILL_ROOT / "SKILL.md",
            *sorted((SKILL_ROOT / "scripts").glob("*.py")),
            *sorted((SKILL_ROOT / "references").glob("*.md")),
        ]
        for path in checked:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{path} contains forbidden portability fallback")


if __name__ == "__main__":
    unittest.main()
