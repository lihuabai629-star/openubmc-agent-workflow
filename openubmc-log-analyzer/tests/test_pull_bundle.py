from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from urllib import error as urllib_error
from unittest import mock


SCRIPT_DIR = pathlib.Path(os.environ['OPENUBMC_TEST_PLUGIN_ROOT']) / 'skills/openubmc-log-analyzer/scripts' if os.environ.get('OPENUBMC_TEST_PLUGIN_ROOT') else pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pull_bundle  # type: ignore  # noqa: E402
import target_runtime_adapter  # type: ignore  # noqa: E402


class ResolveIpTests(unittest.TestCase):
    def test_uses_interactive_input_when_ip_missing(self) -> None:
        with mock.patch("builtins.input", return_value="10.121.177.77") as input_mock:
            self.assertEqual(pull_bundle.resolve_ip("", json_mode=False), "10.121.177.77")
        input_mock.assert_called_once()

    def test_rejects_interactive_prompt_in_json_mode(self) -> None:
        with self.assertRaisesRegex(pull_bundle.BundlePullError, "--ip"):
            pull_bundle.resolve_ip("", json_mode=True)


class MainJsonValidationTests(unittest.TestCase):
    def test_json_mode_reports_missing_ip_as_json_payload(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = pull_bundle.main(["--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "missing_ip")

    @mock.patch("pull_bundle.analyze_bundle")
    @mock.patch("pull_bundle.extract_archive")
    @mock.patch("target_runtime_adapter.open_log_bundle_runtime_lease")
    def test_json_mode_includes_analysis_when_problem_provided(
        self,
        open_runtime_lease_mock: mock.Mock,
        extract_archive_mock: mock.Mock,
        analyze_bundle_mock: mock.Mock,
    ) -> None:
        lease = open_runtime_lease_mock.return_value.__enter__.return_value
        lease.collect.return_value = pull_bundle.BundleStageResult(
            remote_bundle_path="/tmp/bundle.tar.gz",
            local_bundle_path=pathlib.Path("/tmp/bundle.tar.gz"),
            generation_ran=True,
            transport="redfish",
        )
        extract_archive_mock.return_value = pull_bundle.ExtractionResult(
            extract_dir=pathlib.Path("/tmp/extract"),
            bundle_root=pathlib.Path("/tmp/extract"),
        )
        analyze_bundle_mock.return_value = {"problem": "login failed", "selected_logs": [{"name": "security.log"}]}

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = pull_bundle.main(["--json", "--ip", "10.0.0.1", "--problem", "login failed"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["analysis"]["problem"], "login failed")
        self.assertEqual(payload["result"]["analysis"]["selected_logs"][0]["name"], "security.log")

    def test_default_passwords_are_empty(self) -> None:
        args = pull_bundle.parse_args([])
        self.assertEqual(args.redfish_password, "")
        self.assertEqual(args.ssh_password, "")

    def test_ssh_command_uses_a_local_input_reference_instead_of_password_argv(self) -> None:
        secret = "fixture-password-never-in-argv"
        with mock.patch("pull_bundle.shutil.which", return_value="/usr/bin/sshpass"):
            command = pull_bundle.build_ssh_command(
                ip="192.0.2.10",
                user="root",
                password=secret,
                port=22,
                identity_file="",
                remote_command="true",
            )

        self.assertEqual(command[:3], ["sshpass", "-d", "0"])
        self.assertNotIn(secret, command)

    @mock.patch("pull_bundle.subprocess.run")
    def test_checked_ssh_streams_password_without_exporting_it(
        self,
        subprocess_run_mock: mock.Mock,
    ) -> None:
        secret = "fixture-password-never-in-env"
        subprocess_run_mock.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout="",
        )

        pull_bundle.run_checked(
            ["sshpass", "-d", "0", "ssh", "root@192.0.2.10", "true"],
            timeout=3,
            error_code="ssh_failed",
            failure_message="SSH failed",
            secret_input=secret,
        )

        call = subprocess_run_mock.call_args.kwargs
        self.assertEqual(call.get("input"), secret + "\n")
        environment = call.get("env") or {}
        self.assertFalse(any(secret == value for value in environment.values()))
        self.assertNotIn("SSHPASS", environment)

    def test_redfish_proxy_mode_can_be_forced_disabled(self) -> None:
        args = pull_bundle.parse_args(["--redfish-proxy", "disable"])
        self.assertEqual(args.redfish_proxy, "disable")


class RedfishHelperTests(unittest.TestCase):
    def test_should_bypass_proxy_for_private_ip_url(self) -> None:
        self.assertTrue(pull_bundle.should_bypass_proxy("https://10.121.177.77/redfish/v1/"))
        with mock.patch("pull_bundle.socket.getaddrinfo") as getaddrinfo_mock:
            getaddrinfo_mock.return_value = [(0, 0, 0, "", ("93.184.216.34", 443))]
            self.assertFalse(pull_bundle.should_bypass_proxy("https://example.com/redfish/v1/"))

    def test_should_bypass_proxy_for_private_hostname(self) -> None:
        with mock.patch("pull_bundle.socket.getaddrinfo") as getaddrinfo_mock:
            getaddrinfo_mock.return_value = [(0, 0, 0, "", ("10.121.177.77", 443))]
            self.assertTrue(pull_bundle.should_bypass_proxy("https://bmc-host/redfish/v1/"))

    def test_should_respect_proxy_mode_override(self) -> None:
        self.assertTrue(pull_bundle.should_bypass_proxy("https://example.com/redfish/v1/", proxy_mode="disable"))
        self.assertFalse(pull_bundle.should_bypass_proxy("https://10.121.177.77/redfish/v1/", proxy_mode="inherit"))

    def test_resolve_secret_prompts_when_missing_outside_json_mode(self) -> None:
        with mock.patch("pull_bundle.getpass.getpass", return_value="secret") as getpass_mock:
            self.assertEqual(
                pull_bundle.resolve_secret("", "", "Redfish password", json_mode=False),
                "secret",
            )
        getpass_mock.assert_called_once()

    def test_resolve_secret_rejects_missing_secret_in_json_mode(self) -> None:
        with self.assertRaisesRegex(pull_bundle.BundlePullError, "Redfish password"):
            pull_bundle.resolve_secret("", "", "Redfish password", json_mode=True)

    @mock.patch("pull_bundle.urllib_request.urlopen")
    @mock.patch("pull_bundle.urllib_request.build_opener")
    def test_http_request_uses_proxyless_opener_for_private_ip(
        self,
        build_opener_mock: mock.Mock,
        urlopen_mock: mock.Mock,
    ) -> None:
        response_mock = mock.MagicMock()
        response_mock.__enter__.return_value = SimpleNamespace(
            status=200,
            headers={"Content-Type": "application/json"},
            read=lambda: b"{}",
        )
        opener_mock = mock.Mock()
        opener_mock.open.return_value = response_mock
        build_opener_mock.return_value = opener_mock

        response = pull_bundle.http_request(
            method="GET",
            url="https://10.121.177.77/redfish/v1/",
            timeout=30,
            error_code="redfish_request_failed",
            failure_message="Redfish request failed",
        )

        self.assertEqual(response.status, 200)
        build_opener_mock.assert_called_once()
        opener_mock.open.assert_called_once()
        urlopen_mock.assert_not_called()

    @mock.patch("pull_bundle.curl_http_request")
    @mock.patch("pull_bundle.open_urllib_request")
    def test_http_request_falls_back_to_curl_on_retryable_transport_error(
        self,
        open_urllib_request_mock: mock.Mock,
        curl_http_request_mock: mock.Mock,
    ) -> None:
        open_urllib_request_mock.side_effect = urllib_error.URLError(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        )
        curl_http_request_mock.return_value = pull_bundle.HttpResponse(status=200, headers={"Content-Type": "application/json"}, body=b"{}")
        response = pull_bundle.http_request(
            method="GET",
            url="https://10.121.177.77/redfish/v1/",
            timeout=30,
            error_code="redfish_request_failed",
            failure_message="Redfish request failed",
        )
        self.assertEqual(response.status, 200)
        curl_http_request_mock.assert_called_once()

    @mock.patch("pull_bundle.subprocess.run")
    def test_curl_http_request_disables_proxy_for_private_ip(
        self,
        subprocess_run_mock: mock.Mock,
    ) -> None:
        completed = SimpleNamespace(returncode=0, stderr="", stdout="")
        subprocess_run_mock.return_value = completed
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            header_path = tmp_path / "headers.txt"
            body_path = tmp_path / "body.bin"
            header_path.write_text("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n", encoding="utf-8")
            body_path.write_bytes(b"{}")
            with mock.patch("pull_bundle.tempfile.TemporaryDirectory") as tempdir_mock:
                tempdir_mock.return_value.__enter__.return_value = str(tmp_path)
                tempdir_mock.return_value.__exit__.return_value = False
                response = pull_bundle.curl_http_request(
                    method="GET",
                    url="https://10.121.177.77/redfish/v1/",
                    timeout=30,
                    error_code="redfish_request_failed",
                    failure_message="Redfish request failed",
                )

        command = subprocess_run_mock.call_args.args[0]
        self.assertIn("--noproxy", command)
        self.assertEqual(response.status, 200)

    @mock.patch("pull_bundle.subprocess.run")
    def test_curl_http_request_rejects_http_error_status(
        self,
        subprocess_run_mock: mock.Mock,
    ) -> None:
        completed = SimpleNamespace(returncode=0, stderr="", stdout="")
        subprocess_run_mock.return_value = completed
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            header_path = tmp_path / "headers.txt"
            body_path = tmp_path / "body.bin"
            header_path.write_text("HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n\r\n", encoding="utf-8")
            body_path.write_bytes(b'{"error":"bad request"}')
            with mock.patch("pull_bundle.tempfile.TemporaryDirectory") as tempdir_mock:
                tempdir_mock.return_value.__enter__.return_value = str(tmp_path)
                tempdir_mock.return_value.__exit__.return_value = False
                with self.assertRaisesRegex(pull_bundle.BundlePullError, "HTTP 400"):
                    pull_bundle.curl_http_request(
                        method="GET",
                        url="https://10.121.177.77/redfish/v1/",
                        timeout=30,
                        error_code="redfish_request_failed",
                        failure_message="Redfish request failed",
                    )

    @mock.patch("pull_bundle.subprocess.run")
    def test_curl_http_request_keeps_json_body_out_of_process_args(
        self,
        subprocess_run_mock: mock.Mock,
    ) -> None:
        completed = SimpleNamespace(returncode=0, stderr="", stdout="")
        subprocess_run_mock.return_value = completed
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            header_path = tmp_path / "headers.txt"
            body_path = tmp_path / "body.bin"
            header_path.write_text("HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n\r\n", encoding="utf-8")
            body_path.write_bytes(b"{}")
            with mock.patch("pull_bundle.tempfile.TemporaryDirectory") as tempdir_mock:
                tempdir_mock.return_value.__enter__.return_value = str(tmp_path)
                tempdir_mock.return_value.__exit__.return_value = False
                response = pull_bundle.curl_http_request(
                    method="POST",
                    url="https://10.121.177.77/redfish/v1/SessionService/Sessions",
                    json_body={"UserName": "Administrator", "Password": "redfish-secret"},
                    timeout=30,
                    error_code="redfish_auth_failed",
                    failure_message="Failed to create Redfish session",
                )

        command = subprocess_run_mock.call_args.args[0]
        self.assertNotIn("redfish-secret", " ".join(command))
        self.assertIn("--data-binary", command)
        self.assertEqual(response.status, 201)

    @mock.patch("pull_bundle.time.sleep")
    @mock.patch("pull_bundle.http_request")
    def test_redfish_create_session_retries_transient_transport_errors(
        self,
        http_request_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        http_request_mock.side_effect = [
            pull_bundle.BundlePullError(
                "redfish_auth_failed",
                "Failed to create Redfish session: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
            ),
            pull_bundle.HttpResponse(
                status=201,
                headers={
                    "X-Auth-Token": "token",
                    "Location": "/redfish/v1/SessionService/Sessions/1",
                },
                body=b"{}",
            ),
        ]
        session = pull_bundle.redfish_create_session(
            ip="10.121.177.77",
            user="Administrator",
            password="redfish-secret",
            port=443,
            timeout=30,
        )
        self.assertEqual(session.token, "token")
        self.assertEqual(http_request_mock.call_count, 2)
        sleep_mock.assert_called()

    @mock.patch("pull_bundle.time.sleep")
    @mock.patch("pull_bundle.http_request")
    def test_redfish_create_session_retries_transient_gateway_http_errors(
        self,
        http_request_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        for status in (502, 503, 504):
            with self.subTest(status=status):
                http_request_mock.reset_mock()
                sleep_mock.reset_mock()
                http_request_mock.side_effect = [
                    pull_bundle.BundlePullError(
                        "redfish_auth_failed",
                        f"Failed to create Redfish session: HTTP {status}: <html>bad gateway</html>",
                    ),
                    pull_bundle.HttpResponse(
                        status=201,
                        headers={
                            "X-Auth-Token": "token",
                            "Location": "/redfish/v1/SessionService/Sessions/1",
                        },
                        body=b"{}",
                    ),
                ]
                session = pull_bundle.redfish_create_session(
                    ip="10.121.177.77",
                    user="Administrator",
                    password="redfish-secret",
                    port=443,
                    timeout=30,
                )
                self.assertEqual(session.token, "token")
                self.assertEqual(http_request_mock.call_count, 2)
                sleep_mock.assert_called()

    def test_select_redfish_action_target_uses_oem_manager_actions(self) -> None:
        manager_payload = {
            "Actions": {
                "Oem": {
                    "openUBMC": {
                        "#Manager.Dump": {"target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump"},
                        "#Manager.QuickDump": {"target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.QuickDump"},
                        "#Manager.GeneralDownload": {
                            "target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.GeneralDownload"
                        },
                    }
                }
            }
        }
        self.assertEqual(
            pull_bundle.select_redfish_action_target(manager_payload, "dump"),
            "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump",
        )
        self.assertEqual(
            pull_bundle.select_redfish_action_target(manager_payload, "quickdump"),
            "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.QuickDump",
        )
        self.assertEqual(
            pull_bundle.select_redfish_general_download_target(manager_payload),
            "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.GeneralDownload",
        )

    def test_extract_task_path_prefers_odata_id_and_falls_back_to_location(self) -> None:
        response = pull_bundle.HttpResponse(
            status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/1/Monitor"},
            body=b'{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}',
        )
        self.assertEqual(
            pull_bundle.extract_redfish_task_path(response),
            "/redfish/v1/TaskService/Tasks/1",
        )

        response = pull_bundle.HttpResponse(
            status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/9/Monitor"},
            body=b"{}",
        )
        self.assertEqual(
            pull_bundle.extract_redfish_task_path(response),
            "/redfish/v1/TaskService/Tasks/9",
        )

    @mock.patch("pull_bundle.time.sleep")
    @mock.patch("pull_bundle.redfish_request_json")
    def test_redfish_wait_for_task_retries_transient_poll_errors(
        self,
        request_json_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        session = pull_bundle.RedfishSession(
            base_url="https://10.121.177.77",
            token="token",
            session_path="/redfish/v1/SessionService/Sessions/1",
        )
        request_json_mock.side_effect = [
            pull_bundle.BundlePullError(
                "redfish_task_poll_failed",
                "Failed to poll Redfish task: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
            ),
            {"TaskState": "Completed", "Messages": []},
        ]
        task = pull_bundle.redfish_wait_for_task(session, "/redfish/v1/TaskService/Tasks/1", timeout=30, poll_interval=1)
        self.assertEqual(task["TaskState"], "Completed")
        self.assertEqual(request_json_mock.call_count, 2)
        sleep_mock.assert_called()

    @mock.patch("pull_bundle.time.sleep")
    @mock.patch("pull_bundle.redfish_request_json")
    def test_redfish_wait_for_task_retries_timeout_poll_errors(
        self,
        request_json_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        session = pull_bundle.RedfishSession(
            base_url="https://10.121.177.77",
            token="token",
            session_path="/redfish/v1/SessionService/Sessions/1",
        )
        request_json_mock.side_effect = [
            pull_bundle.BundlePullError(
                "redfish_task_poll_failed",
                "Failed to poll Redfish task: timed out",
            ),
            {"TaskState": "Completed", "Messages": []},
        ]
        task = pull_bundle.redfish_wait_for_task(session, "/redfish/v1/TaskService/Tasks/1", timeout=30, poll_interval=1)
        self.assertEqual(task["TaskState"], "Completed")
        self.assertEqual(request_json_mock.call_count, 2)
        sleep_mock.assert_called()

    @mock.patch("pull_bundle.time.sleep")
    @mock.patch("pull_bundle.redfish_request_json")
    def test_redfish_wait_for_task_retries_ssl_syscall_poll_errors(
        self,
        request_json_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        session = pull_bundle.RedfishSession(
            base_url="https://10.121.177.77",
            token="token",
            session_path="/redfish/v1/SessionService/Sessions/1",
        )
        request_json_mock.side_effect = [
            pull_bundle.BundlePullError(
                "redfish_task_poll_failed",
                "Failed to poll Redfish task: curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to 10.121.177.77:443",
            ),
            {"TaskState": "Completed", "Messages": []},
        ]
        task = pull_bundle.redfish_wait_for_task(session, "/redfish/v1/TaskService/Tasks/1", timeout=30, poll_interval=1)
        self.assertEqual(task["TaskState"], "Completed")
        self.assertEqual(request_json_mock.call_count, 2)
        sleep_mock.assert_called()


class RedfishFlowTests(unittest.TestCase):
    @mock.patch("pull_bundle.redfish_delete_session")
    @mock.patch("pull_bundle.redfish_download_bundle")
    @mock.patch("pull_bundle.redfish_wait_for_task")
    @mock.patch("pull_bundle.redfish_request")
    @mock.patch("pull_bundle.redfish_request_json")
    @mock.patch("pull_bundle.redfish_create_session")
    def test_run_redfish_bundle_flow_uses_location_header_when_action_response_has_no_odata_id(
        self,
        create_session_mock: mock.Mock,
        request_json_mock: mock.Mock,
        redfish_request_mock: mock.Mock,
        wait_for_task_mock: mock.Mock,
        download_bundle_mock: mock.Mock,
        delete_session_mock: mock.Mock,
    ) -> None:
        manager_payload = {
            "Actions": {
                "Oem": {
                    "openUBMC": {
                        "#Manager.Dump": {"target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump"},
                        "#Manager.GeneralDownload": {
                            "target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.GeneralDownload"
                        },
                    }
                }
            }
        }
        create_session_mock.return_value = pull_bundle.RedfishSession(
            base_url="https://10.121.177.77",
            token="token",
            session_path="/redfish/v1/SessionService/Sessions/1",
        )
        request_json_mock.return_value = manager_payload
        redfish_request_mock.return_value = pull_bundle.HttpResponse(
            status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/1/Monitor"},
            body=b"{}",
        )
        download_bundle_mock.return_value = pathlib.Path("/tmp/bundle.tar.gz")
        args = SimpleNamespace(
            redfish_port=443,
            redfish_user="Administrator",
            redfish_password="redfish-secret",
            redfish_user_env="",
            redfish_password_env="",
            redfish_action="dump",
            remote_path="",
            download_timeout=60,
            redfish_timeout=60,
            redfish_task_timeout=120,
            redfish_poll_interval=1,
        )

        result = pull_bundle.run_redfish_bundle_flow(args, ip="10.121.177.77", local_dir=pathlib.Path("/tmp/out"))

        self.assertTrue(result.generation_ran)
        wait_for_task_mock.assert_called_once_with(
            create_session_mock.return_value,
            "/redfish/v1/TaskService/Tasks/1",
            timeout=120,
            poll_interval=1,
        )
        delete_session_mock.assert_called_once_with(create_session_mock.return_value, timeout=60)

    @mock.patch("pull_bundle.redfish_delete_session")
    @mock.patch("pull_bundle.redfish_download_bundle")
    @mock.patch("pull_bundle.redfish_wait_for_task")
    @mock.patch("pull_bundle.redfish_request")
    @mock.patch("pull_bundle.redfish_request_json")
    @mock.patch("pull_bundle.redfish_create_session")
    def test_run_redfish_bundle_flow_triggers_dump_then_downloads(
        self,
        create_session_mock: mock.Mock,
        request_json_mock: mock.Mock,
        redfish_request_mock: mock.Mock,
        wait_for_task_mock: mock.Mock,
        download_bundle_mock: mock.Mock,
        delete_session_mock: mock.Mock,
    ) -> None:
        manager_payload = {
            "Actions": {
                "Oem": {
                    "openUBMC": {
                        "#Manager.Dump": {"target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump"},
                        "#Manager.GeneralDownload": {
                            "target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.GeneralDownload"
                        },
                    }
                }
            }
        }
        create_session_mock.return_value = pull_bundle.RedfishSession(
            base_url="https://10.121.177.77",
            token="token",
            session_path="/redfish/v1/SessionService/Sessions/1",
        )
        request_json_mock.return_value = manager_payload
        redfish_request_mock.return_value = pull_bundle.HttpResponse(
            status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/1/Monitor"},
            body=b'{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}',
        )
        download_bundle_mock.return_value = pathlib.Path("/tmp/bundle.tar.gz")
        args = SimpleNamespace(
            redfish_port=443,
            redfish_user="Administrator",
            redfish_password="redfish-secret",
            redfish_user_env="",
            redfish_password_env="",
            redfish_action="dump",
            remote_path="",
            download_timeout=60,
            redfish_timeout=60,
            redfish_task_timeout=120,
            redfish_poll_interval=1,
        )

        result = pull_bundle.run_redfish_bundle_flow(args, ip="10.121.177.77", local_dir=pathlib.Path("/tmp/out"))

        self.assertTrue(result.generation_ran)
        self.assertTrue(result.remote_bundle_path.endswith(".tar.gz"))
        self.assertEqual(result.local_bundle_path, pathlib.Path("/tmp/bundle.tar.gz"))
        self.assertEqual(redfish_request_mock.call_args.kwargs["path"], "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump")
        wait_for_task_mock.assert_called_once_with(
            create_session_mock.return_value,
            "/redfish/v1/TaskService/Tasks/1",
            timeout=120,
            poll_interval=1,
        )
        download_bundle_mock.assert_called_once()
        delete_session_mock.assert_called_once_with(create_session_mock.return_value, timeout=60)

    @mock.patch("pull_bundle.redfish_delete_session")
    @mock.patch("pull_bundle.redfish_download_bundle")
    @mock.patch("pull_bundle.redfish_wait_for_task")
    @mock.patch("pull_bundle.redfish_request")
    @mock.patch("pull_bundle.redfish_request_json")
    @mock.patch("pull_bundle.redfish_create_session")
    def test_run_redfish_bundle_flow_falls_back_to_dump_when_quickdump_disabled(
        self,
        create_session_mock: mock.Mock,
        request_json_mock: mock.Mock,
        redfish_request_mock: mock.Mock,
        wait_for_task_mock: mock.Mock,
        download_bundle_mock: mock.Mock,
        delete_session_mock: mock.Mock,
    ) -> None:
        manager_payload = {
            "Actions": {
                "Oem": {
                    "openUBMC": {
                        "#Manager.Dump": {"target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump"},
                        "#Manager.QuickDump": {"target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.QuickDump"},
                        "#Manager.GeneralDownload": {
                            "target": "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.GeneralDownload"
                        },
                    }
                }
            }
        }
        create_session_mock.return_value = pull_bundle.RedfishSession(
            base_url="https://10.121.177.77",
            token="token",
            session_path="/redfish/v1/SessionService/Sessions/1",
        )
        request_json_mock.return_value = manager_payload
        redfish_request_mock.side_effect = [
            pull_bundle.BundlePullError(
                "redfish_collect_failed",
                "Failed to trigger Redfish bundle collection: HTTP 400: {\"error\":{\"@Message.ExtendedInfo\":[{\"MessageId\":\"openUBMC.1.0.FeatureDisabledAndNotSupportOperation\"}]}}",
            ),
            pull_bundle.HttpResponse(
                status=202,
                headers={"Location": "/redfish/v1/TaskService/Tasks/1/Monitor"},
                body=b'{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}',
            ),
        ]
        download_bundle_mock.return_value = pathlib.Path("/tmp/bundle.tar.gz")
        args = SimpleNamespace(
            redfish_port=443,
            redfish_user="Administrator",
            redfish_password="redfish-secret",
            redfish_user_env="",
            redfish_password_env="",
            redfish_action="quickdump",
            remote_path="",
            download_timeout=60,
            redfish_timeout=60,
            redfish_task_timeout=120,
            redfish_poll_interval=1,
        )

        result = pull_bundle.run_redfish_bundle_flow(args, ip="10.121.177.77", local_dir=pathlib.Path("/tmp/out"))

        self.assertTrue(result.generation_ran)
        self.assertTrue(result.remote_bundle_path.endswith(".tar.gz"))
        self.assertEqual(redfish_request_mock.call_count, 2)
        self.assertEqual(
            redfish_request_mock.call_args_list[0].kwargs["path"],
            "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.QuickDump",
        )
        self.assertEqual(
            redfish_request_mock.call_args_list[1].kwargs["path"],
            "/redfish/v1/Managers/1/Actions/Oem/openUBMC/Manager.Dump",
        )
        wait_for_task_mock.assert_called_once_with(
            create_session_mock.return_value,
            "/redfish/v1/TaskService/Tasks/1",
            timeout=120,
            poll_interval=1,
        )
        download_bundle_mock.assert_called_once()
        delete_session_mock.assert_called_once_with(create_session_mock.return_value, timeout=60)


class AnalysisTests(unittest.TestCase):
    def test_generic_chinese_runtime_check_uses_bounded_core_logs(self) -> None:
        reference = pull_bundle.load_reference_data()

        selected = pull_bundle.select_logs_for_problem(
            "BMC运行状态与近期错误检查",
            reference,
            max_files=8,
        )

        self.assertEqual(
            [item["name"] for item in selected[:3]],
            ["app.log", "framework.log", "journalctl.log"],
        )

    def test_select_logs_for_problem_prefers_rule_matches(self) -> None:
        reference = {
            "files": [
                {"name": "security.log", "paths": ["dump_info/LogDump/security.log"], "purpose": "auth", "keywords": ["auth", "login"]},
                {"name": "operation.log", "paths": ["dump_info/LogDump/operation.log"], "purpose": "audit", "keywords": ["operation"]},
                {"name": "app.log", "paths": ["dump_info/LogDump/app.log"], "purpose": "generic", "keywords": ["error"]},
            ],
            "rules": [
                {"match_keywords": ["login", "auth", "登录失败"], "include": ["security.log", "operation.log"]},
            ],
        }
        selected = pull_bundle.select_logs_for_problem("BMC login auth failure", reference, max_files=5)
        self.assertEqual([item["name"] for item in selected[:2]], ["security.log", "operation.log"])

    def test_select_logs_for_problem_ignores_generic_file_keywords(self) -> None:
        reference = {
            "files": [
                {"name": "security.log", "paths": ["dump_info/LogDump/security.log"], "purpose": "auth", "keywords": ["login"]},
                {"name": "journalctl.log", "paths": ["dump_info/RTOSDump/sysinfo/journalctl.log"], "purpose": "systemd", "keywords": ["systemd", "失败"]},
            ],
            "rules": [
                {"match_keywords": ["登录失败"], "include": ["security.log"]},
            ],
        }
        selected = pull_bundle.select_logs_for_problem("BMC 登录失败", reference, max_files=5)
        self.assertEqual([item["name"] for item in selected], ["security.log"])

    def test_analyze_bundle_collects_matching_evidence(self) -> None:
        reference = {
            "files": [
                {
                    "name": "security.log",
                    "paths": ["dump_info/LogDump/security.log"],
                    "purpose": "auth events",
                    "keywords": ["auth", "login", "failure"],
                }
            ],
            "rules": [
                {"match_keywords": ["login", "auth"], "include": ["security.log"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            log_file = root / "dump_info" / "LogDump" / "security.log"
            log_file.parent.mkdir(parents=True)
            log_file.write_text("2026-04-07 auth failed for Administrator\n", encoding="utf-8")
            analysis = pull_bundle.analyze_bundle(root, "login auth failed", reference_data=reference, max_files=5, max_lines=3)
        self.assertEqual(analysis["selected_logs"][0]["name"], "security.log")
        self.assertIn("auth failed", analysis["selected_logs"][0]["evidence_lines"][0]["line"])

    def test_collect_evidence_lines_uses_generic_failure_terms_alongside_problem_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "security.log"
            path.write_text("2026-04-07 auth failed for Administrator\n", encoding="utf-8")
            evidence = pull_bundle.collect_evidence_lines(path, match_terms=["登录失败"], max_lines=3)
        self.assertIn("auth failed", evidence[0]["line"])

    def test_collect_evidence_lines_prefers_problem_keyword_matches_over_generic_failed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "operation.log"
            path.write_text(
                "\n".join(
                    [
                        "2026-04-07 firmware upgrade failed",
                        "2026-04-07 User Administrator login failed",
                        "2026-04-07 another generic failure",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            evidence = pull_bundle.collect_evidence_lines(path, match_terms=["login"], max_lines=3)
        self.assertEqual(evidence[0]["line"], "2026-04-07 User Administrator login failed")

    def test_analyze_bundle_uses_file_keywords_for_rule_matched_logs(self) -> None:
        reference = {
            "files": [
                {
                    "name": "operation.log",
                    "paths": ["dump_info/LogDump/operation.log"],
                    "purpose": "user operations audit",
                    "keywords": ["login"],
                }
            ],
            "rules": [
                {"match_keywords": ["登录失败"], "include": ["operation.log"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            log_file = root / "dump_info" / "LogDump" / "operation.log"
            log_file.parent.mkdir(parents=True)
            log_file.write_text(
                "2026-04-07 firmware upgrade failed\n"
                "2026-04-07 User Administrator login failed\n",
                encoding="utf-8",
            )
            analysis = pull_bundle.analyze_bundle(root, "BMC 登录失败", reference_data=reference, max_files=5, max_lines=3)
        evidence_lines = analysis["selected_logs"][0]["evidence_lines"]
        self.assertEqual(evidence_lines[0]["line"], "2026-04-07 User Administrator login failed")

    def test_analyze_bundle_prefers_exact_login_failed_phrase_over_newer_authentication_failures(self) -> None:
        reference = {
            "files": [
                {
                    "name": "security.log",
                    "paths": ["dump_info/LogDump/security.log"],
                    "purpose": "auth and security events",
                    "keywords": ["login", "auth", "失败"],
                }
            ],
            "rules": [
                {"match_keywords": ["登录失败"], "include": ["security.log"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            log_file = root / "dump_info" / "LogDump" / "security.log"
            log_file.parent.mkdir(parents=True)
            log_file.write_text(
                "2026-03-31T08:25:00+00:00 sshd[2662]: Received disconnect from 10.0.0.1:11: Too many authentication failures\n"
                "2026-03-31T08:24:52+00:00 security: User Administrator(123) login failed\n",
                encoding="utf-8",
            )

            analysis = pull_bundle.analyze_bundle(
                root,
                "BMC 登录失败",
                reference_data=reference,
                max_files=1,
                max_lines=2,
            )

        evidence_lines = analysis["selected_logs"][0]["evidence_lines"]
        self.assertEqual(
            evidence_lines[0]["line"],
            "2026-03-31T08:24:52+00:00 security: User Administrator(123) login failed",
        )

    def test_expand_log_paths_includes_rotated_files_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            log_dir = root / "dump_info" / "LogDump"
            log_dir.mkdir(parents=True)
            for name in ["app.log.2.gz", "app.log", "app.log.1.gz", "app.log.old"]:
                (log_dir / name).write_text("x\n", encoding="utf-8")

            paths = pull_bundle.expand_log_paths(root, ["dump_info/LogDump/app.log"])

        self.assertEqual([path.name for path in paths], ["app.log", "app.log.1.gz", "app.log.2.gz"])

    def test_collect_evidence_lines_returns_latest_matches_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "operation.log"
            path.write_text(
                "2026-03-11 User Administrator login failed\n"
                "2026-03-12 unrelated operation\n"
                "2026-03-31 User Administrator login failed\n",
                encoding="utf-8",
            )
            evidence = pull_bundle.collect_evidence_lines(path, match_terms=["login"], max_lines=2)

        self.assertEqual([item["line_number"] for item in evidence], [3, 1])

    def test_analyze_bundle_prioritizes_component_specific_appdump_paths(self) -> None:
        reference = {
            "files": [
                {
                    "name": "sync_property_trace.log",
                    "paths": ["dump_info/AppDump/*/sync_property_trace.log"],
                    "purpose": "sync trace",
                    "keywords": ["对象", "不同步", "sync"],
                }
            ],
            "rules": [
                {"match_keywords": ["对象", "不同步"], "include": ["sync_property_trace.log"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            account_log = root / "dump_info" / "AppDump" / "account" / "sync_property_trace.log"
            cooling_log = root / "dump_info" / "AppDump" / "cooling" / "sync_property_trace.log"
            account_log.parent.mkdir(parents=True)
            cooling_log.parent.mkdir(parents=True)
            account_log.write_text("类名 对象名 同步属性名: 属性值 表达式\n", encoding="utf-8")
            cooling_log.write_text("2026-04-07 cooling object sync failed\n", encoding="utf-8")

            analysis = pull_bundle.analyze_bundle(
                root,
                "cooling 对象不同步",
                reference_data=reference,
                max_files=1,
                max_lines=1,
            )

        evidence = analysis["selected_logs"][0]["evidence_lines"][0]
        self.assertIn("/cooling/", evidence["path"])
        self.assertIn("cooling object sync failed", evidence["line"])

    def test_collect_evidence_lines_skips_low_signal_template_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "sync_property_trace.log"
            path.write_text(
                "类名 对象名 同步属性名: 属性值 表达式\n"
                "表达式参数: 同步源服务 同步源资源树路径 同步源资源树接口 同步源属性\n"
                "2026-04-07 cooling property sync failed\n",
                encoding="utf-8",
            )
            evidence = pull_bundle.collect_evidence_lines(path, match_terms=["sync"], max_lines=3)

        self.assertEqual([item["line"] for item in evidence], ["2026-04-07 cooling property sync failed"])

    def test_analyze_bundle_prefers_more_specific_evidence_across_wildcard_paths(self) -> None:
        reference = {
            "files": [
                {
                    "name": "mdb_info.log",
                    "paths": ["dump_info/AppDump/*/mdb_info.log"],
                    "purpose": "object tree",
                    "keywords": ["对象", "object", "sync"],
                }
            ],
            "rules": [
                {"match_keywords": ["对象"], "include": ["mdb_info.log"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            account_log = root / "dump_info" / "AppDump" / "account" / "mdb_info.log"
            hwproxy_log = root / "dump_info" / "AppDump" / "hwproxy" / "mdb_info.log"
            account_log.parent.mkdir(parents=True)
            hwproxy_log.parent.mkdir(parents=True)
            account_log.write_text("/bmc/kepler/AccountService  bmc.kepler.Object.Properties\n", encoding="utf-8")
            hwproxy_log.write_text("2026-04-07 hwproxy object sync failed\n", encoding="utf-8")

            analysis = pull_bundle.analyze_bundle(
                root,
                "对象不更新",
                reference_data=reference,
                max_files=1,
                max_lines=1,
            )

        evidence = analysis["selected_logs"][0]["evidence_lines"][0]
        self.assertIn("/hwproxy/", evidence["path"])
        self.assertEqual(evidence["line"], "2026-04-07 hwproxy object sync failed")

    def test_analyze_bundle_limits_existing_paths_in_output(self) -> None:
        reference = {
            "files": [
                {
                    "name": "mdb_info.log",
                    "paths": ["dump_info/AppDump/*/mdb_info.log"],
                    "purpose": "object tree",
                    "keywords": ["对象"],
                }
            ],
            "rules": [
                {"match_keywords": ["对象"], "include": ["mdb_info.log"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            for index in range(12):
                log_path = root / "dump_info" / "AppDump" / f"comp_{index}" / "mdb_info.log"
                log_path.parent.mkdir(parents=True)
                log_path.write_text(f"2026-04-07 object {index} failed\n", encoding="utf-8")

            analysis = pull_bundle.analyze_bundle(
                root,
                "对象不更新",
                reference_data=reference,
                max_files=1,
                max_lines=1,
            )

        selected = analysis["selected_logs"][0]
        self.assertEqual(selected["existing_path_count"], 12)
        self.assertTrue(selected["existing_paths_truncated"])
        self.assertEqual(len(selected["existing_paths"]), 10)

    def test_parse_log_timestamp_supports_space_and_rfc3339_formats(self) -> None:
        naive = pull_bundle.parse_log_timestamp("2026-03-31 06:28:15 WEB login failed")
        aware = pull_bundle.parse_log_timestamp("2026-03-31T08:24:52.593445+00:00 security: login failed")

        self.assertEqual(naive, "2026-03-31T06:28:15")
        self.assertEqual(aware, "2026-03-31T08:24:52.593445+00:00")

    def test_collect_evidence_lines_prefers_newer_timestamp_over_later_line_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "security.log"
            path.write_text(
                "2026-03-31T08:24:52.593445+00:00 security: User Administrator login failed\n"
                "2026-03-17T05:55:34.429447+00:00 security: User Administrator login failed\n"
                "2026-03-01T00:00:00+00:00 security: User Administrator login failed\n"
                "2026-03-30T00:00:00+00:00 security: User Administrator login failed\n",
                encoding="utf-8",
            )
            evidence = pull_bundle.collect_evidence_lines(path, match_terms=["login"], max_lines=3)

        self.assertEqual(
            [item["timestamp"] for item in evidence],
            [
                "2026-03-31T08:24:52.593445+00:00",
                "2026-03-30T00:00:00+00:00",
                "2026-03-17T05:55:34.429447+00:00",
            ],
        )

    def test_collect_evidence_lines_filters_by_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "operation.log"
            path.write_text(
                "2026-03-30 06:28:15 User Administrator login failed\n"
                "2026-03-31 06:28:15 User Administrator login failed\n"
                "2026-04-01 06:28:15 User Administrator login failed\n",
                encoding="utf-8",
            )
            evidence = pull_bundle.collect_evidence_lines(
                path,
                match_terms=["login"],
                max_lines=5,
                since=pull_bundle.parse_analysis_time_bound("2026-03-31T00:00:00", label="since"),
                until=pull_bundle.parse_analysis_time_bound("2026-03-31T23:59:59", label="until"),
            )

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["timestamp"], "2026-03-31T06:28:15")

    def test_parse_args_accepts_analysis_time_window(self) -> None:
        args = pull_bundle.parse_args(["--analysis-since", "2026-03-31T00:00:00", "--analysis-until", "2026-03-31T23:59:59"])
        self.assertEqual(args.analysis_since, "2026-03-31T00:00:00")
        self.assertEqual(args.analysis_until, "2026-03-31T23:59:59")


class ParseRemoteBundlePathTests(unittest.TestCase):
    def test_prefers_explicit_bundle_path_marker(self) -> None:
        output = "\n".join(
            [
                "collecting...",
                "BUNDLE_PATH=/tmp/openUBMC_20260402-1015.tar.gz",
                "/tmp/older.tar.gz",
            ]
        )
        self.assertEqual(
            pull_bundle.parse_remote_bundle_path(output),
            "/tmp/openUBMC_20260402-1015.tar.gz",
        )

    def test_falls_back_to_last_archive_like_line(self) -> None:
        output = "\n".join(
            [
                "start",
                "/tmp/openUBMC_20260401-0101.tar.gz",
                "/var/tmp/openUBMC_20260402-1015.tar",
            ]
        )
        self.assertEqual(
            pull_bundle.parse_remote_bundle_path(output),
            "/var/tmp/openUBMC_20260402-1015.tar",
        )


class DiscoveryCommandTests(unittest.TestCase):
    def test_build_discovery_command_includes_roots_and_patterns(self) -> None:
        command = pull_bundle.build_discovery_command(
            ["/tmp", "/data"],
            ["*openUBMC*.tar.gz", "*openUBMC*.tar"],
        )
        self.assertIn("/tmp", command)
        self.assertIn("/data", command)
        self.assertIn("*openUBMC*.tar.gz", command)
        self.assertIn("find", command)


if __name__ == "__main__":
    unittest.main()
