from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys


REQUIREMENT_ROLES = (
    "requires",
    "build_requires",
    "python_requires",
    "config_requires",
)


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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


def write_resolved_lock(path: Path, **roles: list[str]) -> None:
    document = {
        role: list(roles.get(role, []))
        for role in REQUIREMENT_ROLES
    }
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


def create_product_attempt(
    root: Path,
    build_root: Path,
    *,
    image_directories: tuple[str, ...] = (),
    image_inode_settings: dict[str, tuple[int, int, int]] | None = None,
) -> dict[str, Path]:
    creator = build_root / "scripts" / "create_build_plan.py"
    runner = build_root / "scripts" / "run_build_attempt.py"

    manifest = root / "manifest"
    manifest.mkdir()
    init_repo(manifest)
    product_lock = manifest / "build/ibmc.lock"
    write_resolved_lock(
        product_lock,
        requires=["storage/1.100.74@openubmc/stable"],
    )

    baseline_lock = root / "baseline.lock"
    write_resolved_lock(
        baseline_lock,
        requires=[
            "storage/1.100.74@openubmc/stable",
            "devmon/1.2.63@openubmc/stable",
        ],
    )
    actual_source = root / "actual-source.lock"
    write_resolved_lock(
        actual_source,
        requires=[
            "storage/1.100.75@openubmc/stable",
            "devmon/1.2.63@openubmc/stable",
        ],
    )

    artifact = root / "rootfs_openUBMC.hpm"
    rootfs_image = root / "rootfs_openUBMC.ext4"
    rootfs_template = root / "rootfs-template.ext4"
    make_ext4(
        root,
        rootfs_template,
        directories=image_directories,
    )
    for path, (uid, gid, mode) in (image_inode_settings or {}).items():
        set_image_directory(
            rootfs_template,
            path,
            uid=uid,
            gid=gid,
            mode=mode,
        )
    actual_lock = root / "package.lock"
    producer = root / "produce.py"
    producer.write_text(
        "import pathlib, shutil, sys\n"
        "artifact, image, template, lock, source = "
        "map(pathlib.Path, sys.argv[1:6])\n"
        "artifact.write_bytes(b'firmware')\n"
        "shutil.copyfile(template, image)\n"
        "shutil.copyfile(source, lock)\n",
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
        str(artifact),
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
        "--allowed-dependency-change",
        "storage",
        "--cwd",
        str(manifest),
        "--output",
        str(plan_path),
        "--run-root",
        str(run_root),
        "--",
        sys.executable,
        str(producer),
        str(artifact),
        str(rootfs_image),
        str(rootfs_template),
        str(actual_lock),
        str(actual_source),
    )
    if planned.returncode != 0:
        raise AssertionError(planned.stderr)

    attempt = run(
        sys.executable,
        str(runner),
        "--plan",
        str(plan_path),
        "--run-root",
        str(run_root),
    )
    if attempt.returncode != 0:
        raise AssertionError(attempt.stderr)
    attempt_result = json.loads(attempt.stdout)
    state_path = Path(attempt_result["state_path"])

    return {
        "manifest": manifest,
        "artifact": artifact,
        "rootfs_image": rootfs_image,
        "rootfs_template": rootfs_template,
        "actual_lock": actual_lock,
        "plan": plan_path,
        "state": state_path,
        "dependency_report": state_path.parent / "reports/dependency-delta.json",
        "permission_report": state_path.parent / "reports/rootfs-access.json",
    }


def create_verified_product(root: Path, build_root: Path) -> dict[str, Path]:
    fixture = create_product_attempt(root, build_root)
    finalizer = build_root / "scripts" / "finalize_product_attempt.py"

    finalized = run(
        sys.executable,
        str(finalizer),
        "--plan",
        str(fixture["plan"]),
        "--attempt-state",
        str(fixture["state"]),
    )
    if finalized.returncode != 0:
        raise AssertionError(finalized.stderr)
    finalization_result = json.loads(finalized.stdout)

    return {
        **fixture,
        "finalization": Path(finalization_result["finalization_path"]),
        "verification": Path(finalization_result["verification_path"]),
        "metadata": Path(finalization_result["metadata_path"]),
    }
