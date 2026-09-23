from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import json


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_route import equivalence_receipt, route_receipt  # noqa: E402


class BuildRouteTests(unittest.TestCase):
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
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "create_build_plan.py"),
                "--mode", "validate", "--workspace", f"component={checkout}",
                "--cwd", str(checkout), "--output", str(root / "plan.json"),
                "--equivalence-receipt", str(receipt_path), "--",
                "conan", "create", ".",
            ], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tool_equivalence", result.stderr)
            self.assertFalse((root / "plan.json").exists())

    def test_public_build_plan_hands_explicit_bingo_command_to_bingo_owner(self) -> None:
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
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "create_build_plan.py"),
                "--mode", "validate", "--workspace", f"component={checkout}",
                "--cwd", str(checkout), "--output", str(root / "plan.json"),
                "--", "bingo", "build",
            ], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bingo", result.stderr.lower())

    def test_product_release_package_contract_uses_the_ordinary_bingo_command(self) -> None:
        product_reference = (
            ROOT / "references" / "modes" / "product-artifact.md"
        ).read_text(encoding="utf-8")
        publish_skill = (ROOT.parent / "openubmc-publish" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "bingo build -t publish -b <board> -bt release --stage stable",
            product_reference,
        )
        self.assertNotIn(" -sc ", product_reference)
        self.assertIn("conan upload '<exact-ref>'", publish_skill)
        self.assertIn(
            "bingo build -t publish -b <board> -bt release --stage stable",
            publish_skill,
        )

    def test_explicit_bingo_build_and_development_are_separate(self) -> None:
        for request, owner in (("run bingo build", "openubmc-bingo-build"), ("开发 bingo 构建工具", "openubmc-bingo-development")):
            receipt = route_receipt(request)
            self.assertEqual(receipt["owner"], owner)
            self.assertEqual(receipt["mode"], "handoff")
            self.assertTrue(receipt["ready"])

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
