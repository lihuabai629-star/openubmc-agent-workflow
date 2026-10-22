"""Offline API contract checks: preview binding, pagination and unknown results."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gitcode_cli as cli


class GitCodeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.body = Path(self.directory.name) / "body.md"
        self.body.write_text("修复行为。\n\n验证结果。\n", encoding="utf-8")
        self.create = ["create-pr", "openUBMC", "component", "--head", "person:fix",
                       "--fork-path", "person/component", "--base", "main",
                       "--title", "修复传感器", "--body-file", str(self.body), "--issues", "12;34"]

    def invoke(self, args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["gitcode_cli.py", *args]), redirect_stdout(output):
            try:
                cli.main()
            except SystemExit as exc:
                if exc.code != 0:
                    raise
        return json.loads(output.getvalue())

    def test_preview_is_offline_and_preserves_complete_body(self):
        with patch.object(cli, "api_request") as request, patch.object(cli, "get_token") as token:
            result = self.invoke(self.create)
        request.assert_not_called()
        token.assert_not_called()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["target"], cli.API_BASE + "/repos/openUBMC/component/pulls")
        self.assertEqual(result["body"]["fork_path"], "person/component")
        self.assertEqual(result["body"]["inner_issue_nums"], "12;34")
        self.assertEqual(result["body"]["body"], self.body.read_text())
        self.assertFalse(result["body"]["prune_source_branch"])

    def test_confirmation_binds_target_and_all_content(self):
        preview = self.invoke(self.create)
        for change in ("body", "target", "branch", "issues"):
            with self.subTest(change=change):
                args = list(self.create)
                if change == "body":
                    self.body.write_text("不同正文\n", encoding="utf-8")
                elif change == "target":
                    args[2] = "another"
                elif change == "branch":
                    args[args.index("--base") + 1] = "release"
                else:
                    args[-1] = "56"
                with patch.object(cli, "api_request") as request:
                    with self.assertRaisesRegex(cli.ApiError, "confirmed_content_mismatch"):
                        self.invoke([*args, "--confirmed", preview["preview_sha256"]])
                    request.assert_not_called()
                self.body.write_text("修复行为。\n\n验证结果。\n", encoding="utf-8")

    def test_matching_confirmation_calls_exact_api_once(self):
        preview = self.invoke(self.create)
        with patch.object(cli, "api_request", return_value={"number": 7}) as request:
            result = self.invoke([*self.create, "--confirmed", preview["preview_sha256"]])
        self.assertEqual(result, {"number": 7})
        request.assert_called_once_with("/repos/openUBMC/component/pulls", body=preview["body"], method="POST")

    def test_cross_fork_requires_correct_fork_path(self):
        args = list(self.create)
        index = args.index("--fork-path")
        del args[index:index + 2]
        with patch.object(cli, "api_request") as request:
            with self.assertRaises(cli.ApiError):
                self.invoke(args)
            args = list(self.create)
            args[args.index("--fork-path") + 1] = "someone/component"
            with self.assertRaises(cli.ApiError):
                self.invoke(args)
            request.assert_not_called()

    def test_update_previews_patch_and_utf8_file(self):
        result = self.invoke(["update-pr", "openUBMC", "component", "7", "--title", "修正说明",
                              "--body-file", str(self.body)])
        self.assertEqual(result["method"], "PATCH")
        self.assertEqual(result["body"]["title"], "修正说明")
        self.assertEqual(result["body"]["body"], self.body.read_text())

    def test_create_issue_and_comment_still_preview(self):
        for args in (["create-issue", "openUBMC", "component", "--title", "问题", "--body-file", str(self.body)],
                     ["post-issue-comment", "openUBMC", "component", "7", "--body-file", str(self.body)]):
            with patch.object(cli, "api_request") as request:
                self.assertEqual(self.invoke(args)["status"], "prepared")
                request.assert_not_called()

    def test_lists_are_paginated_and_partial_reads_fail(self):
        with patch.object(cli, "api_get", side_effect=[[{"number": i} for i in range(100)], [{"number": 100}]]) as get:
            result = self.invoke(["list-prs", "openUBMC", "component"])
        self.assertEqual(len(result), 101)
        self.assertEqual(get.call_args_list[1].args[1]["page"], 2)
        with patch.object(cli, "api_get", side_effect=[[{}] * 100, cli.ApiError("HTTP 503")]):
            with self.assertRaises(cli.ApiError):
                self.invoke(["list-prs", "openUBMC", "component"])
        with patch.object(cli, "api_get", return_value=[{}]):
            with self.assertRaisesRegex(cli.ApiError, "pagination_limit"):
                cli.api_get_all("/repos/a/b/pulls", per_page=1, max_pages=1)

    def test_linked_issues_keep_their_own_repository_and_all_pages(self):
        entries = [{"number": 1, "html_url": "https://gitcode.com/another/repo/issues/42"}]
        with patch.object(cli, "api_get_all", return_value=entries) as get:
            result = self.invoke(["get-pr-linked-issues", "openUBMC", "component", "7"])
        get.assert_called_once()
        self.assertEqual((result[0]["owner"], result[0]["repo"], result[0]["issue_number"]), ("another", "repo", 42))

    def test_unknown_write_is_not_retried_and_errors_hide_credentials(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("synthetic-secret")
        with patch.object(cli, "get_token", return_value="synthetic-secret"), \
                patch.object(cli.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(cli.ApiError, "transport_or_response_failed") as error:
                cli.api_request("/repos/a/b/pulls", body={"title": "fix"}, method="POST")
        self.assertNotIn("synthetic-secret", str(error.exception))
        opener.open.assert_called_once()

    def test_explicit_token_precedes_stored_credentials(self):
        with patch.dict(os.environ, {"GITCODE_TOKEN": "synthetic-explicit"}), \
                patch.object(Path, "read_text", side_effect=AssertionError("must not read credentials")):
            self.assertEqual(cli.get_token(), "synthetic-explicit")

    def test_invalid_api_target_is_rejected_before_preview(self):
        with self.assertRaises(cli.ApiError):
            self.invoke(["update-pr", "..", "component", "7", "--title", "fix", "--body-file", str(self.body)])
        with self.assertRaises(cli.ApiError):
            cli.write_preview("//another.invalid/endpoint", {}, "POST")


if __name__ == "__main__":
    unittest.main()
