#!/usr/bin/env python3
"""Locate or update component Conan refs under an openUBMC manifest."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def read_text_preserve_newlines(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_text_preserve_newlines(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def iter_manifest_files(manifest_root: Path, stage: str | None) -> list[Path]:
    root = manifest_root / "build" / "subsys"
    if not root.is_dir():
        raise ValueError(f"subsys directory not found: {root}")
    files = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".yml", ".yaml"):
            continue
        files.append(path)
    if not stage:
        return files

    staged = [path for path in files if stage in path.relative_to(root).parts]
    if staged:
        return staged
    if stage == "dev":
        return [path for path in files if len(path.relative_to(root).parts) == 1]
    return staged


def conan_ref_pattern(component: str) -> re.Pattern[str]:
    escaped = re.escape(component)
    return re.compile(rf'(?P<quote>["\']?)(?P<ref>{escaped}/[^"\'\s,\]\}}]+)(?P=quote)')


def find_refs(files: list[Path], component: str) -> list[tuple[Path, int, str]]:
    pattern = conan_ref_pattern(component)
    matches: list[tuple[Path, int, str]] = []
    for path in files:
        for index, line in enumerate(read_text_preserve_newlines(path).splitlines(), start=1):
            for match in pattern.finditer(line):
                matches.append((path, index, match.group("ref")))
    return matches


def replace_refs(matches: list[tuple[Path, int, str]], component: str, new_ref: str) -> dict[Path, int]:
    pattern = conan_ref_pattern(component)
    changed: dict[Path, int] = {}
    for path in sorted({item[0] for item in matches}):
        text = read_text_preserve_newlines(path)
        updated, count = pattern.subn(lambda m: f"{m.group('quote')}{new_ref}{m.group('quote')}", text)
        if count:
            write_text_preserve_newlines(path, updated)
            changed[path] = count
    return changed


def product_contains_component(product_manifest: Path, component: str) -> bool:
    if not product_manifest.is_file():
        raise ValueError(f"product manifest not found: {product_manifest}")
    return component in read_text_preserve_newlines(product_manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run or update openUBMC manifest Conan refs")
    parser.add_argument("--manifest-root", required=True, help="manifest workspace root")
    parser.add_argument("--component", required=True, help="component/package name, e.g. general_hardware")
    parser.add_argument("--new-ref", required=True, help="new Conan ref, e.g. component/1.2.3@openubmc/stable")
    parser.add_argument("--stage", help="limit search to build/subsys/<stage>/... path component")
    parser.add_argument("--product-manifest", help="optional product manifest.yml to check dependency presence")
    parser.add_argument("--write", action="store_true", help="write replacements; default is dry-run")
    args = parser.parse_args()

    if not args.new_ref.startswith(f"{args.component}/"):
        print(f"error: --new-ref must start with {args.component}/", file=sys.stderr)
        return 1

    try:
        files = iter_manifest_files(Path(args.manifest_root), args.stage)
        matches = find_refs(files, args.component)
        if not matches:
            print(f"no existing refs for {args.component} under build/subsys")
            print("if this is a new component, add the correct product/subsystem dependency explicitly")
            return 2

        print(f"found {len(matches)} ref(s) for {args.component}:")
        for path, line, old_ref in matches:
            print(f"  {path}:{line}: {old_ref} -> {args.new_ref}")

        if args.product_manifest:
            present = product_contains_component(Path(args.product_manifest), args.component)
            status = "present" if present else "not found"
            print(f"product manifest component text check: {status}: {args.product_manifest}")
            if not present:
                print(
                    "component may be selected through an existing subsystem; "
                    "only new components require product/subsystem dependency wiring"
                )

        if args.write:
            changed = replace_refs(matches, args.component, args.new_ref)
            for path, count in changed.items():
                print(f"updated {count} ref(s): {path}")
        else:
            print("dry-run only; pass --write to update files")
    except Exception as exc:  # noqa: BLE001 - command-line helper should print concise errors.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
