from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

try:
    from .support import (
        REQUIREMENT_ROLES,
        create_product_attempt,
        create_verified_product,
        write_resolved_lock,
    )
except ImportError:
    from support import (
        REQUIREMENT_ROLES,
        create_product_attempt,
        create_verified_product,
        write_resolved_lock,
    )


BUILD_ROOT = Path(__file__).resolve().parents[1]


def run(
    *args: str,
    cwd: Path | None = None,
    umask: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        preexec_fn=(lambda: os.umask(umask)) if umask is not None else None,
    )


def init_repo(path: Path) -> None:
    run("git", "init", "-q", str(path))
    run("git", "-C", str(path), "config", "user.email", "test@example.com")
    run("git", "-C", str(path), "config", "user.name", "Build Skill Test")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    run("git", "-C", str(path), "add", "tracked.txt")
    committed = run("git", "-C", str(path), "commit", "-qm", "baseline")
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr)


class BuildPlanBehaviorTests(unittest.TestCase):
    def test_plan_preserves_exact_argv_and_reuses_supplied_checkout(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "component"
            repo.mkdir()
            init_repo(repo)
            plan_path = root / "plan.json"
            before = run("git", "-C", str(repo), "worktree", "list", "--porcelain")
            argv = [
                sys.executable,
                "-c",
                "print('local validation')",
                "literal;$(not-a-shell)",
            ]

            result = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--",
                *argv,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["command"]["argv"], argv)
            self.assertEqual(plan["command"]["cwd"], str(repo.resolve()))
            self.assertEqual(
                plan["workspaces"]["component"]["root"],
                str(repo.resolve()),
            )
            self.assertTrue(plan["workspaces"]["component"]["git_dir"])
            after = run("git", "-C", str(repo), "worktree", "list", "--porcelain")
            self.assertEqual(after.stdout, before.stdout)

    def test_manifest_product_command_cannot_use_validate_mode(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            plan_path = root / "plan.json"

            result = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"manifest={manifest}",
                "--cwd",
                str(manifest),
                "--output",
                str(plan_path),
                "--",
                "bmcgo",
                "build",
                "-b",
                "openUBMC",
                "-bt",
                "release",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "product_command_requires_product_artifact",
                result.stderr,
            )
            self.assertFalse(plan_path.exists())

    def test_product_plan_rejects_community_lock_mismatch(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            build_dir = manifest / "build"
            build_dir.mkdir()
            (build_dir / "openubmc.lock").write_text(
                '{"requires": []}\n',
                encoding="utf-8",
            )
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)
            plan_path = root / "plan.json"
            artifact_path = root / "rootfs_openUBMC.hpm"
            rootfs_image = root / "rootfs.ext4"
            actual_lock = root / "package.lock"
            command = [sys.executable, "-c", "print('planned')"]

            mismatch = run(
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"manifest={manifest}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(artifact_path),
                "--rootfs-image",
                str(rootfs_image),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--resolved-lock-path",
                str(actual_lock),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/apps",
                "--cwd",
                str(manifest),
                "--output",
                str(plan_path),
                "--",
                *command,
            )

            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("community_lock_mismatch", mismatch.stderr)
            self.assertIn("build/ibmc.lock", mismatch.stderr.replace("\\", "/"))
            self.assertFalse(plan_path.exists())

            expected_lock = build_dir / "ibmc.lock"
            write_resolved_lock(expected_lock)
            accepted = run(
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"manifest={manifest}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(artifact_path),
                "--rootfs-image",
                str(rootfs_image),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--resolved-lock-path",
                str(actual_lock),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/apps",
                "--cwd",
                str(manifest),
                "--output",
                str(plan_path),
                "--",
                *command,
            )

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["mode"], "product-artifact")
            self.assertEqual(plan["command"]["argv"], command)
            self.assertEqual(
                plan["locks"]["product"]["path"],
                str(expected_lock.resolve()),
            )
            self.assertEqual(
                plan["expectations"]["rootfs_access"]["root"],
                str(rootfs_image.resolve()),
            )
            self.assertEqual(
                plan["expectations"]["rootfs_access"]["services"][0]["paths"],
                ["/opt/bmc/apps", "/opt/bmc/drivers"],
            )

    def test_product_plan_defaults_to_safe_umask_under_restrictive_caller(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            build_dir = manifest / "build"
            build_dir.mkdir()
            write_resolved_lock(build_dir / "ibmc.lock")
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)
            plan_path = root / "plan.json"

            result = run(
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"manifest={manifest}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(root / "rootfs_openUBMC.hpm"),
                "--rootfs-image",
                str(root / "rootfs.ext4"),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--resolved-lock-path",
                str(root / "package.lock"),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/apps",
                "--cwd",
                str(manifest),
                "--output",
                str(plan_path),
                "--",
                sys.executable,
                "-c",
                "print('planned')",
                umask=0o077,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["environment"]["umask"], "022")

    def test_product_rootfs_service_paths_extend_mandatory_defaults(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            (manifest / "build").mkdir()
            write_resolved_lock(manifest / "build" / "ibmc.lock")
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)
            plan_path = root / "plan.json"

            result = run(
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"manifest={manifest}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(root / "rootfs_openUBMC.hpm"),
                "--rootfs-image",
                str(root / "rootfs.ext4"),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--resolved-lock-path",
                str(root / "package.lock"),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/custom,/opt/bmc/apps",
                "--cwd",
                str(manifest),
                "--output",
                str(plan_path),
                "--",
                sys.executable,
                "-c",
                "print('planned')",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(
                plan["expectations"]["rootfs_access"]["services"][0]["paths"],
                [
                    "/opt/bmc/apps",
                    "/opt/bmc/custom",
                    "/opt/bmc/drivers",
                ],
            )

    def test_plan_path_is_immutable_and_same_plan_creation_is_idempotent(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            plan_path = root / "plan.json"

            def create(message: str) -> subprocess.CompletedProcess[str]:
                return run(
                    sys.executable,
                    str(creator),
                    "--mode",
                    "validate",
                    "--workspace",
                    f"component={repo}",
                    "--cwd",
                    str(repo),
                    "--output",
                    str(plan_path),
                    "--",
                    sys.executable,
                    "-c",
                    f"print({message!r})",
                )

            first = create("first")
            self.assertEqual(first.returncode, 0, first.stderr)
            original = plan_path.read_bytes()
            retry = create("first")
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertTrue(json.loads(retry.stdout)["reused"])
            self.assertEqual(plan_path.read_bytes(), original)

            conflicting = create("second")
            self.assertNotEqual(conflicting.returncode, 0)
            self.assertIn("plan_path_conflict", conflicting.stderr)
            self.assertEqual(plan_path.read_bytes(), original)

    def test_plan_evidence_and_cwd_must_stay_with_declared_boundaries(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)

            evidence_inside = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(repo / ".runs" / "plan.json"),
                "--",
                sys.executable,
                "-c",
                "print('x')",
            )
            self.assertNotEqual(evidence_inside.returncode, 0)
            self.assertIn("evidence_inside_workspace", evidence_inside.stderr)

            cwd_outside = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(root),
                "--output",
                str(root / "plan.json"),
                "--",
                sys.executable,
                "-c",
                "print('x')",
            )
            self.assertNotEqual(cwd_outside.returncode, 0)
            self.assertIn("cwd_outside_workspace", cwd_outside.stderr)

    def test_product_plan_requires_the_manifest_checkout_binding(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            decoy = root / "decoy"
            manifest.mkdir()
            decoy.mkdir()
            init_repo(manifest)
            init_repo(decoy)
            (manifest / "build").mkdir()
            write_resolved_lock(manifest / "build" / "ibmc.lock")
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)

            result = run(
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"decoy={decoy}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(root / "rootfs_openUBMC.hpm"),
                "--rootfs-image",
                str(root / "rootfs.ext4"),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--resolved-lock-path",
                str(root / "package.lock"),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/apps",
                "--cwd",
                str(decoy),
                "--output",
                str(root / "plan.json"),
                "--",
                sys.executable,
                "-c",
                "print('must not run')",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing_manifest_workspace", result.stderr)
            self.assertNotIn("unknown workspace", result.stderr)

    def test_product_output_paths_must_be_absolute(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            write_resolved_lock(manifest / "build/ibmc.lock")
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)

            cases = (
                ("artifact", "--artifact-path", "artifact.hpm", "artifact_path_not_absolute"),
                ("rootfs", "--rootfs-image", "rootfs.ext4", "rootfs_image_not_absolute"),
                (
                    "resolved-lock",
                    "--resolved-lock-path",
                    "package.lock",
                    "resolved_lock_path_not_absolute",
                ),
            )
            for name, flag, relative_value, error_code in cases:
                with self.subTest(path=name):
                    values = {
                        "--artifact-path": str(root / f"{name}.hpm"),
                        "--rootfs-image": str(root / f"{name}.ext4"),
                        "--resolved-lock-path": str(root / f"{name}.lock"),
                    }
                    values[flag] = relative_value
                    result = run(
                        sys.executable,
                        str(creator),
                        "--mode",
                        "product-artifact",
                        "--workspace",
                        f"manifest={manifest}",
                        "--manifest-root",
                        str(manifest),
                        "--community",
                        "ibmc",
                        "--artifact-path",
                        values["--artifact-path"],
                        "--rootfs-image",
                        values["--rootfs-image"],
                        "--product-version",
                        "12.00.05.03",
                        "--baseline-resolved-lock",
                        str(baseline_lock),
                        "--resolved-lock-path",
                        values["--resolved-lock-path"],
                        "--rootfs-service",
                        "secbox=1000:1000=/opt/bmc/apps",
                        "--cwd",
                        str(manifest),
                        "--output",
                        str(root / f"{name}-plan.json"),
                        "--",
                        sys.executable,
                        "-c",
                        "print('must not run')",
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error_code, result.stderr)

    def test_product_metadata_path_cannot_collide_with_outputs_or_evidence(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            write_resolved_lock(manifest / "build/ibmc.lock")
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)
            artifact = root / "firmware.hpm"
            metadata = Path(f"{artifact}.metadata.json")

            common = (
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"manifest={manifest}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(artifact),
                "--rootfs-image",
                str(root / "rootfs.ext4"),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/apps",
                "--cwd",
                str(manifest),
            )

            output_collision = run(
                *common,
                "--resolved-lock-path",
                str(metadata),
                "--output",
                str(root / "collision-plan.json"),
                "--",
                sys.executable,
                "-c",
                "print('must not run')",
            )
            self.assertNotEqual(output_collision.returncode, 0)
            self.assertIn("product_output_path_collision", output_collision.stderr)

            evidence_collision = run(
                *common,
                "--resolved-lock-path",
                str(root / "package.lock"),
                "--output",
                str(metadata),
                "--",
                sys.executable,
                "-c",
                "print('must not run')",
            )
            self.assertNotEqual(evidence_collision.returncode, 0)
            self.assertIn(
                "product_output_evidence_collision",
                evidence_collision.stderr,
            )

            input_collision = run(
                *common,
                "--artifact-path",
                str(baseline_lock),
                "--resolved-lock-path",
                str(root / "package.lock"),
                "--output",
                str(root / "input-collision-plan.json"),
                "--",
                sys.executable,
                "-c",
                "print('must not run')",
            )
            self.assertNotEqual(input_collision.returncode, 0)
            self.assertIn("product_output_input_collision", input_collision.stderr)

    def test_staged_rootfs_flags_report_the_retired_contract(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            write_resolved_lock(manifest / "build/ibmc.lock")
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)

            legacy_flags = (
                ("--rootfs", str(root / "staged-rootfs")),
                ("--rootfs-identity", "secbox=1000:1000"),
                ("--rootfs-path", "/opt/bmc/custom"),
                ("--product-version-file", str(root / "version.json")),
            )
            for index, (legacy_flag, legacy_value) in enumerate(legacy_flags):
                with self.subTest(flag=legacy_flag):
                    result = run(
                        sys.executable,
                        str(creator),
                        "--mode",
                        "product-artifact",
                        "--workspace",
                        f"manifest={manifest}",
                        "--manifest-root",
                        str(manifest),
                        "--community",
                        "ibmc",
                        "--artifact-path",
                        str(root / f"artifact-{index}.hpm"),
                        "--rootfs-image",
                        str(root / f"rootfs-{index}.ext4"),
                        "--product-version",
                        "12.00.05.03",
                        "--baseline-resolved-lock",
                        str(baseline_lock),
                        "--resolved-lock-path",
                        str(root / f"package-{index}.lock"),
                        "--rootfs-service",
                        "secbox=1000:1000=/opt/bmc/apps",
                        legacy_flag,
                        legacy_value,
                        "--cwd",
                        str(manifest),
                        "--output",
                        str(root / f"legacy-{index}-plan.json"),
                        "--",
                        sys.executable,
                        "-c",
                        "print('must not run')",
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("staged_rootfs_evidence_retired", result.stderr)


class PlannedVersionBehaviorTests(unittest.TestCase):
    def test_component_version_is_compare_and_set_and_retry_is_noop(self) -> None:
        setter = BUILD_ROOT / "scripts" / "ensure_planned_version.py"
        with tempfile.TemporaryDirectory() as raw:
            service_json = Path(raw) / "service.json"
            service_json.write_text(
                '{\n  "name": "storage",\n  "version": "1.100.74"\n}\n',
                encoding="utf-8",
            )

            first = run(
                sys.executable,
                str(setter),
                "--kind",
                "component",
                "--path",
                str(service_json),
                "--expected-current",
                "1.100.74",
                "--target",
                "1.100.75",
                "--write",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(json.loads(first.stdout)["changed"])
            after_first = service_json.read_bytes()

            retry = run(
                sys.executable,
                str(setter),
                "--kind",
                "component",
                "--path",
                str(service_json),
                "--expected-current",
                "1.100.74",
                "--target",
                "1.100.75",
                "--write",
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertFalse(json.loads(retry.stdout)["changed"])
            self.assertEqual(service_json.read_bytes(), after_first)

            service_json.write_text(
                '{\n  "name": "storage",\n  "version": "1.100.99"\n}\n',
                encoding="utf-8",
            )
            before_conflict = service_json.read_bytes()
            conflict = run(
                sys.executable,
                str(setter),
                "--kind",
                "component",
                "--path",
                str(service_json),
                "--expected-current",
                "1.100.74",
                "--target",
                "1.100.75",
                "--write",
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("version_conflict", conflict.stderr)
            self.assertEqual(service_json.read_bytes(), before_conflict)

    def test_component_version_update_targets_only_the_top_level_field(self) -> None:
        setter = BUILD_ROOT / "scripts" / "ensure_planned_version.py"
        with tempfile.TemporaryDirectory() as raw:
            service_json = Path(raw) / "service.json"
            service_json.write_text(
                "{\n"
                '  "dependency": {"version": "1.100.74"},\n'
                '  "version": "1.100.74"\n'
                "}\n",
                encoding="utf-8",
            )

            result = run(
                sys.executable,
                str(setter),
                "--kind",
                "component",
                "--path",
                str(service_json),
                "--expected-current",
                "1.100.74",
                "--target",
                "1.100.75",
                "--write",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(service_json.read_text(encoding="utf-8"))
            self.assertEqual(document["dependency"]["version"], "1.100.74")
            self.assertEqual(document["version"], "1.100.75")

    def test_product_base_version_is_compare_and_set_and_retry_is_noop(self) -> None:
        setter = BUILD_ROOT / "scripts" / "ensure_planned_version.py"
        with tempfile.TemporaryDirectory() as raw:
            product_yaml = Path(raw) / "product.yml"
            product_yaml.write_text(
                "product: openUBMC\n"
                "base:\n"
                "  version: '12.00.05.02'  # release identity\n"
                "  community: ibmc\n",
                encoding="utf-8",
            )

            first = run(
                sys.executable,
                str(setter),
                "--kind",
                "product",
                "--path",
                str(product_yaml),
                "--expected-current",
                "12.00.05.02",
                "--target",
                "12.00.05.03",
                "--write",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(json.loads(first.stdout)["changed"])
            after_first = product_yaml.read_bytes()
            self.assertIn(b"version: '12.00.05.03'  # release identity", after_first)

            retry = run(
                sys.executable,
                str(setter),
                "--kind",
                "product",
                "--path",
                str(product_yaml),
                "--expected-current",
                "12.00.05.02",
                "--target",
                "12.00.05.03",
                "--write",
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertFalse(json.loads(retry.stdout)["changed"])
            self.assertEqual(product_yaml.read_bytes(), after_first)

            product_yaml.write_text(
                "product: openUBMC\n"
                "base:\n"
                "  version: '12.00.05.99'  # release identity\n"
                "  community: ibmc\n",
                encoding="utf-8",
            )
            before_conflict = product_yaml.read_bytes()
            conflict = run(
                sys.executable,
                str(setter),
                "--kind",
                "product",
                "--path",
                str(product_yaml),
                "--expected-current",
                "12.00.05.02",
                "--target",
                "12.00.05.03",
                "--write",
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("version_conflict", conflict.stderr)
            self.assertEqual(product_yaml.read_bytes(), before_conflict)


class ManifestReferenceBehaviorTests(unittest.TestCase):
    def test_manifest_ref_is_compare_and_set_and_retry_is_noop(self) -> None:
        updater = BUILD_ROOT / "scripts" / "update_manifest_conan_ref.py"
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw)
            subsys = manifest / "build" / "subsys" / "stable"
            subsys.mkdir(parents=True)
            target_file = subsys / "storage.yml"
            old_ref = "storage/1.100.74@openubmc/stable"
            new_ref = "storage/1.100.75@openubmc/stable"
            target_file.write_text(
                f'dependencies:\n  - conan: "{old_ref}"\n',
                encoding="utf-8",
            )

            first = run(
                sys.executable,
                str(updater),
                "--manifest-root",
                str(manifest),
                "--component",
                "storage",
                "--expected-old-ref",
                old_ref,
                "--new-ref",
                new_ref,
                "--exact-file",
                str(target_file),
                "--write",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_result = json.loads(first.stdout)
            self.assertTrue(first_result["changed"])
            after_first = target_file.read_bytes()

            retry = run(
                sys.executable,
                str(updater),
                "--manifest-root",
                str(manifest),
                "--component",
                "storage",
                "--expected-old-ref",
                old_ref,
                "--new-ref",
                new_ref,
                "--exact-file",
                str(target_file),
                "--write",
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertFalse(json.loads(retry.stdout)["changed"])
            self.assertEqual(target_file.read_bytes(), after_first)

            target_file.write_text(
                'dependencies:\n  - conan: "storage/1.100.99@openubmc/stable"\n',
                encoding="utf-8",
            )
            before_conflict = target_file.read_bytes()
            conflict = run(
                sys.executable,
                str(updater),
                "--manifest-root",
                str(manifest),
                "--component",
                "storage",
                "--expected-old-ref",
                old_ref,
                "--new-ref",
                new_ref,
                "--exact-file",
                str(target_file),
                "--write",
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("manifest_ref_conflict", conflict.stderr)
            self.assertEqual(target_file.read_bytes(), before_conflict)


class BuildAttemptBehaviorTests(unittest.TestCase):
    def test_retry_reuses_the_same_dirty_checkout_without_adding_worktrees(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "tracked.txt").write_text("stable local edit\n", encoding="utf-8")
            plan_path = root / "plan.json"
            run_root = root / "runs"
            before = run("git", "-C", str(repo), "worktree", "list", "--porcelain")
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                "-c",
                "print('same checkout')",
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            attempts = [
                run(
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                )
                for _index in range(2)
            ]

            self.assertTrue(
                all(attempt.returncode == 0 for attempt in attempts),
                "\n".join(attempt.stderr for attempt in attempts),
            )
            attempt_ids = {
                json.loads(attempt.stdout)["attempt_id"] for attempt in attempts
            }
            self.assertEqual(len(attempt_ids), 2)
            after = run("git", "-C", str(repo), "worktree", "list", "--porcelain")
            self.assertEqual(after.stdout, before.stdout)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(
                plan["workspaces"]["component"]["root"],
                str(repo.resolve()),
            )

    def test_attempt_executes_plan_argv_without_shell_reconstruction(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            recorder = root / "record_argv.py"
            captured = root / "captured.json"
            recorder.write_text(
                "import json, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text("
                "json.dumps(sys.argv[2:]), encoding='utf-8')\n",
                encoding="utf-8",
            )
            expected_args = ["two words", "$HOME", "literal;semicolon"]
            plan_path = root / "plan.json"
            plan_result = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
                "--",
                sys.executable,
                str(recorder),
                str(captured),
                *expected_args,
            )
            self.assertEqual(plan_result.returncode, 0, plan_result.stderr)

            attempt = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
            )

            self.assertEqual(attempt.returncode, 0, attempt.stderr)
            self.assertEqual(
                json.loads(captured.read_text(encoding="utf-8")),
                expected_args,
            )
            result = json.loads(attempt.stdout)
            state = json.loads(
                Path(result["state_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "succeeded")
            self.assertEqual(state["rc"], 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(state["plan_id"], plan["plan_id"])

    def test_attempt_rejects_failure_log_even_when_process_exits_zero(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            plan_path = root / "plan.json"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
                "--",
                sys.executable,
                "-c",
                "print('ERROR build task failed')",
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            attempt = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
            )

            self.assertNotEqual(attempt.returncode, 0)
            result = json.loads(attempt.stdout)
            state = json.loads(
                Path(result["state_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["process_rc"], 0)
            self.assertEqual(state["rc"], 1)
            self.assertTrue(state["failure_log_lines"])

    def test_attempt_rejects_untracked_content_drift_in_bound_checkout(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            scratch = repo / "scratch.txt"
            scratch.write_text("planned\n", encoding="utf-8")
            plan_path = root / "plan.json"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
                "--",
                sys.executable,
                "-c",
                "print('should not run')",
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            scratch.write_text("drifted\n", encoding="utf-8")

            attempt = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
            )

            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn("workspace_drift", attempt.stderr)

    def test_attempt_fails_when_command_changes_the_bound_git_head(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                "git",
                "commit",
                "--allow-empty",
                "-m",
                "post-plan-head",
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            attempt = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(run_root),
            )

            self.assertNotEqual(attempt.returncode, 0)
            result = json.loads(attempt.stdout)
            state = json.loads(
                Path(result["state_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["workspace_contamination"], ["component"])
            self.assertIn(
                "git_head",
                state["workspace_drift"]["component"],
            )

    def test_attempt_fails_when_command_changes_a_frozen_input_lock(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest"
            manifest.mkdir()
            init_repo(manifest)
            (manifest / ".gitignore").write_text(
                "build/*.lock\n",
                encoding="utf-8",
            )
            run("git", "-C", str(manifest), "add", ".gitignore")
            committed = run(
                "git",
                "-C",
                str(manifest),
                "commit",
                "-qm",
                "ignore generated locks",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            (manifest / "build").mkdir()
            product_lock = manifest / "build" / "ibmc.lock"
            write_resolved_lock(product_lock)
            baseline_lock = root / "baseline.lock"
            write_resolved_lock(baseline_lock)
            modifier = root / "modify-lock.py"
            modifier.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text("
                "'{\\\"requires\\\": [\\\"changed/1.0\\\"]}\\n', "
                "encoding='utf-8')\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "product-artifact",
                "--workspace",
                f"manifest={manifest}",
                "--manifest-root",
                str(manifest),
                "--community",
                "ibmc",
                "--artifact-path",
                str(root / "rootfs_openUBMC.hpm"),
                "--rootfs-image",
                str(root / "rootfs.ext4"),
                "--product-version",
                "12.00.05.03",
                "--baseline-resolved-lock",
                str(baseline_lock),
                "--resolved-lock-path",
                str(root / "package.lock"),
                "--rootfs-service",
                "secbox=1000:1000=/opt/bmc/apps",
                "--cwd",
                str(manifest),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(modifier),
                str(product_lock),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            attempt = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(run_root),
            )

            self.assertNotEqual(attempt.returncode, 0)
            result = json.loads(attempt.stdout)
            state = json.loads(
                Path(result["state_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "failed")
            self.assertIn("product", state["input_lock_drift"])
            self.assertIn("sha256", state["input_lock_drift"]["product"])

    def test_spawn_failure_is_recorded_as_a_terminal_attempt(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            broken = root / "broken-command"
            broken.write_text("#!/definitely/missing/interpreter\n", encoding="utf-8")
            broken.chmod(0o755)
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                str(broken),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            attempt = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(run_root),
            )

            self.assertNotEqual(attempt.returncode, 0)
            states = list(run_root.glob("plans/*/attempts/*/state.json"))
            self.assertEqual(len(states), 1)
            state = json.loads(states[0].read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["rc"], 1)
            self.assertIn("FileNotFoundError", state["runner_error"])

    def test_different_plans_cannot_build_the_same_checkout_concurrently(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            sleeper = root / "sleeper.py"
            sleeper.write_text(
                "import fcntl, pathlib, sys, time\n"
                "for descriptor in range(3, 256):\n"
                "    try:\n"
                "        fcntl.flock(descriptor, fcntl.LOCK_UN)\n"
                "    except OSError:\n"
                "        pass\n"
                "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
                "time.sleep(1.2)\n",
                encoding="utf-8",
            )
            run_root = root / "runs"
            plans: list[Path] = []
            markers = [root / "first.started", root / "second.started"]
            for index, marker in enumerate(markers, start=1):
                plan_path = root / f"plan-{index}.json"
                planned = run(
                    sys.executable,
                    str(creator),
                    "--mode",
                    "validate",
                    "--workspace",
                    f"component={repo}",
                    "--cwd",
                    str(repo),
                    "--output",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                    "--",
                    sys.executable,
                    str(sleeper),
                    str(marker),
                )
                self.assertEqual(planned.returncode, 0, planned.stderr)
                plans.append(plan_path)

            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plans[0]),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not markers[0].exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(markers[0].exists())

            second = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plans[1]),
                "--run-root",
                str(run_root),
            )
            first_stdout, first_stderr = first.communicate(timeout=5)

            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertTrue(first_stdout)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("workspace_or_plan_already_running", second.stderr)
            self.assertFalse(markers[1].exists())

    def test_different_checkouts_cannot_write_the_same_artifact_concurrently(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sleeper = root / "artifact-sleeper.py"
            sleeper.write_text(
                "import pathlib, sys, time\n"
                "marker, artifact = map(pathlib.Path, sys.argv[1:3])\n"
                "marker.write_text('started', encoding='utf-8')\n"
                "time.sleep(1.2)\n"
                "artifact.write_bytes(b'firmware')\n",
                encoding="utf-8",
            )
            artifact = root / "shared-rootfs.hpm"
            plans: list[Path] = []
            run_roots: list[Path] = []
            markers = [root / "first.started", root / "second.started"]
            for index, marker in enumerate(markers, start=1):
                manifest = root / f"manifest-{index}"
                manifest.mkdir()
                init_repo(manifest)
                (manifest / "build").mkdir()
                write_resolved_lock(manifest / "build" / "ibmc.lock")
                baseline_lock = root / f"baseline-{index}.lock"
                write_resolved_lock(baseline_lock)
                plan_path = root / f"plan-{index}.json"
                run_root = root / f"runs-{index}"
                planned = run(
                    sys.executable,
                    str(creator),
                    "--mode",
                    "product-artifact",
                    "--workspace",
                    f"manifest={manifest}",
                    "--manifest-root",
                    str(manifest),
                    "--community",
                    "ibmc",
                    "--artifact-path",
                    str(artifact),
                    "--rootfs-image",
                    str(root / f"rootfs-{index}.ext4"),
                    "--product-version",
                    "12.00.05.03",
                    "--baseline-resolved-lock",
                    str(baseline_lock),
                    "--resolved-lock-path",
                    str(root / f"package-{index}.lock"),
                    "--rootfs-service",
                    "secbox=1000:1000=/opt/bmc/apps",
                    "--cwd",
                    str(manifest),
                    "--output",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                    "--",
                    sys.executable,
                    str(sleeper),
                    str(marker),
                    str(artifact),
                )
                self.assertEqual(planned.returncode, 0, planned.stderr)
                plans.append(plan_path)
                run_roots.append(run_root)

            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plans[0]),
                    "--run-root",
                    str(run_roots[0]),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not markers[0].exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(markers[0].exists())

            second = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plans[1]),
                "--run-root",
                str(run_roots[1]),
            )
            first_stdout, first_stderr = first.communicate(timeout=5)

            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertTrue(first_stdout)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("output_resource_already_running", second.stderr)
            self.assertFalse(markers[1].exists())

    def test_signal_terminates_the_attempt_process_group(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            ready = root / "ready"
            sentinel = root / "grandchild-survived"
            spawner = root / "spawner.py"
            spawner.write_text(
                "import pathlib, subprocess, sys, time\n"
                "ready, sentinel = map(pathlib.Path, sys.argv[1:3])\n"
                "subprocess.Popen([sys.executable, '-c', "
                "'import pathlib,sys,time; time.sleep(0.8); '"
                "'pathlib.Path(sys.argv[1]).write_text(\\\"survived\\\", encoding=\\\"utf-8\\\")', "
                "str(sentinel)])\n"
                "ready.write_text('ready', encoding='utf-8')\n"
                "time.sleep(10)\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(spawner),
                str(ready),
                str(sentinel),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            attempt = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            attempt.send_signal(signal.SIGTERM)
            stdout, stderr = attempt.communicate(timeout=10)

            self.assertEqual(attempt.returncode, 143, stderr)
            result = json.loads(stdout)
            state = json.loads(
                Path(result["state_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["rc"], 143)
            time.sleep(1)
            self.assertFalse(sentinel.exists())

    def test_runner_sigkill_does_not_release_locks_while_child_is_alive(self) -> None:
        creator = BUILD_ROOT / "scripts" / "create_build_plan.py"
        runner = BUILD_ROOT / "scripts" / "run_build_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            pids = root / "pids"
            sleeper = root / "locked-sleeper.py"
            sleeper.write_text(
                "import os, pathlib, signal, sys, time\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "for descriptor in range(3, 256):\n"
                "    try:\n"
                "        os.close(descriptor)\n"
                "    except OSError:\n"
                "        pass\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "with path.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(f'{os.getpid()}\\n')\n"
                "    handle.flush()\n"
                "time.sleep(1.2)\n",
                encoding="utf-8",
            )
            plan_path = root / "plan.json"
            run_root = root / "runs"
            planned = run(
                sys.executable,
                str(creator),
                "--mode",
                "validate",
                "--workspace",
                f"component={repo}",
                "--cwd",
                str(repo),
                "--output",
                str(plan_path),
                "--run-root",
                str(run_root),
                "--",
                sys.executable,
                str(sleeper),
                str(pids),
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)

            first = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--plan",
                    str(plan_path),
                    "--run-root",
                    str(run_root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not pids.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pids.exists())
            first.kill()
            first.wait(timeout=3)
            if first.stdout is not None:
                first.stdout.close()
            if first.stderr is not None:
                first.stderr.close()

            retry = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(run_root),
            )
            recorded_pids = [
                int(line)
                for line in pids.read_text(encoding="utf-8").splitlines()
            ]
            states = list(run_root.glob("plans/*/attempts/*/state.json"))
            self.assertEqual(len(states), 1)
            recovery_deadline = time.monotonic() + 12
            recovered_state: dict[str, object] = {}
            while time.monotonic() < recovery_deadline:
                recovered_state = json.loads(
                    states[0].read_text(encoding="utf-8")
                )
                try:
                    os.kill(recorded_pids[0], 0)
                    child_alive = True
                except ProcessLookupError:
                    child_alive = False
                if (
                    recovered_state.get("status") == "interrupted"
                    and not child_alive
                ):
                    break
                time.sleep(0.02)

            self.assertNotEqual(retry.returncode, 0)
            self.assertIn("workspace_or_plan_already_running", retry.stderr)
            self.assertEqual(len(recorded_pids), 1)
            self.assertEqual(recovered_state["status"], "interrupted")
            self.assertEqual(
                recovered_state["runner_error"],
                "runner_connection_lost_guardian_recovered",
            )
            second = run(
                sys.executable,
                str(runner),
                "--plan",
                str(plan_path),
                "--run-root",
                str(run_root),
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                len(pids.read_text(encoding="utf-8").splitlines()),
                2,
            )


class ArtifactVerificationBehaviorTests(unittest.TestCase):
    def test_verification_requires_successful_attempt_and_passing_gates(self) -> None:
        finalizer = BUILD_ROOT / "scripts" / "finalize_product_attempt.py"
        verifier = BUILD_ROOT / "scripts" / "verify_product_artifact.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = create_verified_product(root, BUILD_ROOT)
            finalization = json.loads(
                fixture["finalization"].read_text(encoding="utf-8")
            )
            verification = json.loads(
                fixture["verification"].read_text(encoding="utf-8")
            )
            self.assertEqual(finalization["status"], "accepted_local_only")
            self.assertEqual(finalization["verification_status"], "accepted")
            self.assertEqual(verification["status"], "accepted")
            self.assertEqual(
                verification["artifact"]["observed_version"],
                "12.00.05.03",
            )
            self.assertEqual(
                verification["package_binding"],
                "package_binding_unverified",
            )
            self.assertIs(verification["upgrade_eligible"], False)
            rootfs_report = json.loads(
                fixture["permission_report"].read_text(encoding="utf-8")
            )
            self.assertEqual(
                rootfs_report["inputs"][0]["path"],
                str(fixture["rootfs_image"]),
            )

            rejected_root = root / "rejected"
            rejected_root.mkdir()
            rejected_fixture = create_product_attempt(
                rejected_root,
                BUILD_ROOT,
                image_inode_settings={
                    "/opt/bmc/apps": (0, 0, 0o700),
                },
            )
            rejected = run(
                sys.executable,
                str(finalizer),
                "--plan",
                str(rejected_fixture["plan"]),
                "--attempt-state",
                str(rejected_fixture["state"]),
            )
            self.assertEqual(rejected.returncode, 1, rejected.stderr)
            rejected_result = json.loads(rejected.stdout)
            rejected_verification = json.loads(
                Path(rejected_result["verification_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(rejected_result["status"], "rejected")
            self.assertEqual(rejected_verification["status"], "rejected")
            self.assertIn(
                "rootfs-access",
                rejected_verification["failed_checks"],
            )
            self.assertNotIn("metadata_path", rejected_result)
            self.assertFalse(
                Path(f"{rejected_fixture['artifact']}.metadata.json").exists()
            )

            standalone_path = root / "standalone-verification.json"
            standalone = run(
                sys.executable,
                str(verifier),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--artifact",
                str(fixture["artifact"]),
                "--gate-report",
                str(fixture["dependency_report"]),
                "--gate-report",
                str(fixture["permission_report"]),
                "--output",
                str(standalone_path),
            )
            self.assertEqual(standalone.returncode, 2)
            self.assertIn("finalize_product_attempt", standalone.stderr)
            self.assertFalse(standalone_path.exists())


class ArtifactMetadataBehaviorTests(unittest.TestCase):
    def test_metadata_requires_accepted_verification_bound_to_artifact(self) -> None:
        writer = BUILD_ROOT / "scripts" / "write_artifact_metadata.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = create_verified_product(root, BUILD_ROOT)
            artifact = fixture["artifact"]
            verification_path = fixture["verification"]
            verification = json.loads(
                verification_path.read_text(encoding="utf-8")
            )
            metadata = json.loads(
                fixture["metadata"].read_text(encoding="utf-8")
            )
            self.assertEqual(
                metadata["artifact"]["sha256"],
                verification["artifact"]["sha256"],
            )
            self.assertEqual(metadata["artifact"]["size"], len(b"firmware"))
            self.assertEqual(metadata["product_version"], "12.00.05.03")
            self.assertEqual(metadata["build"]["plan_id"], verification["plan_id"])
            self.assertEqual(
                metadata["build"]["attempt_id"],
                verification["attempt_id"],
            )
            self.assertEqual(
                metadata["build"]["finalization_id"],
                verification["finalization_id"],
            )
            self.assertEqual(
                metadata["package_binding"],
                "package_binding_unverified",
            )
            self.assertIs(metadata["upgrade_eligible"], False)

            standalone = run(
                sys.executable,
                str(writer),
                "--artifact-path",
                str(artifact),
                "--verification",
                str(verification_path),
            )
            self.assertEqual(standalone.returncode, 2)
            self.assertIn("finalize_product_attempt", standalone.stderr)

    def test_product_attempt_cannot_be_finalized_twice(self) -> None:
        finalizer = BUILD_ROOT / "scripts" / "finalize_product_attempt.py"
        with tempfile.TemporaryDirectory() as raw:
            fixture = create_verified_product(Path(raw), BUILD_ROOT)
            finalization_before = fixture["finalization"].read_bytes()
            verification_before = fixture["verification"].read_bytes()
            metadata_before = fixture["metadata"].read_bytes()
            finalization = json.loads(finalization_before.decode("utf-8"))

            repeated = run(
                sys.executable,
                str(finalizer),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )

            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("finalization_already_recorded", repeated.stderr)
            self.assertEqual(
                fixture["finalization"].read_bytes(),
                finalization_before,
            )
            self.assertEqual(
                fixture["verification"].read_bytes(),
                verification_before,
            )
            self.assertEqual(fixture["metadata"].read_bytes(), metadata_before)
            self.assertEqual(
                json.loads(
                    fixture["finalization"].read_text(encoding="utf-8")
                )["finalization_id"],
                finalization["finalization_id"],
            )


class DependencyDeltaBehaviorTests(unittest.TestCase):
    def test_dependency_delta_rejects_changes_outside_plan_allowlist(self) -> None:
        checker = BUILD_ROOT / "scripts" / "check_dependency_delta.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan_path = root / "plan.json"
            state_path = root / "state.json"
            baseline_path = root / "baseline.lock"
            actual_path = root / "actual.lock"
            report_path = root / "dependency-report.json"
            plan_id = "a" * 64
            attempt_id = "attempt-1"
            write_resolved_lock(
                baseline_path,
                requires=[
                    "component_drivers/1.2.252@openubmc/stable",
                    "devmon/1.2.63@openubmc/stable",
                    "storage/1.100.74@openubmc/stable",
                ],
            )

            baseline_stat = baseline_path.stat()
            plan_path.write_text(
                json.dumps(
                    {
                        "plan_id": plan_id,
                        "expectations": {
                            "allowed_dependency_changes": ["storage"],
                            "dependency_delta": {
                                "baseline": {
                                    "path": str(baseline_path.resolve()),
                                    "sha256": hashlib.sha256(
                                        baseline_path.read_bytes()
                                    ).hexdigest(),
                                    "size": baseline_stat.st_size,
                                },
                                "actual_path": str(actual_path.resolve()),
                                "roles": list(REQUIREMENT_ROLES),
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            def write_state() -> None:
                actual_stat = actual_path.stat()
                state_path.write_text(
                    json.dumps(
                        {
                            "plan_id": plan_id,
                            "attempt_id": attempt_id,
                            "status": "succeeded",
                            "rc": 0,
                            "outputs_before": {
                                "dependency_lock": {
                                    "path": str(actual_path.resolve()),
                                    "status": "missing",
                                }
                            },
                            "outputs_after": {
                                "dependency_lock": {
                                    "path": str(actual_path.resolve()),
                                    "status": "present",
                                    "sha256": hashlib.sha256(
                                        actual_path.read_bytes()
                                    ).hexdigest(),
                                    "size": actual_stat.st_size,
                                    "device": actual_stat.st_dev,
                                    "inode": actual_stat.st_ino,
                                    "mtime_ns": actual_stat.st_mtime_ns,
                                    "ctime_ns": actual_stat.st_ctime_ns,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )

            write_resolved_lock(
                actual_path,
                requires=[
                    "component_drivers/1.2.295@openubmc/stable",
                    "devmon/1.2.75@openubmc/stable",
                    "storage/1.100.75@openubmc/stable",
                ],
            )
            write_state()

            rejected = run(
                sys.executable,
                str(checker),
                "--plan",
                str(plan_path),
                "--attempt-state",
                str(state_path),
                "--output",
                str(report_path),
            )

            self.assertNotEqual(rejected.returncode, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertEqual(
                report["details"]["unexpected"],
                ["component_drivers", "devmon"],
            )

            write_resolved_lock(
                actual_path,
                requires=[
                    "component_drivers/1.2.252@openubmc/stable",
                    "devmon/1.2.63@openubmc/stable",
                    "storage/1.100.75@openubmc/stable",
                ],
            )
            write_state()
            accepted = run(
                sys.executable,
                str(checker),
                "--plan",
                str(plan_path),
                "--attempt-state",
                str(state_path),
                "--output",
                str(report_path),
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["details"]["changed"], ["storage"])


class RootfsAccessBehaviorTests(unittest.TestCase):
    def test_non_root_identity_must_traverse_shared_service_ancestors(self) -> None:
        checker = BUILD_ROOT / "scripts" / "check_rootfs_access.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rejected_root = root / "rejected"
            rejected_root.mkdir()
            rejected_fixture = create_product_attempt(
                rejected_root,
                BUILD_ROOT,
                image_inode_settings={
                    "/opt/bmc/apps": (0, 0, 0o700),
                },
            )
            report_path = root / "permission-report.json"

            rejected = run(
                sys.executable,
                str(checker),
                "--plan",
                str(rejected_fixture["plan"]),
                "--attempt-state",
                str(rejected_fixture["state"]),
                "--output",
                str(report_path),
            )

            self.assertNotEqual(rejected.returncode, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertEqual(
                report["details"]["blocked"][0]["path"],
                "/opt/bmc/apps",
            )
            self.assertEqual(
                report["inputs"][0]["path"],
                str(rejected_fixture["rootfs_image"]),
            )

            accepted_root = root / "accepted"
            accepted_root.mkdir()
            accepted_fixture = create_product_attempt(
                accepted_root,
                BUILD_ROOT,
            )
            accepted = run(
                sys.executable,
                str(checker),
                "--plan",
                str(accepted_fixture["plan"]),
                "--attempt-state",
                str(accepted_fixture["state"]),
                "--output",
                str(root / "accepted-permission-report.json"),
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            report = json.loads(
                (root / "accepted-permission-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["details"]["blocked"], [])


if __name__ == "__main__":
    unittest.main()
