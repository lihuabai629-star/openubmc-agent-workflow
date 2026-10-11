from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from urllib import error as urlerror
from urllib import request as urlrequest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-upgrade"))

from openubmc_upgrade.webui import (  # noqa: E402
    WebUiHttpError,
    WebUiHttpSession,
    SameOriginRedirectHandler,
    classify_tasks,
    has_new_task_identity,
    matching_tasks,
    task_id_from_start,
    task_identity_signature,
    tasks_added_since,
    upload_multipart_parts,
    uploaded_file_path,
)


class _Response:
    def __init__(
        self,
        status: int,
        payload: object,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingOpener:
    def __init__(self) -> None:
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if request.full_url.endswith("/UI/Rest/Login"):
            return _Response(
                200,
                {"Token": "csrf-token", "Session": {"SessionID": "session-7"}},
            )
        return _Response(200, {"url": "/UI/Rest/Task/7"})


class _FailingOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def open(self, request, timeout):
        raise urlerror.HTTPError(
            request.full_url,
            500,
            "Internal Server Error",
            {},
            io.BytesIO(self.body),
        )


class WebUiTests(unittest.TestCase):
    def test_multipart_uses_browser_upload_field(self) -> None:
        prefix, suffix, boundary = upload_multipart_parts("component.hpm")

        self.assertIn(b'name="imgfile"', prefix)
        self.assertIn(b'filename="component.hpm"', prefix)
        self.assertTrue(suffix.endswith(f"--{boundary}--\r\n".encode("ascii")))

    def test_task_matching_and_classification_are_artifact_scoped(self) -> None:
        payload = {
            "UpgradeTasks": [
                {
                    "FileName": "other.hpm",
                    "TaskState": "Exception",
                    "ErrorCode": -1,
                },
                {
                    "FileName": "component.hpm",
                    "Component": "HWSR",
                    "TaskState": "Completed",
                    "ErrorCode": 0,
                },
            ]
        }

        selected = matching_tasks(payload, "component.hpm")

        self.assertEqual(len(selected), 1)
        self.assertEqual(classify_tasks(selected), "completed")

    def test_task_success_requires_completed_and_explicit_zero_error_code(self) -> None:
        for error_code in (None, "", False, "invalid"):
            with self.subTest(error_code=error_code):
                self.assertEqual(
                    classify_tasks(
                        [{"TaskState": "Completed", "ErrorCode": error_code}]
                    ),
                    "failed",
                )
        for state in ("Success", "Succeeded"):
            with self.subTest(state=state):
                self.assertEqual(
                    classify_tasks([{"TaskState": state, "ErrorCode": 0}]),
                    "running",
                )

    def test_global_task_matching_rejects_unnamed_tasks(self) -> None:
        payload = {"TaskState": "Completed", "ErrorCode": 0}

        self.assertEqual(matching_tasks(payload, "component.hpm"), [])
        self.assertEqual(
            len(
                matching_tasks(
                    payload,
                    "component.hpm",
                    allow_unnamed=True,
                )
            ),
            1,
        )

    def test_task_identity_baseline_ignores_order_and_dynamic_state(self) -> None:
        baseline = [
            {
                "TaskName": "Task A",
                "Component": "HWSR",
                "FileName": "component.hpm",
                "Percentage": "20%",
                "TaskState": "Running",
                "ErrorCode": 0,
                "Version": "1.54",
            },
            {
                "TaskName": "Task B",
                "Component": "HWSR",
                "FileName": "component.hpm",
                "Percentage": "100%",
                "TaskState": "Completed",
                "ErrorCode": 0,
                "Version": "1.53",
            },
        ]
        current = [
            {
                **baseline[1],
                "Percentage": "100%",
            },
            {
                **baseline[0],
                "Percentage": "100%",
                "TaskState": "Completed",
            },
        ]
        identity_baseline = task_identity_signature(baseline)

        self.assertFalse(has_new_task_identity(current, identity_baseline))

    def test_task_identity_baseline_preserves_duplicate_counts(self) -> None:
        historical = {
            "TaskName": "HWSR Upgrade Task",
            "Component": "HWSR",
            "FileName": "component.hpm",
            "Version": "1.54",
        }
        identity_baseline = task_identity_signature([historical, historical])

        self.assertFalse(
            has_new_task_identity([historical, historical], identity_baseline)
        )
        self.assertFalse(has_new_task_identity([historical], identity_baseline))
        self.assertTrue(
            has_new_task_identity(
                [historical, historical, historical],
                identity_baseline,
            )
        )

        current = [
            {**historical, "TaskState": "Running", "ErrorCode": 0},
            {**historical, "TaskState": "Completed", "ErrorCode": 0},
            {**historical, "TaskState": "Completed", "ErrorCode": 0},
        ]
        candidates = tasks_added_since(current, identity_baseline)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(classify_tasks(candidates), "running")

    def test_task_identity_baseline_rejects_malformed_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "baseline is malformed"):
            has_new_task_identity([], [["incomplete"]])

    def test_multipart_rejects_header_control_characters(self) -> None:
        for filename in ('bad"name.hpm', "bad\rname.hpm", "bad\nname.hpm"):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(ValueError, "safe artifact filename"):
                    upload_multipart_parts(filename)

    def test_redirect_handler_rejects_cross_origin_redirects(self) -> None:
        handler = SameOriginRedirectHandler()
        request = urlrequest.Request(
            "https://bmc.example/UI/Rest/Task",
            headers={"X-CSRF-Token": "secret-token"},
        )

        with self.assertRaisesRegex(urlerror.HTTPError, "changed request origin"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example/collect",
            )

    def test_uploaded_path_is_confined_to_tmp_web(self) -> None:
        self.assertEqual(
            uploaded_file_path(
                {"FilePath": "/tmp/web/component.hpm"},
                "component.hpm",
            ),
            "/tmp/web/component.hpm",
        )
        self.assertEqual(
            uploaded_file_path(
                {"FilePath": "/tmp/web/../other.hpm"},
                "component.hpm",
            ),
            "/tmp/web/component.hpm",
        )

    def test_login_keeps_password_out_of_followup_headers(self) -> None:
        deleted = []
        session = WebUiHttpSession(
            origin="https://bmc.example:443",
            username="Administrator",
            password="web-secret",
            verify_tls=False,
            redfish_request=lambda method, path: deleted.append((method, path)),
        )
        opener = _RecordingOpener()
        session.opener = opener

        session.login()
        started = session.start("/tmp/web/component.hpm")
        session.close()

        self.assertEqual(task_id_from_start(started.payload), "7")
        followup = opener.requests[1][0]
        headers = {key.lower(): value for key, value in followup.header_items()}
        self.assertEqual(headers["x-csrf-token"], "csrf-token")
        self.assertEqual(headers["from"], "WebUI")
        self.assertNotIn("web-secret", json.dumps(headers))
        self.assertEqual(
            deleted,
            [("DELETE", "/redfish/v1/SessionService/Sessions/session-7")],
        )

    def test_login_accepts_case_insensitive_csrf_response_header(self) -> None:
        class HeaderTokenOpener:
            @staticmethod
            def open(_request, timeout):
                del timeout
                return _Response(
                    200,
                    {"Session": {"SessionID": "session-8"}},
                    {"x-csrf-token": "header-token"},
                )

        session = WebUiHttpSession(
            origin="https://bmc.example:443",
            username="Administrator",
            password="web-secret",
            verify_tls=False,
            redfish_request=lambda _method, _path: None,
        )
        session.opener = HeaderTokenOpener()

        session.login()

        self.assertTrue(session.logged_in)

    def test_http_error_reports_digest_without_response_body(self) -> None:
        body = b'{"Password":"response-secret"}'
        session = WebUiHttpSession(
            origin="https://bmc.example:443",
            username="Administrator",
            password="request-secret",
            verify_tls=False,
        )
        session.opener = _FailingOpener(body)

        with self.assertRaises(WebUiHttpError) as raised:
            session.request_json("GET", "/UI/Rest/Failure")

        message = str(raised.exception)
        self.assertIn(hashlib.sha256(body).hexdigest(), message)
        self.assertNotIn("response-secret", message)
        self.assertNotIn("request-secret", message)

    def test_close_resets_state_and_allows_a_fresh_login(self) -> None:
        deleted = []
        session = WebUiHttpSession(
            origin="https://bmc.example:443",
            username="Administrator",
            password="web-secret",
            verify_tls=False,
            redfish_request=lambda method, path: deleted.append((method, path)),
        )
        opener = _RecordingOpener()
        session.opener = opener

        session.login()
        session.close()
        session.login()

        login_requests = [
            request
            for request, _timeout in opener.requests
            if request.full_url.endswith("/UI/Rest/Login")
        ]
        self.assertEqual(len(login_requests), 2)
        self.assertTrue(session.logged_in)
        self.assertEqual(
            deleted,
            [("DELETE", "/redfish/v1/SessionService/Sessions/session-7")],
        )

    def test_close_reports_cleanup_failure(self) -> None:
        cleanup_attempts = 0

        def cleanup(_method, _path):
            nonlocal cleanup_attempts
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise OSError("cleanup unavailable")
            return SimpleNamespace(status=200)

        session = WebUiHttpSession(
            origin="https://bmc.example:443",
            username="Administrator",
            password="web-secret",
            verify_tls=False,
            redfish_request=cleanup,
        )
        session.opener = _RecordingOpener()
        session.login()

        evidence = session.close()
        retry = session.close()

        self.assertTrue(evidence["attempted"])
        self.assertFalse(evidence["completed"])
        self.assertIn("OSError", evidence["error"])
        self.assertTrue(retry["completed"])
        self.assertEqual(cleanup_attempts, 2)


if __name__ == "__main__":
    unittest.main()
