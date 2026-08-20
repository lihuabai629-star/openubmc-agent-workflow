#!/usr/bin/env python3
"""Run the repository's self-contained workflow validation suite."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLES = (
    ROOT / "bootstrap.py",
    ROOT / "scripts" / "validate_workflow.py",
    ROOT / "scripts" / "live_smoke.py",
)
SKILL_PACKAGE_IGNORED_PARTS = frozenset(
    {"tests", "__pycache__", ".pytest_cache"}
)


def run(command: list[str], *, cwd: Path = ROOT, stage: str) -> None:
    github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if github_actions:
        print(f"::group::{stage}", flush=True)
    else:
        print(f"==> {stage}", flush=True)
    print("$ " + " ".join(command), flush=True)
    try:
        result = subprocess.run(command, cwd=cwd, check=False)
    finally:
        if github_actions:
            print("::endgroup::", flush=True)
    if result.returncode:
        print(f"validation stage failed: {stage}", file=sys.stderr, flush=True)
        raise SystemExit(result.returncode)


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise SystemExit(f"invalid Skill frontmatter: {path}")
    lines = match.group(1).splitlines()
    top_level_keys = {
        line.partition(":")[0].strip()
        for line in lines
        if line and not line[0].isspace() and ":" in line
    }
    if top_level_keys != {"name", "description"}:
        extras = ", ".join(sorted(top_level_keys - {"name", "description"})) or "missing required key"
        raise SystemExit(f"unsupported Skill frontmatter in {path}: {extras}")
    values: dict[str, str] = {}
    for index, line in enumerate(lines):
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            normalized = value.strip().strip("'\"")
            if key.strip() == "description" and normalized in {">", ">-", "|", "|-"}:
                continuation = []
                for following in lines[index + 1 :]:
                    if following and not following[0].isspace() and ":" in following:
                        break
                    if following.strip():
                        continuation.append(following.strip())
                normalized = " ".join(continuation)
            values[key.strip()] = normalized
    return values


def validate_agent_metadata(skill_root: Path, name: str) -> None:
    path = skill_root / "agents" / "openai.yaml"
    if not path.is_file():
        raise SystemExit(f"missing agents/openai.yaml: {skill_root.relative_to(ROOT)}")
    content = path.read_text(encoding="utf-8")
    for field in ("display_name", "short_description", "default_prompt"):
        if not re.search(rf"^\s{{2}}{field}:\s+['\"].+['\"]\s*$", content, re.MULTILINE):
            raise SystemExit(f"invalid {field} in {path.relative_to(ROOT)}")
    if f"${name}" not in content:
        raise SystemExit(f"default_prompt does not invoke ${name}: {path.relative_to(ROOT)}")


def validate_skill_manifest(skill_root: Path, name: str) -> None:
    path = skill_root / "skill.json"
    if not path.is_file():
        raise SystemExit(f"missing skill.json: {skill_root.relative_to(ROOT)}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("manifestVersion") != 1:
        raise SystemExit(f"unsupported skill.json manifestVersion: {path.relative_to(ROOT)}")
    if document.get("name") != name:
        raise SystemExit(f"skill.json name mismatch: {path.relative_to(ROOT)}")
    files = document.get("files")
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise SystemExit(f"invalid skill.json files list: {path.relative_to(ROOT)}")
    if len(files) != len(set(files)):
        raise SystemExit(f"duplicate skill.json files entry: {path.relative_to(ROOT)}")
    for relative in files:
        package_path = PurePosixPath(relative)
        if (
            not relative
            or package_path.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in package_path.parts)
        ):
            raise SystemExit(
                f"invalid skill.json file path ({relative}): {path.relative_to(ROOT)}"
            )
    required = {"SKILL.md", "agents/openai.yaml"}
    if not required.issubset(files):
        missing = ", ".join(sorted(required - set(files)))
        raise SystemExit(f"skill.json omits required files ({missing}): {path.relative_to(ROOT)}")
    for relative in files:
        if not (skill_root / relative).is_file():
            raise SystemExit(f"skill.json lists missing file: {skill_root.relative_to(ROOT) / relative}")
    declared = set(files)
    package_files = {
        path.relative_to(skill_root).as_posix()
        for path in skill_root.rglob("*")
        if path.is_file()
        and path.suffix != ".pyc"
        and not SKILL_PACKAGE_IGNORED_PARTS.intersection(
            path.relative_to(skill_root).parts
        )
    }
    omitted = sorted(package_files - declared)
    if omitted:
        raise SystemExit(
            "skill.json omits package files ("
            + ", ".join(omitted)
            + f"): {path.relative_to(ROOT)}"
        )


def assigned_expression(tree: ast.Module, name: str) -> ast.expr:
    for statement in tree.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            return statement.value
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
            return statement.value
    raise SystemExit(f"installer is missing {name}")


def validate_installer_manifest(document: dict[str, object]) -> None:
    installer_path = (
        ROOT / "openubmc-environment-setup" / "scripts" / "install_environment.py"
    )
    tree = ast.parse(installer_path.read_text(encoding="utf-8"), filename=str(installer_path))
    bundle = ast.literal_eval(assigned_expression(tree, "SKILL_BUNDLE"))
    expected_bundle = tuple(
        (str(item["name"]), str(item["path"]))
        for item in document["skills"]
    )
    if bundle != expected_bundle:
        raise SystemExit("workflow.json and installer SKILL_BUNDLE differ")

    target_expression = assigned_expression(tree, "TARGET_RUNTIME_SKILL_NAMES")
    if not isinstance(target_expression, ast.Call) or len(target_expression.args) != 1:
        raise SystemExit("installer TARGET_RUNTIME_SKILL_NAMES is not a static set")
    target_names = set(ast.literal_eval(target_expression.args[0]))
    profiles = dict(document.get("profiles", {}))
    if profiles.get("full") != [name for name, _ in expected_bundle]:
        raise SystemExit("full profile must contain every workflow Skill in manifest order")
    if set(profiles.get("target-runtime", [])) != target_names:
        raise SystemExit("workflow.json and installer target-runtime profile differ")


def validate_manifest() -> dict[str, object]:
    document = json.loads((ROOT / "workflow.json").read_text(encoding="utf-8"))
    skills = document.get("skills")
    if not isinstance(skills, list) or not skills:
        raise SystemExit("workflow.json has no Skills")
    names: set[str] = set()
    for item in skills:
        if not isinstance(item, dict):
            raise SystemExit("workflow.json contains an invalid Skill entry")
        name = str(item.get("name", ""))
        relative = str(item.get("path", ""))
        metadata = frontmatter(ROOT / relative / "SKILL.md")
        if metadata.get("name") != name or not metadata.get("description"):
            raise SystemExit(f"Skill metadata mismatch: {relative}")
        if name in names:
            raise SystemExit(f"duplicate Skill name: {name}")
        names.add(name)
        skill_root = ROOT / relative
        validate_agent_metadata(skill_root, name)
        validate_skill_manifest(skill_root, name)
    profiles = document.get("profiles", {})
    for profile, selected in dict(profiles).items():
        if not isinstance(selected, list) or not set(selected).issubset(names):
            raise SystemExit(f"invalid Skill profile: {profile}")
    validate_installer_manifest(document)
    return document


def validate_release_metadata(document: dict[str, object]) -> None:
    package = json.loads(
        (ROOT / "openubmc-kb-mcp" / "package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (ROOT / "openubmc-kb-mcp" / "package-lock.json").read_text(encoding="utf-8")
    )
    knowledge_version = str(dict(document.get("components", {})).get("knowledge_mcp", ""))
    if package.get("name") != "openubmc-kb-mcp":
        raise SystemExit("openubmc-kb-mcp package name mismatch")
    if package.get("version") != knowledge_version:
        raise SystemExit("workflow and openubmc-kb-mcp versions differ")
    if lock.get("name") != package.get("name") or lock.get("version") != package.get("version"):
        raise SystemExit("openubmc-kb-mcp package-lock metadata differs")
    installer = (
        ROOT / "openubmc-environment-setup" / "scripts" / "install_environment.py"
    ).read_text(encoding="utf-8")
    if f'KNOWLEDGE_MCP_VERSION = "{knowledge_version}"' not in installer:
        raise SystemExit("installer knowledge MCP version differs")

    evaluation = ET.parse(ROOT / "openubmc-kb-mcp" / "evals" / "evaluation.xml")
    pairs = evaluation.getroot().findall("qa_pair")
    if len(pairs) != 10:
        raise SystemExit("openubmc-kb-mcp evaluation must contain 10 QA pairs")
    for pair in pairs:
        if not (pair.findtext("question") or "").strip() or not (pair.findtext("answer") or "").strip():
            raise SystemExit("openubmc-kb-mcp evaluation contains an empty QA pair")

    for path in EXECUTABLES:
        if not path.is_file() or not path.stat().st_mode & 0o111:
            raise SystemExit(f"workflow script is not executable: {path.relative_to(ROOT)}")

    retired_route = "code" + "-review"
    legacy_knowledge_name = "openubmc-" + "studio"
    legacy_knowledge_files = {
        Path("openubmc-environment-setup/SKILL.md"),
        Path("openubmc-environment-setup/scripts/install_environment.py"),
        Path("openubmc-environment-setup/tests/test_install_environment.py"),
    }
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if retired_route in content:
            raise SystemExit(f"retired review route found in {path.relative_to(ROOT)}")
        relative = path.relative_to(ROOT)
        if legacy_knowledge_name in content and relative not in legacy_knowledge_files:
            raise SystemExit(f"legacy knowledge MCP name found outside migration files: {relative}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="skip unit and Node.js tests")
    parser.add_argument(
        "--release-contract-only",
        action="store_true",
        help="validate release manifests and metadata without compiling or testing",
    )
    args = parser.parse_args(argv)
    document = validate_manifest()
    validate_release_metadata(document)
    if args.release_contract_only:
        print("release contract validation passed")
        return 0
    run(
        [sys.executable, "-m", "compileall", "-q", "."],
        stage="Python compile",
    )
    if args.quick:
        print("workflow validation passed")
        return 0
    test_roots = sorted(
        path.parent
        for path in ROOT.glob("*/tests/test_*.py")
    )
    for tests in dict.fromkeys(test_roots):
        run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", "test_*.py"],
            stage=f"Python tests: {tests.relative_to(ROOT).as_posix()}",
        )
    node_root = ROOT / "openubmc-kb-mcp"
    run(
        ["npm", "ci", "--no-audit", "--no-fund"],
        cwd=node_root,
        stage="Node dependencies: openubmc-kb-mcp",
    )
    run(
        ["npm", "test"],
        cwd=node_root,
        stage="Node tests: openubmc-kb-mcp",
    )
    run(
        ["npm", "run", "check"],
        cwd=node_root,
        stage="Node syntax: openubmc-kb-mcp",
    )
    print("workflow validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
