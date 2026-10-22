import importlib.util
from pathlib import Path
import unittest
import json
import stat
import subprocess
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("publish_package", Path(__file__).resolve().parents[1] / "scripts/publish_package.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
RECIPE = "component/1.0@openubmc/stable"
RREV, PACKAGE, PREV = "a" * 32, "b" * 40, "c" * 32
REF = f"{RECIPE}#{RREV}:{PACKAGE}#{PREV}"


def listing(namespace, revision=PREV):
    return {namespace: {RECIPE: {"revisions": {RREV: {"packages": {
        PACKAGE: {"revisions": {revision: {}}},
        "d" * 40: {"revisions": {"e" * 32: {}}},
    }}}}}}


class PublishPackageTests(unittest.TestCase):
    def test_failed_commands_keep_raw_output_private_and_classify_the_remedy(self):
        for output, code in (
            ("ERROR: Remote 'missing' not found (remote not found)", "remote_not_found"),
            ("SSLError: certificate verify failed", "tls_error"),
            ("HTTP 403 Forbidden", "permission_denied"),
            ("HTTP 401 Unauthorized", "authentication_rejected"),
            ("ConnectionError: connection refused", "network_error"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as raw:
                secret = "Bearer private-fixture-token"
                completed = subprocess.CompletedProcess([], 1, "", output + "\n" + secret)
                with patch.object(module.subprocess, "run", return_value=completed):
                    with self.assertRaises(module.ConanFailure) as raised:
                        module.run_conan(["upload", REF, "-r", "remote"], diagnostics_dir=raw)
                error = raised.exception
                report = error.to_public()
                self.assertEqual(report["code"], code)
                self.assertNotIn(secret, json.dumps(report))
                path = Path(report["diagnostic_path"])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertIn(secret, json.loads(path.read_text())["stderr"])

    def test_timeout_and_invalid_response_are_diagnostic_failures(self):
        for effect, code in (
            (subprocess.TimeoutExpired(["conan"], 600, stderr=b"private"), "timeout"),
            (FileNotFoundError("private path"), "conan_unavailable"),
            (subprocess.CompletedProcess([], 0, "private invalid JSON", ""), "invalid_response"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as raw:
                kwargs = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
                with patch.object(module.subprocess, "run", **kwargs), self.assertRaises(module.ConanFailure) as raised:
                    module.run_conan(["list", REF], diagnostics_dir=raw)
                self.assertEqual(raised.exception.code, code)
                self.assertTrue(Path(raised.exception.diagnostic_path).is_file())

    def test_diagnostic_write_failure_is_explicit_and_never_leaks_the_conan_output(self):
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "secret", "401")), \
                patch.object(module, "save_diagnostics", return_value=None), self.assertRaises(module.ConanFailure) as raised:
            module.run_conan(["list", REF])
        self.assertFalse(raised.exception.to_public()["diagnostics_saved"])
        self.assertNotIn("secret", str(raised.exception))

    def test_only_verified_binary_is_selected_even_with_other_cached_packages(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return listing("Local Cache" if "-c" in argv else "chosen-remote")
        result = module.publish(REF, "chosen-remote", upload=True, runner=runner)
        self.assertEqual(result["status"], "published")
        self.assertEqual([argv[1] for argv in calls], [REF, REF, REF])
        self.assertEqual([argv[0] for argv in calls], ["list", "upload", "list"])

    def test_partial_or_wildcard_refs_never_invoke_conan(self):
        for ref in (RECIPE, f"{RECIPE}#{RREV}", f"{RECIPE}#{RREV}:*",
                    f"{RECIPE}#{RREV}:{PACKAGE}", REF.replace(PREV, "latest")):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                module.publish(ref, "remote", upload=True,
                               runner=lambda _: self.fail("Conan must not run"))

    def test_missing_local_binary_prevents_upload(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return listing("Local Cache", "f" * 32)
        with self.assertRaises(ValueError):
            module.publish(REF, "remote", upload=True, runner=runner)
        self.assertEqual(len(calls), 1)

    def test_recipe_or_other_remote_revision_does_not_prove_success(self):
        def runner(argv):
            return listing("Local Cache") if "-c" in argv else listing("remote", "f" * 32)
        with self.assertRaises(ValueError):
            module.publish(REF, "remote", upload=True, runner=runner)

    def test_preview_does_not_upload(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return listing("Local Cache")
        result = module.publish(REF, "remote", runner=runner)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(len(calls), 1)
