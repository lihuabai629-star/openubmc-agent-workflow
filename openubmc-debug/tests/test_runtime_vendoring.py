#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "openubmc-debug"
SCRIPTS = SKILL_ROOT / "scripts"
CANONICAL_RUNTIME = REPO_ROOT / "openubmc-target-runtime" / "openubmc_target_runtime"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"openubmc_debug_vendoring_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeVendoringTests(unittest.TestCase):
    def test_case_literal_scanner_distinguishes_code_types_from_named_alarms(self) -> None:
        package_skill = load_script("package_skill")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "runtime.py"
            source.write_text(
                "def apply(event):\n"
                "    if event == WorkflowDefinition:\n"
                "        return AcceptancePlan\n",
                encoding="utf-8",
            )
            package_skill.scan_forbidden_literals(root)
            source.write_text(
                'description = "FanFailure alarm"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "inline-alarm-literal"):
                package_skill.scan_forbidden_literals(root)

    def test_packaging_generates_matching_runtime_and_records_contract(self) -> None:
        package_skill = load_script("package_skill")
        distribution = load_script("_runtime_distribution")

        self.assertFalse((SKILL_ROOT / "scripts" / "_vendor").exists())
        expected_digest = distribution.runtime_content_digest(CANONICAL_RUNTIME)
        expected_api = distribution.read_runtime_api_version(CANONICAL_RUNTIME)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "openubmc-debug"
            copied = package_skill.copy_shareable_tree(SKILL_ROOT, output)
            vendor = output / "scripts" / "_vendor" / "openubmc_target_runtime"

            self.assertTrue((vendor / "__init__.py").is_file())
            self.assertEqual(
                distribution.runtime_content_digest(vendor), expected_digest
            )
            self.assertIn(
                "scripts/_vendor/openubmc_target_runtime/__init__.py", copied
            )

            marker = json.loads(
                (output / package_skill.PACKAGE_MARKER).read_text(encoding="utf-8")
            )
            manifest = json.loads((output / "skill.json").read_text(encoding="utf-8"))
            expected = {
                "apiVersion": expected_api,
                "contentDigest": expected_digest,
                "vendorPath": "scripts/_vendor/openubmc_target_runtime",
                "source": "generated-from-canonical",
            }
            self.assertEqual(marker["target_runtime"], expected)
            self.assertEqual(manifest["targetRuntime"], expected)
            self.assertIn(
                "scripts/_vendor/openubmc_target_runtime/runtime.py",
                manifest["files"],
            )

    def test_cold_package_loads_v1_without_control_plane_repository(self) -> None:
        package_skill = load_script("package_skill")

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            output = base / "cold" / "openubmc-debug"
            package_skill.copy_shareable_tree(SKILL_ROOT, output)
            probe = (
                f"import sys; sys.path.insert(0, {str(output / 'scripts')!r}); "
                "import _target_runtime_adapter as adapter; "
                "assert not hasattr(adapter, 'select_runtime_engine'); "
                "print(adapter._load_runtime_module().RUNTIME_API_VERSION)"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                cwd=base,
                env={
                    "PATH": str(Path(sys.executable).parent),
                    "PYTHONPATH": str(output / "scripts"),
                    "OPENUBMC_TARGET_RUNTIME_ENGINE": "legacy",
                },
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["openubmc.target-runtime.v1"],
        )

    def test_digest_mismatch_fails_before_transport_creation(self) -> None:
        package_skill = load_script("package_skill")

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            output = base / "openubmc-debug"
            package_skill.copy_shareable_tree(SKILL_ROOT, output)
            runtime_file = (
                output
                / "scripts"
                / "_vendor"
                / "openubmc_target_runtime"
                / "runtime.py"
            )
            runtime_file.write_text(
                runtime_file.read_text(encoding="utf-8") + "\n# tampered\n",
                encoding="utf-8",
            )
            sentinel = base / "transport-created"
            probe = f"""
import argparse
import sys
sys.path.insert(0, {str(output / 'scripts')!r})
import _target_runtime_adapter as adapter

class Transport:
    def __init__(self, **_kwargs):
        open({str(sentinel)!r}, "w", encoding="utf-8").write("created")

args = argparse.Namespace(
    ip="example.invalid",
    ssh_port=22,
    telnet_port=23,
    redfish_port=443,
    ssh_user="root",
    ssh_user_env="",
    ssh_password_env="OPENUBMC_SSH_PASSWORD",
    ssh_identity_file="",
    ssh_host_key_policy="strict",
    ssh_known_hosts_file="",
    allow_insecure_host_key=False,
)
adapter.ObjectAlarmRuntimeLease(
    args=args,
    credential_loader=lambda: {{"user": "root", "password": "<secret>", "port": 22}},
    task_id="digest-mismatch",
    transport_factory=Transport,
)
"""
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                cwd=base,
                env={
                    "PATH": str(Path(sys.executable).parent),
                    "PYTHONPATH": str(output / "scripts"),
                    "OPENUBMC_TARGET_RUNTIME_ENGINE": "v1",
                },
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("content digest mismatch", result.stderr)
        self.assertIn("repair or reinstall", result.stderr)
        self.assertFalse(sentinel.exists())

    def test_api_mismatch_fails_before_runtime_import(self) -> None:
        package_skill = load_script("package_skill")

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            output = base / "openubmc-debug"
            package_skill.copy_shareable_tree(SKILL_ROOT, output)
            marker_path = output / package_skill.PACKAGE_MARKER
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["target_runtime"]["apiVersion"] = "openubmc.target-runtime.v2"
            marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
            probe = (
                f"import sys; sys.path.insert(0, {str(output / 'scripts')!r}); "
                "import _target_runtime_adapter as adapter; adapter._load_runtime_module()"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                cwd=base,
                env={
                    "PATH": str(Path(sys.executable).parent),
                    "PYTHONPATH": str(output / "scripts"),
                },
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Runtime API mismatch", result.stderr)
        self.assertIn("repair or reinstall", result.stderr)


if __name__ == "__main__":
    unittest.main()
