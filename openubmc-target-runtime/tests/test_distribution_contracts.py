from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGER = REPO_ROOT / "openubmc-target-runtime" / "tools" / "package_runtime_skill.py"


class RuntimeDistributionContractTests(unittest.TestCase):
    def package(self, skill_name: str, output: Path) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(PACKAGER),
                "--source",
                str(REPO_ROOT / skill_name),
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def isolated_import(self, package_root: Path, probe: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", probe],
            cwd=package_root.parent,
            env={"PATH": str(Path(sys.executable).parent)},
            capture_output=True,
            text=True,
        )

    def test_live_patch_and_upgrade_packages_load_the_generated_runtime(self) -> None:
        cases = {
            "openubmc-live-patch": (
                "openubmc_live_patch.runtime_backend",
                "target_runtime_adapter",
            ),
            "openubmc-upgrade": (
                "openubmc_upgrade.runtime_backend",
                "target_runtime_adapter",
            ),
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for skill_name, (backend_module, adapter_module) in cases.items():
                with self.subTest(skill=skill_name):
                    output = root / skill_name
                    self.package(skill_name, output)
                    manifest = json.loads(
                        (output / "skill.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        manifest["targetRuntime"]["source"],
                        "generated-from-canonical",
                    )
                    self.assertIn("scripts/_runtime_loader.py", manifest["files"])
                    self.assertIn(
                        "scripts/_vendor/openubmc_target_runtime/runtime.py",
                        manifest["files"],
                    )
                    self.assertIn(
                        "scripts/_vendor/openubmc_target_runtime/task_context.py",
                        manifest["files"],
                    )
                    probe = (
                        "import sys;"
                        f"sys.path.insert(0,{str(output / 'scripts')!r});"
                        f"sys.path.insert(0,{str(output)!r});"
                        f"import {adapter_module} as adapter;"
                        f"import {backend_module};"
                        "print(adapter._runtime.RUNTIME_API_VERSION)"
                    )
                    completed = self.isolated_import(output, probe)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        completed.stdout.strip(), "openubmc.target-runtime.v1"
                    )

    def test_tampered_vendor_is_rejected_even_when_an_installed_v1_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "openubmc-live-patch"
            self.package("openubmc-live-patch", output)
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
            installed_root = root / "installed"
            installed_package = installed_root / "openubmc_target_runtime"
            installed_package.mkdir(parents=True)
            (installed_package / "__init__.py").write_text(
                'RUNTIME_API_VERSION = "openubmc.target-runtime.v1"\n',
                encoding="utf-8",
            )
            probe = (
                "import sys;"
                f"sys.path.insert(0,{str(installed_root)!r});"
                f"sys.path.insert(0,{str(output / 'scripts')!r});"
                "import target_runtime_adapter"
            )
            completed = self.isolated_import(output, probe)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("content digest", completed.stderr.lower())


if __name__ == "__main__":
    unittest.main()
