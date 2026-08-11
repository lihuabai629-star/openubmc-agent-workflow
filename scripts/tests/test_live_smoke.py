from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "live_smoke.py"
SPEC = importlib.util.spec_from_file_location("openubmc_live_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
live_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_smoke)


class LiveSmokeTests(unittest.TestCase):
    def test_probe_target_returns_bounded_capability_summary(self) -> None:
        payload = {
            "ok": True,
            "code": "ok",
            "observed_at": "2026-08-11T00:00:00Z",
            "result": {
                "capabilities": {
                    "ssh_transport": True,
                    "mdbctl": True,
                    "remote_object": True,
                    "ignored": "large detail",
                },
                "failed_checks": [],
                "checks": {"SSH": {"lines": ["clock", "uptime"]}},
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(live_smoke.subprocess, "run", return_value=completed):
            result = live_smoke.probe_target(
                "10.0.0.1",
                environment={},
                timeout=5,
                deadline=20,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["clock"], "clock")
        self.assertEqual(
            result["capabilities"],
            {"ssh_transport": True, "mdbctl": True, "remote_object": True},
        )

    def test_probe_timeout_is_a_stable_result(self) -> None:
        with mock.patch.object(
            live_smoke.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["probe"], 10),
        ):
            result = live_smoke.probe_target(
                "10.0.0.2",
                environment={},
                timeout=5,
                deadline=20,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "probe_timeout")

    def test_comparison_reports_capability_drift(self) -> None:
        result = live_smoke.compare_probes([
            {"ok": True, "capabilities": {"ssh_transport": True, "mdbctl": True, "remote_object": True}},
            {"ok": False, "capabilities": {"ssh_transport": False, "mdbctl": False, "remote_object": False}},
        ])
        self.assertFalse(result["all_targets_ready"])
        self.assertEqual(
            result["differing_capabilities"],
            ["ssh_transport", "mdbctl", "remote_object"],
        )

    def test_main_returns_success_for_a_plain_ready_comparison(self) -> None:
        ready = {
            "ok": True,
            "capabilities": {
                "ssh_transport": True,
                "mdbctl": True,
                "remote_object": True,
            },
        }
        with (
            mock.patch.object(
                live_smoke,
                "probe_target",
                side_effect=lambda target, **_: {"target": target, **ready},
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = live_smoke.main(["--target", "10.0.0.1", "--target", "10.0.0.2"])
        self.assertEqual(result, 0)

    def test_upgrade_preflight_defaults_to_internal_tls_mode(self) -> None:
        ready = {
            "ok": True,
            "capabilities": {
                "ssh_transport": True,
                "mdbctl": True,
                "remote_object": True,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "firmware.hpm"
            artifact.write_bytes(b"fixture")
            with (
                mock.patch.object(
                    live_smoke,
                    "probe_target",
                    side_effect=lambda target, **_: {"target": target, **ready},
                ),
                mock.patch.object(live_smoke, "run", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                result = live_smoke.main(
                    [
                        "--target", "10.0.0.1",
                        "--target", "10.0.0.2",
                        "--upgrade-artifact", str(artifact),
                        "--product-version", "1.2.3",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertIn("--allow-insecure-tls", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
