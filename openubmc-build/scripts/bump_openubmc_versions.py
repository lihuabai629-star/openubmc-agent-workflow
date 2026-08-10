#!/usr/bin/env python3
"""Dry-run or write openUBMC component/product version bumps."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def read_text_preserve_newlines(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_text_preserve_newlines(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def bump_dotted(version: str, step: int = 1) -> str:
    parts = version.split(".")
    if not parts or not parts[-1].isdigit():
        raise ValueError(f"version last segment is not numeric: {version}")
    width = len(parts[-1])
    parts[-1] = str(int(parts[-1]) + step).zfill(width)
    return ".".join(parts)


def next_product_version(version: str, build_type: str, step: int | None) -> tuple[str, int]:
    desired_even = build_type == "debug"
    if step is not None:
        if step not in (1, 2):
            raise ValueError("--step must be 1 or 2 for product version bumps")
        candidate = bump_dotted(version, step)
        actual_even = int(candidate.split(".")[-1]) % 2 == 0
        if actual_even != desired_even:
            want = "even/debug" if desired_even else "odd/release"
            raise ValueError(f"{candidate} does not match required {want} parity")
        return candidate, step

    for inferred_step in (1, 2):
        candidate = bump_dotted(version, inferred_step)
        actual_even = int(candidate.split(".")[-1]) % 2 == 0
        if actual_even == desired_even:
            return candidate, inferred_step
    raise ValueError(f"cannot infer next {build_type} version from {version}")


def update_service_json(path: Path, step: int, write: bool) -> tuple[str, str]:
    text = read_text_preserve_newlines(path)
    data = json.loads(text)
    old = data.get("version")
    if not isinstance(old, str):
        raise ValueError(f"{path}: top-level version is missing or not a string")
    new = bump_dotted(old, step)
    pattern = re.compile(r'("version"\s*:\s*")' + re.escape(old) + r'(")')
    updated, count = pattern.subn(lambda match: f"{match.group(1)}{new}{match.group(2)}", text, count=1)
    if count != 1:
        raise ValueError(f"{path}: could not locate top-level version text for {old}")
    if write:
        write_text_preserve_newlines(path, updated)
    return old, new


def find_base_version(lines: list[str]) -> tuple[int, str]:
    base_indent = None
    in_base = False
    version_re = re.compile(r'^(\s*)version\s*:\s*(["\']?)([^"\'\s#]+)(["\']?)(.*)$')
    for index, line in enumerate(lines):
        if re.match(r"^\s*base\s*:", line):
            base_indent = len(line) - len(line.lstrip(" "))
            in_base = True
            continue
        if not in_base:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if line.strip() and base_indent is not None and indent <= base_indent:
            in_base = False
            continue
        match = version_re.match(line)
        if match:
            return index, match.group(3)
    raise ValueError("could not find base.version in product manifest")


def update_product_manifest(path: Path, build_type: str, step: int | None, write: bool) -> tuple[str, str, int]:
    lines = read_text_preserve_newlines(path).splitlines(keepends=True)
    index, old = find_base_version(lines)
    new, used_step = next_product_version(old, build_type, step)
    pattern = re.compile(r'^(\s*version\s*:\s*)(["\']?)([^"\'\s#]+)(["\']?)(.*)$')
    match = pattern.match(lines[index])
    if not match:
        raise ValueError(f"{path}: version line changed while updating")
    quote = match.group(2) or match.group(4)
    replacement = f"{match.group(1)}{quote}{new}{quote}{match.group(5)}"
    if lines[index].endswith("\n") and not replacement.endswith("\n"):
        replacement += "\n"
    lines[index] = replacement
    if write:
        write_text_preserve_newlines(path, "".join(lines))
    return old, new, used_step


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump openUBMC service.json and product base.version")
    parser.add_argument("--component-root", help="component root containing mds/service.json")
    parser.add_argument("--service-json", help="explicit mds/service.json path")
    parser.add_argument("--product-manifest", help="build/product/<board>/manifest.yml path")
    parser.add_argument("--build-type", choices=("debug", "release"), help="product package type")
    parser.add_argument("--step", type=int, help="explicit version step; product accepts 1 or 2")
    parser.add_argument("--write", action="store_true", help="write changes; default is dry-run")
    args = parser.parse_args()

    service_json = Path(args.service_json) if args.service_json else None
    if args.component_root:
        service_json = Path(args.component_root) / "mds" / "service.json"

    if not service_json and not args.product_manifest:
        parser.error("provide --component-root/--service-json and/or --product-manifest")
    if args.product_manifest and not args.build_type:
        parser.error("--product-manifest requires --build-type debug|release")

    try:
        if service_json:
            old, new = update_service_json(service_json, 1, args.write)
            action = "updated" if args.write else "would update"
            print(f"{action} component version: {service_json}: {old} -> {new} (+1)")
        if args.product_manifest:
            old, new, used_step = update_product_manifest(
                Path(args.product_manifest), args.build_type, args.step, args.write
            )
            action = "updated" if args.write else "would update"
            print(
                f"{action} product version: {args.product_manifest}: "
                f"{old} -> {new} (+{used_step}, {args.build_type})"
            )
    except Exception as exc:  # noqa: BLE001 - command-line helper should print concise errors.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
