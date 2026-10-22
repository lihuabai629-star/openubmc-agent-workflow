from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import json
import os


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_route import equivalence_receipt, route_receipt  # noqa: E402


class BuildRouteTests(unittest.TestCase):
    def test_build_environment_installation_selects_the_toolchain_specialist(self):
        for request in (
            "安装构建环境", "配置构建环境", "安装开发环境", "配置开发环境", "安装 openUBMC 构建环境",
            "install build environment", "setup build environment",
            "set up the development environment", "configure the build environment",
        ):
            with self.subTest(request=request):
                receipt = route_receipt(request)
                self.assertEqual(receipt["owner"], "openubmc-env-init")
                self.assertEqual(receipt["mode"], "handoff")
                self.assertEqual(receipt["handoff"], "openubmc-env-init")
                self.assertEqual(receipt["tool"], "")

    def test_build_configuration_and_failures_do_not_enter_plugin_setup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "conanfile.py").write_text("", encoding="utf-8")
            for request in (
                "配置环境变量后编译组件", "配置构建环境变量后编译", "修改配置并验证测试环境",
                "Conan 登录失败，检查构建环境", "Conan dependency resolution failed",
                "compile component and configure its environment variables",
                "configure build environment variables and compile component",
                "install component into the test environment",
                "只编译，不要安装构建环境",
                "compile component; do not configure the build environment",
            ):
                with self.subTest(request=request):
                    receipt = route_receipt(request, workspace=root)
                    self.assertEqual(receipt["owner"], "openubmc-build")
                    self.assertEqual(receipt["handoff"], "")
                    self.assertTrue(receipt["ready"])
                    self.assertEqual(receipt["tool"], "" if receipt["mode"] == "diagnose" else "bmcgo")

    def test_diagnosis_does_not_select_a_build_command(self):
        for request in ("分析 HPM 构建失败日志，不要重新构建", "检查 Conan 依赖报错",
                        "diagnose HPM build failure; do not rebuild"):
            receipt = route_receipt(request, argv=["bmcgo", "build", "-b", "openUBMC"])
            self.assertEqual(receipt["mode"], "diagnose")
            self.assertEqual(receipt["tool"], "")
            self.assertTrue(receipt["ready"])

    def test_component_command_and_later_publication_keep_current_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "conanfile.py").touch()
            (root / ".bmcgo").mkdir()
            (root / ".bmcgo/config").touch()
            for request, argv, mode in (
                ("只编译组件", ["bmcgo", "build"], "validate"),
                ("构建产品HPM，完成后上传 conan 包", ["bmcgo", "build", "-b", "openUBMC"], "product-artifact"),
                ("compile component then upload package", ["bmcgo", "build"], "validate"),
                ("compile debug component then upload Conan package", ["bmcgo", "build"], "validate"),
            ):
                receipt = route_receipt(request, argv=argv, workspace=root)
                self.assertEqual((receipt["owner"], receipt["mode"], receipt["tool"]),
                                 ("openubmc-build", mode, "bmcgo"))
            receipt = route_receipt("上传 conan 包", argv=["conan", "upload", "reference"], workspace=root)
            self.assertEqual((receipt["owner"], receipt["tool"]), ("openubmc-publish", ""))

    def test_public_build_plan_rejects_a_forged_equivalence_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkout = root / "component"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            (checkout / "conanfile.py").write_text("from conan import ConanFile\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "base"], check=True)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps({
                "source": "sha256:" + "a" * 64,
                "profile": "default", "options": ["create", "."],
                "dependency_graph": "sha256:" + "b" * 64,
                "expected_artifact": {"kind": "component-package", "version": "1.0"},
                "release_gates": ["unit-tests"],
            }), encoding="utf-8")
            binary = root / "bin"
            binary.mkdir()
            conan = binary / "conan"
            conan.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conan.chmod(0o755)
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "create_build_plan.py"),
                "--mode", "validate", "--workspace", f"component={checkout}",
                "--cwd", str(checkout), "--output", str(root / "plan.json"),
                "--equivalence-receipt", str(receipt_path), "--",
                "conan", "create", ".",
            ], capture_output=True, text=True, check=False,
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tool_equivalence", result.stderr)
            self.assertFalse((root / "plan.json").exists())


    def test_retired_bingo_requests_never_select_a_tool_or_handoff(self):
        for request, argv in (
            ("run bingo build", ["bingo", "build"]),
            ("开发 bingo 构建工具", None),
            ("build", ["bingo.exe", "build"]),
            ("build", ["/usr/local/bin/bingo", "build"]),
            ("run bingo build", ["ninja"]),
            ("configure build environment and run explicit build", ["bingo", "build"]),
        ):
            with self.subTest(request=request, argv=argv):
                result = route_receipt(request, argv=argv)
                self.assertEqual(result['mode'], 'unsupported')
                self.assertFalse(result['ready'])
                self.assertEqual(result['tool'], '')
                self.assertEqual(result['handoff'], '')

    def test_product_and_component_routes_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".bmcgo").mkdir()
            (root / ".bmcgo" / "config").write_text("product\n")
            product = route_receipt("构建产品 HPM", workspace=root)
            component = route_receipt("compile component and run unit test", workspace=root)
        self.assertEqual(product["mode"], "product-artifact")
        self.assertEqual(component["mode"], "validate")
        self.assertNotEqual(product["mode"], component["mode"])

    def test_product_route_stops_when_manifest_precondition_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            receipt = route_receipt("build product HPM", workspace=Path(raw))
        self.assertFalse(receipt["ready"])
        self.assertEqual(receipt["preconditions"][0]["status"], "failed")

    def test_component_validation_rejects_an_unrelated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            receipt = route_receipt("compile component", workspace=Path(raw))
        self.assertFalse(receipt["ready"])
        self.assertEqual(receipt["preconditions"][0]["name"], "component_workspace")

    def test_raw_conan_drift_is_blocked_without_a_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "conanfile.py").write_text("", encoding="utf-8")
            receipt = route_receipt("run conan create", argv=["conan", "create", "."], workspace=root)
        self.assertFalse(receipt["ready"])
        self.assertIn("raw conan", receipt["reason"])
        self.assertEqual(receipt["preconditions"][-1]["name"], "tool_equivalence")

    def test_substitution_is_bound_to_a_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "conanfile.py").write_text("", encoding="utf-8")
            receipt = route_receipt(
                "compile component",
                argv=["ninja", "test"],
                workspace=root,
                equivalence={
                    "source": "sha256:" + "a" * 64,
                    "profile": "debug",
                    "options": ["release"],
                    "dependency_graph": "sha256:" + "b" * 64,
                    "expected_artifact": {"kind": "component-package", "version": "1.2.3"},
                    "release_gates": ["unit-tests"],
                },
            )
        self.assertFalse(receipt["ready"])
        self.assertTrue(receipt["plan_binding_required"])
        self.assertEqual(receipt["tool"], "")
        self.assertTrue(receipt["equivalence"]["digest"].startswith("sha256:"))

    def test_tool_substitution_requires_a_complete_equivalence_receipt(self) -> None:
        with self.assertRaisesRegex(ValueError, "dependency_graph"):
            equivalence_receipt({"source": "sha256:source", "profile": "debug"})
        receipt = equivalence_receipt({
            "source": "sha256:" + "a" * 64,
            "profile": "debug",
            "options": ["release"],
            "dependency_graph": "sha256:" + "b" * 64,
            "expected_artifact": {"kind": "openubmc-hpm", "version": "1.2.3"},
            "release_gates": ["lua_syntax", "hpm_containment"],
        })
        self.assertTrue(receipt["claim_complete"])
        self.assertTrue(str(receipt["digest"]).startswith("sha256:"))

    def test_cli_returns_failure_for_a_missing_workspace_precondition(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_route.py"),
             "--request", "build product HPM", "--workspace", tempfile.gettempdir()],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
