from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from release_gates import (  # noqa: E402
    inspect_final_package,
    lua_source_gate,
    release_result,
    rollback_gate,
    service_start_smoke,
)


class ReleaseGateTests(unittest.TestCase):
    def test_lua_syntax_error_blocks_before_packaging(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "component.lua"
            path.write_text("broken", encoding="utf-8")
            gate = lua_source_gate(
                {"component": path},
                checker=["fake-luac"],
                runner=lambda *args, **kwargs: type("Result", (), {"returncode": 1})(),
            )
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(release_result(gate)["status"], "rejected")

    def test_final_package_detects_missing_and_truncated_files(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "firmware.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("rootfs/app.lua", "-- TRUNCATED\n")
            gate = inspect_final_package(package, expected_files={"rootfs/app.lua": "" , "manifest.json": ""})
        self.assertEqual(gate["status"], "fail")
        self.assertIn("manifest.json", gate["failures"]["missing"])
        self.assertIn("rootfs/app.lua", gate["failures"]["truncated"])

    def test_service_smoke_binds_target_and_version_and_blocks_failure(self):
        gate = service_start_smoke(
            lambda service: service == "network.service",
            required_services=["network.service", "bmc.service"],
            target="f904t",
            address="192.0.2.10",
            version="2.0.14",
            command=["systemctl", "is-active"],
        )
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["target"], "f904t")

    def test_rollback_requires_preexisting_artifact_and_verification(self):
        missing = rollback_gate(recovery_artifact=None, rollback=lambda: {"verified": True})
        self.assertEqual(missing["status"], "fail")
        passed = rollback_gate(
            recovery_artifact={
                "sha256": "a" * 64,
                "version": "1.0",
                "established_before_mutation": True,
            },
            rollback=lambda: {
                "verified": True,
                "artifact_sha256": "a" * 64,
                "version": "1.0",
                "evidence_ids": ["rollback-check"],
            },
        )
        self.assertEqual(passed["status"], "pass")

    def test_release_result_requires_every_named_gate_once(self):
        incomplete = release_result(
            {"gate": "lua-source-syntax", "status": "pass"},
            required_gates=("lua-source-syntax", "package-completeness"),
        )
        self.assertEqual(incomplete["status"], "rejected")
        self.assertIn("package-completeness", incomplete["failed_gates"])
        duplicate = release_result(
            {"gate": "lua-source-syntax", "status": "pass"},
            {"gate": "lua-source-syntax", "status": "pass"},
            required_gates=("lua-source-syntax",),
        )
        self.assertEqual(duplicate["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
