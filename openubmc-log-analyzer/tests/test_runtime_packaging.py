from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "openubmc-log-analyzer"
SCRIPTS = SKILL_ROOT / "scripts"
CANONICAL_RUNTIME = REPO_ROOT / "openubmc-target-runtime" / "openubmc_target_runtime"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"openubmc_log_package_{name}",
        SCRIPTS / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LogAnalyzerRuntimePackagingTests(unittest.TestCase):
    def test_package_generates_matching_runtime_contract(self) -> None:
        package_skill = load_script("package_skill")
        distribution = load_script("_runtime_distribution")

        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "openubmc-log-analyzer"
            copied = package_skill.copy_shareable_tree(SKILL_ROOT, output)
            vendor = output / "scripts" / "_vendor" / "openubmc_target_runtime"
            marker = json.loads(
                (output / package_skill.PACKAGE_MARKER).read_text(encoding="utf-8")
            )
            manifest = json.loads((output / "skill.json").read_text(encoding="utf-8"))
            vendored_openssh = (vendor / "openssh.py").is_file()

        expected_digest = distribution.runtime_content_digest(CANONICAL_RUNTIME)
        self.assertEqual(marker["target_runtime"]["contentDigest"], expected_digest)
        self.assertEqual(manifest["targetRuntime"], marker["target_runtime"])
        self.assertIn("scripts/_vendor/openubmc_target_runtime/runtime.py", copied)
        self.assertTrue(vendored_openssh)

    def test_cold_package_loads_v1_without_repository(self) -> None:
        package_skill = load_script("package_skill")
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            output = base / "openubmc-log-analyzer"
            package_skill.copy_shareable_tree(SKILL_ROOT, output)
            probe = (
                f"import sys; sys.path.insert(0, {str(output / 'scripts')!r}); "
                "import target_runtime_adapter as adapter; "
                "print(adapter._load_runtime_module().RUNTIME_API_VERSION)"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                cwd=base,
                env={"PATH": str(Path(sys.executable).parent)},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "openubmc.target-runtime.v1")


if __name__ == "__main__":
    unittest.main()
