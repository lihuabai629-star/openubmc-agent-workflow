from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

try:
    from .support import init_repo, run
    from .test_hpm_containment import TEST_KEY, make_hpm
except ImportError:
    from support import init_repo, run
    from test_hpm_containment import TEST_KEY, make_hpm


BUILD_ROOT = Path(__file__).resolve().parents[1]
CREATE_PLAN = BUILD_ROOT / "scripts" / "create_build_plan.py"
RUN_ATTEMPT = BUILD_ROOT / "scripts" / "run_build_attempt.py"
CHECK_DEPENDENCIES = BUILD_ROOT / "scripts" / "check_dependency_delta.py"
CHECK_ROOTFS = BUILD_ROOT / "scripts" / "check_rootfs_access.py"
VERIFY_PRODUCT = BUILD_ROOT / "scripts" / "verify_product_artifact.py"
FINALIZE = BUILD_ROOT / "scripts" / "finalize_product_attempt.py"
REQUIREMENT_ROLES = (
    "requires",
    "build_requires",
    "python_requires",
    "config_requires",
)


def write_lock(path: Path, **roles: list[str]) -> None:
    document = {role: list(roles.get(role, [])) for role in REQUIREMENT_ROLES}
    document["version"] = "0.5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def debugfs(image: Path, command: str, *, write: bool = False) -> None:
    arguments = [shutil.which("debugfs") or "debugfs"]
    if write:
        arguments.append("-w")
    arguments.extend(("-R", command, str(image)))
    completed = subprocess.run(
        arguments,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    errors = [
        line
        for line in completed.stderr.splitlines()
        if line and not line.startswith("debugfs ")
    ]
    if completed.returncode != 0 or errors:
        raise AssertionError(
            f"debugfs failed: rc={completed.returncode} errors={errors}"
        )


def set_image_directory(
    image: Path,
    path: str,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    debugfs(image, f"set_inode_field {path} uid {uid}", write=True)
    debugfs(image, f"set_inode_field {path} gid {gid}", write=True)
    debugfs(image, f"set_inode_field {path} mode 04{mode:04o}", write=True)


def make_ext4(
    root: Path,
    image: Path,
    *,
    version: str = "12.00.05.03",
    directories: tuple[str, ...] = (),
    files: dict[str, str] | None = None,
) -> Path:
    staging = root / f"{image.stem}-staging"
    for relative in (
        "etc",
        "opt/bmc/apps",
        "opt/bmc/drivers",
        *directories,
    ):
        (staging / relative).mkdir(parents=True, exist_ok=True)
    (staging / "etc/version.json").write_text(
        json.dumps({"Version": version}),
        encoding="utf-8",
    )
    default_lua = staging / "opt/bmc/apps/health.lua"
    default_lua.write_text("return true\n", encoding="utf-8")
    for relative, content in (files or {}).items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    image.parent.mkdir(parents=True, exist_ok=True)
    with image.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    completed = subprocess.run(
        [
            shutil.which("mke2fs") or "mke2fs",
            "-q",
            "-t",
            "ext4",
            "-d",
            str(staging),
            "-F",
            str(image),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return staging


def snapshot(path: Path) -> dict[str, object]:
    absolute = path.absolute()
    if not absolute.exists():
        return {"path": str(absolute), "status": "missing"}
    metadata = os.lstat(absolute)
    raw = absolute.read_bytes()
    return {
        "path": str(absolute),
        "status": "present",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


class ProductFinalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("mke2fs") or not shutil.which("debugfs"):
            raise unittest.SkipTest("mke2fs and debugfs are required")

    def create_manifest(self, root: Path) -> tuple[Path, Path]:
        manifest = root / "manifest"
        manifest.mkdir()
        init_repo(manifest)
        product_lock = manifest / "build/ibmc.lock"
        write_lock(
            product_lock,
            requires=["storage/1.0@openubmc/stable"],
        )
        return manifest, product_lock

    def plan_command(
        self,
        *,
        root: Path,
        manifest: Path,
        baseline: Path,
        image: Path,
        artifact: Path,
        actual_lock: Path,
        services: tuple[str, ...],
        producer: Path | None = None,
        producer_args: tuple[str, ...] = (),
        allowed_changes: tuple[str, ...] = (),
        mutable_paths: tuple[str, ...] = (),
        key_path: Path | None = None,
        lua_checker: Path = Path("/bin/true"),
    ) -> list[str]:
        command = [
            sys.executable,
            str(CREATE_PLAN),
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
            str(image),
            "--product-version",
            "12.00.05.03",
            "--baseline-resolved-lock",
            str(baseline),
            "--resolved-lock-path",
            str(actual_lock),
            "--lua-checker",
            str(lua_checker),
        ]
        for service in services:
            command.extend(("--rootfs-service", service))
        for component in allowed_changes:
            command.extend(("--allowed-dependency-change", component))
        for mutable_path in mutable_paths:
            command.extend(("--mutable-path", mutable_path))
        if key_path is not None:
            command.extend(("--hpm-key-file", str(key_path)))
        command.extend(
            (
                "--cwd",
                str(manifest),
                "--output",
                str(root / "plan.json"),
                "--run-root",
                str(root / "runs"),
                "--",
                sys.executable,
                str(producer or (root / "producer.py")),
                *producer_args,
            )
        )
        return command

    def create_attempt(
        self,
        *,
        root: Path,
        baseline_roles: dict[str, list[str]],
        actual_roles: dict[str, list[str]],
        services: tuple[str, ...],
        image_directories: tuple[str, ...] = (),
        image_inode_settings: dict[str, tuple[int, int, int]] | None = None,
        allowed_changes: tuple[str, ...] = (),
        use_runner: bool = False,
        artifact_inside_manifest: bool = False,
        hpm_payload: str | None = None,
        bind_hpm_key: bool = False,
        image_files: dict[str, str] | None = None,
        lua_checker: Path = Path("/bin/true"),
    ) -> dict[str, Path]:
        manifest, _ = self.create_manifest(root)
        baseline = root / "baseline.lock"
        write_lock(baseline, **baseline_roles)
        actual_source = root / "actual-source.lock"
        write_lock(actual_source, **actual_roles)
        template = root / "template.ext4"
        staging = make_ext4(
            root,
            template,
            directories=image_directories,
            files=image_files,
        )
        for path, (uid, gid, mode) in (image_inode_settings or {}).items():
            set_image_directory(
                template,
                path,
                uid=uid,
                gid=gid,
                mode=mode,
            )
        artifact = (
            manifest / "firmware.hpm"
            if artifact_inside_manifest
            else root / "rootfs_openUBMC.hpm"
        )
        image = root / "rootfs_iBMC.img"
        actual_lock = root / "package.lock"
        key_path = root / "private-package-key"
        if bind_hpm_key:
            key_path.write_bytes(TEST_KEY)
            key_path.chmod(0o600)
        artifact_source = root / "artifact-source.hpm"
        if hpm_payload is None:
            artifact_source.write_bytes(b"firmware")
        else:
            rootfs_bytes = template.read_bytes()
            if hpm_payload == "mismatching":
                rootfs_bytes = rootfs_bytes[:-1] + bytes((rootfs_bytes[-1] ^ 1,))
            artifact_source.write_bytes(make_hpm(rootfs_bytes, signed=True))
        producer = root / "producer.py"
        producer.write_text(
            "import pathlib, shutil, sys\n"
            "artifact, image, template, lock, source, package = map(pathlib.Path, sys.argv[1:7])\n"
            "shutil.copyfile(package, artifact)\n"
            "shutil.copyfile(template, image)\n"
            "shutil.copyfile(source, lock)\n"
            "for relative in ('output/attempt-marker', 'temp/attempt-marker'):\n"
            "    marker = pathlib.Path.cwd() / relative\n"
            "    marker.parent.mkdir(parents=True, exist_ok=True)\n"
            "    marker.write_text('generated', encoding='utf-8')\n",
            encoding="utf-8",
        )
        planned = run(
            *self.plan_command(
                root=root,
                manifest=manifest,
                baseline=baseline,
                image=image,
                artifact=artifact,
                actual_lock=actual_lock,
                services=services,
                producer=producer,
                producer_args=(
                    str(artifact),
                    str(image),
                    str(template),
                    str(actual_lock),
                    str(actual_source),
                    str(artifact_source),
                ),
                allowed_changes=allowed_changes,
                mutable_paths=(
                    ("manifest=firmware.hpm",)
                    if artifact_inside_manifest
                    else ()
                ),
                key_path=key_path if bind_hpm_key else None,
                lua_checker=lua_checker,
            )
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan_path = root / "plan.json"
        if use_runner:
            attempted = run(
                sys.executable,
                str(RUN_ATTEMPT),
                "--plan",
                str(plan_path),
                "--run-root",
                str(root / "runs"),
            )
            self.assertEqual(attempted.returncode, 0, attempted.stderr)
            state = Path(json.loads(attempted.stdout)["state_path"])
        else:
            before = {
                "artifact": snapshot(artifact),
                "dependency_lock": snapshot(actual_lock),
                "rootfs_image": snapshot(image),
            }
            produced = run(
                sys.executable,
                str(producer),
                str(artifact),
                str(image),
                str(template),
                str(actual_lock),
                str(actual_source),
                str(artifact_source),
                cwd=manifest,
            )
            self.assertEqual(produced.returncode, 0, produced.stderr)
            plan_bytes = plan_path.read_bytes()
            plan = json.loads(plan_bytes.decode("utf-8"))
            attempt_id = "attempt-product-finalization"
            state = (
                root
                / "runs"
                / "plans"
                / plan["plan_id"]
                / "attempts"
                / attempt_id
                / "state.json"
            )
            state.parent.mkdir(parents=True)
            state.write_text(
                json.dumps(
                    {
                        "schema": "openubmc-build/attempt-v1",
                        "plan_id": plan["plan_id"],
                        "plan_path": str(plan_path.resolve()),
                        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                        "attempt_id": attempt_id,
                        "status": "succeeded",
                        "rc": 0,
                        "process_rc": 0,
                        "workspace_contamination": [],
                        "signal_escalated": False,
                        "input_lock_drift": {},
                        "outputs_before": before,
                        "outputs_after": {
                            "artifact": snapshot(artifact),
                            "dependency_lock": snapshot(actual_lock),
                            "rootfs_image": snapshot(image),
                        },
                    }
                ),
                encoding="utf-8",
            )
        return {
            "manifest": manifest,
            "baseline": baseline,
            "actual_lock": actual_lock,
            "artifact": artifact,
            "image": image,
            "template": template,
            "staging": staging,
            "plan": plan_path,
            "state": state,
            "key": key_path,
        }

    def test_plan_binds_final_ext4_complete_baseline_and_service_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _ = self.create_manifest(root)
            baseline = root / "baseline.lock"
            write_lock(
                baseline,
                requires=["storage/1.0@openubmc/stable"],
                build_requires=["tool/2.0@openubmc/stable"],
                python_requires=["python-tool/3.0@openubmc/stable"],
                config_requires=["config/4.0@openubmc/stable"],
            )
            image = root / "rootfs_iBMC.img"
            artifact = root / "rootfs_openUBMC.hpm"
            actual_lock = root / "package.lock"

            missing_baseline = self.plan_command(
                root=root,
                manifest=manifest,
                baseline=baseline,
                image=image,
                artifact=artifact,
                actual_lock=actual_lock,
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
            )
            baseline_index = missing_baseline.index("--baseline-resolved-lock")
            del missing_baseline[baseline_index : baseline_index + 2]
            rejected = run(*missing_baseline)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("--baseline-resolved-lock", rejected.stderr)

            planned = run(
                *self.plan_command(
                    root=root,
                    manifest=manifest,
                    baseline=baseline,
                    image=image,
                    artifact=artifact,
                    actual_lock=actual_lock,
                    services=(
                        "ssdp=104:104=/opt/bmc/apps/ssdp",
                        "web=200:201:202=/opt/bmc/apps/web,/var/lib/web",
                    ),
                )
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(
                plan["workspaces"]["manifest"]["mutable_paths"],
                ["output", "temp"],
            )
            self.assertEqual(
                plan["locks"]["dependency_baseline"]["path"],
                str(baseline.resolve()),
            )
            access = plan["expectations"]["rootfs_access"]
            self.assertEqual(access["image_path"], str(image.resolve()))
            self.assertNotIn("identities", access)
            self.assertNotIn("paths", access)
            services = {item["name"]: item for item in access["services"]}
            self.assertEqual(set(services), {"ssdp", "web"})
            self.assertEqual(
                services["ssdp"]["paths"],
                [
                    "/opt/bmc/apps",
                    "/opt/bmc/apps/ssdp",
                    "/opt/bmc/drivers",
                ],
            )
            self.assertEqual(services["web"]["supplementary_gids"], [202])
            self.assertIn("/var/lib/web", services["web"]["paths"])
            lua = plan["expectations"]["rootfs_lua"]
            self.assertEqual(lua["image_path"], str(image.resolve()))
            self.assertEqual(lua["roots"], ["/opt/bmc/apps", "/opt/bmc/drivers"])
            self.assertEqual(lua["checker"]["path"], str(Path("/bin/true").resolve()))
            self.assertEqual(lua["checker_argv"], ["-p", "{source}"])
            self.assertEqual(
                plan["expectations"]["required_gates"],
                ["dependency-delta", "rootfs-access", "rootfs-lua-syntax"],
            )
            self.assertEqual(
                plan["expectations"]["outputs"]["rootfs_image"],
                str(image.resolve()),
            )
            image_locks = [
                item
                for item in plan["execution"]["output_resources"]
                if item["path"] == str(image.resolve())
            ]
            self.assertGreaterEqual(len(image_locks), 1)
            self.assertEqual(
                plan["expectations"]["package_binding"],
                {
                    "status": "package_binding_unverified",
                    "upgrade_eligible": False,
                },
            )

            incomplete = root / "incomplete.lock"
            incomplete.write_text(
                json.dumps({"requires": ["storage/1.0@openubmc/stable"]}),
                encoding="utf-8",
            )
            incomplete_command = self.plan_command(
                root=root / "incomplete-run",
                manifest=manifest,
                baseline=incomplete,
                image=root / "other.img",
                artifact=root / "other.hpm",
                actual_lock=root / "other.lock",
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
            )
            rejected = run(*incomplete_command)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("incomplete_resolved_lock", rejected.stderr)

    def test_product_plan_requires_an_executable_lua_checker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _ = self.create_manifest(root)
            baseline = root / "baseline.lock"
            write_lock(baseline)
            command = self.plan_command(
                root=root,
                manifest=manifest,
                baseline=baseline,
                image=root / "rootfs.img",
                artifact=root / "firmware.hpm",
                actual_lock=root / "actual.lock",
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
            )
            checker_index = command.index("--lua-checker")
            del command[checker_index : checker_index + 2]

            missing = run(*command)

            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("missing_product_inputs", missing.stderr)
            self.assertIn("--lua-checker", missing.stderr)

            not_executable = root / "luac"
            not_executable.write_text("checker\n", encoding="utf-8")
            command = self.plan_command(
                root=root / "invalid",
                manifest=manifest,
                baseline=baseline,
                image=root / "other.img",
                artifact=root / "other.hpm",
                actual_lock=root / "other.lock",
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                lua_checker=not_executable,
            )
            invalid = run(*command)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("invalid_lua_checker", invalid.stderr)

    def test_dependency_delta_preserves_requirement_roles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                    "build_requires": ["tool/2.0@openubmc/stable"],
                    "python_requires": ["python-tool/3.0@openubmc/stable"],
                    "config_requires": ["config/4.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": [
                        "storage/1.0@openubmc/stable",
                        "config/4.0@openubmc/stable",
                    ],
                    "build_requires": ["tool/2.0@openubmc/stable"],
                    "python_requires": ["python-tool/3.0@openubmc/stable"],
                    "config_requires": [],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
                allowed_changes=("config",),
            )
            report_path = root / "dependency.json"
            checked = run(
                sys.executable,
                str(CHECK_DEPENDENCIES),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--output",
                str(report_path),
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["details"]["changed"], ["config"])
            role_changes = report["details"]["changes_by_role"]
            self.assertEqual(
                role_changes["requires"]["config"]["before"],
                [],
            )
            self.assertEqual(
                role_changes["requires"]["config"]["after"],
                ["config/4.0@openubmc/stable"],
            )
            self.assertEqual(
                role_changes["config_requires"]["config"]["before"],
                ["config/4.0@openubmc/stable"],
            )
            self.assertEqual(
                role_changes["config_requires"]["config"]["after"],
                [],
            )

    def test_runner_snapshots_final_ext4_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
                use_runner=True,
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            self.assertEqual(
                state["outputs_before"]["rootfs_image"]["status"],
                "missing",
            )
            image = state["outputs_after"]["rootfs_image"]
            self.assertEqual(image["status"], "present")
            for field in (
                "path",
                "sha256",
                "size",
                "device",
                "inode",
                "mtime_ns",
                "ctime_ns",
            ):
                self.assertIn(field, image)

    def test_dependency_delta_rejects_incomplete_actual_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            actual = json.loads(
                fixture["actual_lock"].read_text(encoding="utf-8")
            )
            del actual["config_requires"]
            fixture["actual_lock"].write_text(
                json.dumps(actual),
                encoding="utf-8",
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["outputs_after"]["dependency_lock"] = snapshot(
                fixture["actual_lock"]
            )
            fixture["state"].write_text(json.dumps(state), encoding="utf-8")
            checked = run(
                sys.executable,
                str(CHECK_DEPENDENCIES),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--output",
                str(root / "dependency.json"),
            )
            self.assertEqual(checked.returncode, 2, checked.stderr)
            self.assertIn("incomplete_resolved_lock", checked.stderr)

    def test_dependency_role_move_requires_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "config_requires": ["config/4.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["config/4.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            report_path = root / "dependency.json"
            checked = run(
                sys.executable,
                str(CHECK_DEPENDENCIES),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--output",
                str(report_path),
            )
            self.assertEqual(checked.returncode, 1, checked.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["details"]["unexpected"], ["config"])

    def test_rootfs_access_uses_per_service_mapping_and_image_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=(
                    "alpha=1000:1000=/srv/alpha",
                    "beta=2000:2000=/srv/beta",
                ),
                image_directories=("srv/alpha", "srv/beta"),
                image_inode_settings={
                    "/srv/alpha": (1000, 1000, 0o710),
                    "/srv/beta": (2000, 2000, 0o710),
                },
            )
            (fixture["staging"] / "srv/alpha").chmod(0)
            (fixture["staging"] / "srv/beta").chmod(0)
            report_path = root / "rootfs.json"
            checked = run(
                sys.executable,
                str(CHECK_ROOTFS),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--output",
                str(report_path),
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            pairs = {
                (item["service"], item["path"])
                for item in report["details"]["checked"]
            }
            self.assertIn(("alpha", "/srv/alpha"), pairs)
            self.assertIn(("beta", "/srv/beta"), pairs)
            self.assertNotIn(("alpha", "/srv/beta"), pairs)
            self.assertNotIn(("beta", "/srv/alpha"), pairs)
            self.assertEqual(report["inputs"][0]["path"], str(fixture["image"]))
            self.assertEqual(
                report["inputs"][0]["sha256"],
                snapshot(fixture["image"])["sha256"],
            )
            self.assertEqual(
                report["inputs"][0]["size"],
                snapshot(fixture["image"])["size"],
            )

    def test_rootfs_access_rejects_image_owner_even_when_host_tree_allows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("alpha=1000:1000=/srv/alpha",),
                image_directories=("srv/alpha",),
                image_inode_settings={
                    "/srv/alpha": (0, 0, 0o700),
                },
            )
            (fixture["staging"] / "srv/alpha").chmod(0o755)
            report_path = root / "rootfs.json"
            checked = run(
                sys.executable,
                str(CHECK_ROOTFS),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--output",
                str(report_path),
            )
            self.assertEqual(checked.returncode, 1, checked.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            failure = next(
                item
                for item in report["details"]["blocked"]
                if item["path"] == "/srv/alpha"
            )
            self.assertEqual(failure["blocked_at"], "/srv/alpha")
            self.assertEqual(failure["reason"], "execute_bit_denied")
            self.assertEqual(failure["owner_uid"], 0)
            self.assertEqual(failure["mode"], "0700")

    def test_rootfs_access_includes_the_image_root_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("alpha=1000:1000=/srv/alpha",),
                image_directories=("srv/alpha",),
                image_inode_settings={"/": (0, 0, 0o700)},
            )
            report_path = root / "rootfs.json"
            checked = run(
                sys.executable,
                str(CHECK_ROOTFS),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--output",
                str(report_path),
            )
            self.assertEqual(checked.returncode, 1, checked.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(
                all(item["blocked_at"] == "/" for item in report["details"]["blocked"])
            )

    def test_finalizer_rejects_syntax_invalid_lua_from_the_final_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checker = root / "test-luac"
            checker.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, sys\n"
                "source = pathlib.Path(sys.argv[-1]).read_bytes()\n"
                "if b'function truncated(' in source:\n"
                "    print(f'{sys.argv[-1]}:42: unexpected symbol near eof', file=sys.stderr)\n"
                "    raise SystemExit(1)\n",
                encoding="utf-8",
            )
            checker.chmod(0o755)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
                image_files={
                    "opt/bmc/apps/ssdp/truncated.lua": "function truncated(\n",
                },
                lua_checker=checker,
            )

            finalized = self.finalize(fixture)

            self.assertEqual(finalized.returncode, 1, finalized.stderr)
            result = json.loads(finalized.stdout)
            self.assertEqual(result["status"], "rejected")
            verification = json.loads(
                Path(result["verification_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(verification["failed_checks"], ["rootfs-lua-syntax"])
            report_path = fixture["state"].parent / "reports/rootfs-lua-syntax.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertEqual(
                report["details"]["failures"][0]["path"],
                "/opt/bmc/apps/ssdp/truncated.lua",
            )
            self.assertEqual(report["details"]["failures"][0]["line"], 42)
            rendered_report = json.dumps(report)
            self.assertNotIn("unexpected symbol", rendered_report)
            self.assertNotIn("openubmc-lua-gate-", rendered_report)
            self.assertFalse(Path(str(fixture["artifact"]) + ".metadata.json").exists())

    def test_finalizer_rejects_lua_checker_drift_after_the_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checker = root / "test-luac"
            checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            checker.chmod(0o755)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
                lua_checker=checker,
            )
            checker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

            finalized = self.finalize(fixture)

            self.assertEqual(finalized.returncode, 2)
            self.assertIn("input_lock_drift: lua_checker.sha256", finalized.stderr)
            self.assertFalse((fixture["state"].parent / "finalization.json").exists())

    def test_finalizer_rejects_an_empty_lua_file_even_when_checker_accepts_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
                image_files={"opt/bmc/apps/ssdp/empty.lua": ""},
            )

            finalized = self.finalize(fixture)

            self.assertEqual(finalized.returncode, 1, finalized.stderr)
            result = json.loads(finalized.stdout)
            verification = json.loads(
                Path(result["verification_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(verification["failed_checks"], ["rootfs-lua-syntax"])
            report = json.loads(
                (fixture["state"].parent / "reports/rootfs-lua-syntax.json").read_text(
                    encoding="utf-8"
                )
            )
            failure = next(
                item
                for item in report["details"]["failures"]
                if item.get("path") == "/opt/bmc/apps/ssdp/empty.lua"
            )
            self.assertEqual(failure["reason"], "empty_lua_source")

    def test_finalizer_recomputes_evidence_and_disables_automatic_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            finalized = run(
                sys.executable,
                str(FINALIZE),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )
            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            result = json.loads(finalized.stdout)
            self.assertEqual(result["status"], "accepted_local_only")
            self.assertEqual(result["verification_status"], "accepted")
            self.assertEqual(
                result["package_binding"],
                "package_binding_unverified",
            )
            self.assertIs(result["upgrade_eligible"], False)
            verification = json.loads(
                Path(result["verification_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                verification["artifact"]["observed_version"],
                "12.00.05.03",
            )
            self.assertEqual(
                verification["version_evidence"]["path"],
                str(fixture["image"]),
            )
            self.assertEqual(
                verification["package_binding"],
                "package_binding_unverified",
            )
            self.assertIs(verification["upgrade_eligible"], False)
            metadata = json.loads(
                Path(result["metadata_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                metadata["package_binding"],
                "package_binding_unverified",
            )
            self.assertIs(metadata["upgrade_eligible"], False)

    def containment_attempt(self, root: Path, *, bind_key: bool = True,
                            payload: str = "matching", blocked_access: bool = False) -> dict[str, Path]:
        return self.create_attempt(
            root=root,
            baseline_roles={"requires": ["storage/1.0@openubmc/stable"]},
            actual_roles={"requires": ["storage/1.0@openubmc/stable"]},
            services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
            image_directories=("opt/bmc/apps/ssdp",),
            image_inode_settings={"/opt/bmc/apps/ssdp": (0, 0, 0o700)} if blocked_access else None,
            hpm_payload=payload,
            bind_hpm_key=bind_key,
            use_runner=True,
        )

    def finalize(self, fixture: dict[str, Path]) -> subprocess.CompletedProcess[str]:
        return run(sys.executable, str(FINALIZE), "--plan", str(fixture["plan"]),
                   "--attempt-state", str(fixture["state"]))

    def test_signed_hpm_plan_attempt_finalizer_and_metadata_qualify_same_rootfs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.containment_attempt(Path(raw))
            finalized = self.finalize(fixture)
            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            result = json.loads(finalized.stdout)
            verification = json.loads(Path(result["verification_path"]).read_text())
            metadata = json.loads(Path(result["metadata_path"]).read_text())
            self.assertEqual(result["status"], "accepted")
            for record in (result, verification, metadata):
                self.assertIs(record["upgrade_eligible"], True)
                self.assertEqual(record["package_binding"], "package_binding_verified")
                proof = record["package_binding_proof"]
                self.assertEqual(proof["status"], "verified")
                self.assertEqual(proof["artifact"]["sha256"], snapshot(fixture["artifact"])["sha256"])
                self.assertEqual(proof["rootfs"]["sha256"], snapshot(fixture["image"])["sha256"])
                self.assertTrue(proof["payloads"]["signed_wrapper"]["manifest_digest_verified"])
                self.assertFalse(proof["payloads"]["signed_wrapper"]["signature_trust_verified"])
                rendered = json.dumps(record)
                self.assertNotIn(str(fixture["key"]), rendered)
                self.assertNotIn(TEST_KEY.decode(), rendered)
                self.assertNotIn(hashlib.sha256(TEST_KEY).hexdigest(), rendered)
            self.assertEqual(metadata["product_version"], "12.00.05.03")
            self.assertTrue(all(item["status"] == "pass" for item in verification["checks"]))
            plan = json.loads(fixture["plan"].read_text())
            self.assertEqual(plan["locks"]["hpm_key"]["sha256"], hashlib.sha256(TEST_KEY).hexdigest())
            self.assertEqual(metadata["build"]["plan_id"], plan["plan_id"])
            self.assertEqual(result["artifact_sha256"], metadata["artifact"]["sha256"])
            self.assertEqual(result["evidence_ids"][0], "build-plan:" + hashlib.sha256(fixture["plan"].read_bytes()).hexdigest())

    def test_valid_hpm_without_bound_key_remains_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.containment_attempt(Path(raw), bind_key=False)
            finalized = self.finalize(fixture)
            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            result = json.loads(finalized.stdout)
            metadata = json.loads(Path(result["metadata_path"]).read_text())
            self.assertEqual(result["status"], "accepted_local_only")
            self.assertIs(result["upgrade_eligible"], False)
            self.assertIs(metadata["upgrade_eligible"], False)
            self.assertEqual(metadata["package_binding_proof"], {})

    def test_qualified_finalizer_rejects_different_contained_rootfs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.containment_attempt(Path(raw), payload="mismatching")
            finalized = self.finalize(fixture)
            self.assertEqual(finalized.returncode, 1, finalized.stderr)
            result = json.loads(finalized.stdout)
            self.assertEqual(result["status"], "rejected")
            self.assertIs(result["upgrade_eligible"], False)
            self.assertEqual(result["package_binding_proof"]["reason"], "contained_rootfs_hash_mismatch")
            verification = json.loads(Path(result["verification_path"]).read_text())
            self.assertEqual(verification["failed_checks"], ["package-binding-policy"])
            self.assertFalse(Path(str(fixture["artifact"]) + ".metadata.json").exists())

    def test_qualified_finalizer_rejects_key_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.containment_attempt(Path(raw))
            fixture["key"].write_bytes(b"x" * 16)
            finalized = self.finalize(fixture)
            self.assertNotEqual(finalized.returncode, 0)
            self.assertIn("input_lock_drift: hpm_key.sha256", finalized.stderr)
            self.assertFalse(Path(str(fixture["artifact"]) + ".metadata.json").exists())
            self.assertFalse((fixture["state"].parent / "finalization.json").exists())

    def test_product_plan_rejects_invalid_key_before_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _ = self.create_manifest(root)
            baseline = root / "baseline.lock"
            write_lock(baseline)
            key = root / "invalid-key"
            key.write_bytes(b"x" * 15)
            planned = run(*self.plan_command(
                root=root, manifest=manifest, baseline=baseline,
                image=root / "rootfs.img", artifact=root / "firmware.hpm",
                actual_lock=root / "actual.lock", services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                key_path=key,
            ))
            self.assertNotEqual(planned.returncode, 0)
            self.assertIn("invalid_hpm_key_length", planned.stderr)
            self.assertFalse((root / "plan.json").exists())

    def test_metadata_rejects_containment_identity_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.containment_attempt(root)
            finalized = self.finalize(fixture)
            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            result = json.loads(finalized.stdout)
            source = (
                "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                "from write_artifact_metadata import write_metadata; "
                "write_metadata(Path(sys.argv[2]),verification_path=Path(sys.argv[3]),output_path=Path(sys.argv[4]))"
            )
            for role in ("artifact", "rootfs"):
                with self.subTest(role=role):
                    verification = json.loads(Path(result["verification_path"]).read_text())
                    verification["package_binding_proof"][role]["sha256"] = "0" * 64
                    tampered = root / "tampered-verification.json"
                    tampered.write_text(json.dumps(verification))
                    output = root / "untrusted.metadata.json"
                    written = run(sys.executable, "-c", source, str(BUILD_ROOT / "scripts"),
                                  str(fixture["artifact"]), str(tampered), str(output))
                    self.assertNotEqual(written.returncode, 0)
                    self.assertIn("verification_containment_identity_mismatch", written.stderr)
                    self.assertFalse(output.exists())

    def test_containment_does_not_override_a_failed_rootfs_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.containment_attempt(Path(raw), blocked_access=True)
            finalized = self.finalize(fixture)
            self.assertEqual(finalized.returncode, 1, finalized.stderr)
            result = json.loads(finalized.stdout)
            self.assertEqual(result["status"], "rejected")
            self.assertIs(result["upgrade_eligible"], False)
            self.assertEqual(result["package_binding_proof"]["status"], "verified")
            verification = json.loads(Path(result["verification_path"]).read_text())
            self.assertEqual(verification["failed_checks"], ["rootfs-access"])
            self.assertFalse(Path(str(fixture["artifact"]) + ".metadata.json").exists())

    def test_finalizer_rejects_touch_only_laundering_of_stale_product_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["outputs_before"] = {
                "artifact": snapshot(fixture["artifact"]),
                "dependency_lock": snapshot(fixture["actual_lock"]),
                "rootfs_image": snapshot(fixture["image"]),
            }
            time.sleep(0.01)
            for path in (
                fixture["artifact"],
                fixture["actual_lock"],
                fixture["image"],
            ):
                path.touch()
            state["outputs_after"] = {
                "artifact": snapshot(fixture["artifact"]),
                "dependency_lock": snapshot(fixture["actual_lock"]),
                "rootfs_image": snapshot(fixture["image"]),
            }
            fixture["state"].write_text(json.dumps(state), encoding="utf-8")

            finalized = run(
                sys.executable,
                str(FINALIZE),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )

            self.assertNotEqual(finalized.returncode, 0)
            self.assertIn("stale_product_output", finalized.stderr)
            self.assertFalse((fixture["state"].parent / "finalization.json").exists())

    def test_finalizer_rejects_touch_only_laundering_of_stale_dependency_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["outputs_before"]["dependency_lock"] = snapshot(
                fixture["actual_lock"]
            )
            time.sleep(0.01)
            fixture["actual_lock"].touch()
            state["outputs_after"]["dependency_lock"] = snapshot(
                fixture["actual_lock"]
            )
            fixture["state"].write_text(json.dumps(state), encoding="utf-8")

            finalized = run(
                sys.executable,
                str(FINALIZE),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )

            self.assertNotEqual(finalized.returncode, 0)
            self.assertIn("stale_product_output: dependency_lock", finalized.stderr)
            self.assertFalse((fixture["state"].parent / "finalization.json").exists())

    def test_finalizer_rolls_back_formal_evidence_when_postcheck_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            scripts_root = str(BUILD_ROOT / "scripts")
            sys.path.insert(0, scripts_root)
            try:
                import finalize_product_attempt as finalizer_module

                real_verify_workspaces = finalizer_module.verify_workspaces
                calls = 0

                def fail_postcommit_workspace_check(plan: dict[str, object]) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise ValueError("forced_postcommit_workspace_drift")
                    real_verify_workspaces(plan)

                with mock.patch.object(
                    finalizer_module,
                    "verify_workspaces",
                    side_effect=fail_postcommit_workspace_check,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "forced_postcommit_workspace_drift",
                    ):
                        finalizer_module.finalization(
                            fixture["plan"], fixture["state"]
                        )
            finally:
                sys.path.remove(scripts_root)

            attempt_root = fixture["state"].parent
            self.assertFalse((attempt_root / "verification.json").exists())
            self.assertFalse((attempt_root / "finalization.json").exists())
            self.assertFalse(
                Path(f"{fixture['artifact']}.metadata.json").exists()
            )

    def test_finalization_is_an_immutable_terminal_record_for_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            command = (
                sys.executable,
                str(FINALIZE),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )
            first = run(*command)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_result = json.loads(first.stdout)
            evidence_paths = [
                Path(first_result["finalization_path"]),
                Path(first_result["verification_path"]),
                Path(first_result["metadata_path"]),
                *(Path(path) for path in first_result["gate_reports"]),
            ]
            original = {path: path.read_bytes() for path in evidence_paths}

            second = run(*command)

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("finalization_already_recorded", second.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in evidence_paths},
                original,
            )

    def test_finalizer_owned_sidecar_is_a_planned_workspace_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
                artifact_inside_manifest=True,
            )

            finalized = run(
                sys.executable,
                str(FINALIZE),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )

            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            result = json.loads(finalized.stdout)
            self.assertEqual(result["status"], "accepted_local_only")
            self.assertTrue(Path(result["metadata_path"]).is_file())

    def test_finalizer_rejects_old_gate_reports_after_output_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.create_attempt(
                root=root,
                baseline_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                actual_roles={
                    "requires": ["storage/1.0@openubmc/stable"],
                },
                services=("ssdp=104:104=/opt/bmc/apps/ssdp",),
                image_directories=("opt/bmc/apps/ssdp",),
            )
            reports = fixture["state"].parent / "reports"
            reports.mkdir()
            dependency_report = reports / "dependency-delta.json"
            rootfs_report = reports / "rootfs-access.json"
            for script, output in (
                (CHECK_DEPENDENCIES, dependency_report),
                (CHECK_ROOTFS, rootfs_report),
            ):
                checked = run(
                    sys.executable,
                    str(script),
                    "--plan",
                    str(fixture["plan"]),
                    "--attempt-state",
                    str(fixture["state"]),
                    "--output",
                    str(output),
                )
                self.assertEqual(checked.returncode, 0, checked.stderr)

            changed = json.loads(
                fixture["actual_lock"].read_text(encoding="utf-8")
            )
            changed["requires"] = ["storage/9.9@openubmc/stable"]
            fixture["actual_lock"].write_text(
                json.dumps(changed),
                encoding="utf-8",
            )
            finalized = run(
                sys.executable,
                str(FINALIZE),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
            )
            self.assertNotEqual(finalized.returncode, 0)
            self.assertIn("output_identity_mismatch", finalized.stderr)
            self.assertFalse(
                Path(f"{fixture['artifact']}.metadata.json").exists()
            )

            standalone = run(
                sys.executable,
                str(VERIFY_PRODUCT),
                "--plan",
                str(fixture["plan"]),
                "--attempt-state",
                str(fixture["state"]),
                "--artifact",
                str(fixture["artifact"]),
                "--gate-report",
                str(dependency_report),
                "--gate-report",
                str(rootfs_report),
                "--output",
                str(root / "standalone-verification.json"),
            )
            self.assertNotEqual(standalone.returncode, 0)
            self.assertIn("finalize_product_attempt", standalone.stderr)

            writer = run(
                sys.executable,
                str(BUILD_ROOT / "scripts/write_artifact_metadata.py"),
                "--artifact-path",
                str(fixture["artifact"]),
                "--verification",
                str(root / "standalone-verification.json"),
            )
            self.assertEqual(writer.returncode, 2)
            self.assertIn("finalize_product_attempt", writer.stderr)


if __name__ == "__main__":
    unittest.main()
