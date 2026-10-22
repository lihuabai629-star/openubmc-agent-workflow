from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import preflight_upgrade
import redfish_credentials


class ActivatedUpgradeCredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "openubmc/credentials.json"
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({"schema_version": 1}))
        self.source.chmod(0o600)
        configuration = importlib.import_module(
            redfish_credentials._runtime.__name__ + ".configuration"
        )
        store = configuration.LocalConfigurationStore(self.source, kind="targets")
        store.save_and_activate(
            {
                "schema_version": 1,
                "credentials": {
                    "redfish-account": {"user": "target-redfish", "password": "fixture-redfish-secret"},
                    "ssh-account": {"user": "other-ssh", "password": "fixture-ssh-secret"},
                },
                "targets": {"192.0.2.39": {"bmc": {"redfish": "redfish-account", "ssh": "ssh-account"}}},
            },
            expected_revision=None,
            expected_active_revision=None,
            expected_source_text=self.source.read_text(),
        )
        environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_lookup_uses_activated_protocol_specific_target(self):
        selected = redfish_credentials.resolve_target_credentials("https://192.0.2.39")
        self.assertEqual(selected.user, "target-redfish")
        self.assertEqual(selected.password, "fixture-redfish-secret")
        self.assertEqual(selected.port, 443)
        with self.assertRaises(Exception) as raised:
            redfish_credentials.resolve_target_credentials("https://192.0.2.40")
        self.assertEqual(raised.exception.code, "credentials_missing")

    def test_explicit_legacy_source_preserves_account_selection(self):
        legacy = self.root / "explicit.env"
        legacy.write_text("REDFISH_USERNAME=explicit-redfish\nREDFISH_PASSWORD=explicit-fixture-secret\n")
        legacy.chmod(0o600)
        selected = redfish_credentials.resolve_target_credentials("https://192.0.2.39", str(legacy))
        self.assertEqual(selected.user, "explicit-redfish")
        self.assertEqual(selected.password, "explicit-fixture-secret")

    def test_local_diagnostic_reuses_activation_without_reporting_authentication(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["redfish_credentials.py", "--target", "https://192.0.2.39"]), contextlib.redirect_stdout(output):
            self.assertEqual(redfish_credentials.main(), 0)
        document = json.loads(output.getvalue())
        self.assertTrue(document["password_present"])
        self.assertEqual(document["remote_authentication"], "not_checked")
        self.assertNotIn("fixture-redfish-secret", output.getvalue())

    def test_preflight_reaches_transport_with_activated_account_and_keeps_tls_strict(self):
        artifact = self.root / "rootfs.hpm"
        artifact.write_bytes(b"preflight fixture")
        backend = importlib.import_module("openubmc_upgrade.runtime_backend")
        output = io.StringIO()
        with mock.patch.object(backend, "RedfishHttpSession", side_effect=ssl.SSLCertVerificationError("fixture hostname mismatch")) as session, contextlib.redirect_stdout(output):
            result = preflight_upgrade.main([
                "--target", "https://192.0.2.39", "--artifact-path", str(artifact),
                "--artifact-sha256", hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "--product-version", "1.2.3.4",
            ])
        self.assertEqual(result, 1)
        session.assert_called_once()
        self.assertEqual(session.call_args.kwargs["credentials"].user, "target-redfish")
        self.assertIs(session.call_args.kwargs["verify_tls"], True)
        self.assertIn("fixture hostname mismatch", json.loads(output.getvalue())["error"])
        self.assertNotIn("fixture-redfish-secret", output.getvalue())
