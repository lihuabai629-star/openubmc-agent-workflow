#!/usr/bin/env python3
"""Run the repository's self-contained workflow validation suite."""

from __future__ import annotations

import argparse
import ast
import hashlib
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
    ROOT / "scripts" / "compatibility_retirement.py",
    ROOT / "scripts" / "codex_adoption_qualification.py",
    ROOT / "scripts" / "continuous_closeout_qualification.py",
    ROOT / "scripts" / "evaluation_harness.py",
    ROOT / "scripts" / "model_planning_evaluation.py",
    ROOT / "scripts" / "product_closeout_ingestion.py",
    ROOT / "scripts" / "product_closeout_qualification.py",
    ROOT / "scripts" / "diagnosis_chain_qualification.py",
    ROOT / "scripts" / "validate_workflow.py",
    ROOT / "scripts" / "live_smoke.py",
)
SKILL_PACKAGE_IGNORED_PARTS = frozenset(
    {"tests", "__pycache__", ".pytest_cache"}
)
ROADMAP_CLOSEOUT_REFERENCES = {
    "README.md": "docs/roadmap-completion.json",
    "docs/adr/README.md": "[ADR-0005](0005-retire-compatibility-writers-and-profile.md) | Accepted",
    "docs/adr/0005-retire-compatibility-writers-and-profile.md": "../roadmap-completion.json",
    "docs/compatibility-retirement.md": "roadmap-completion.json",
    "docs/workflow-evolution-roadmap.md": "roadmap-completion.json",
    "docs/roadmap-completion-audit.md": "roadmap-completion.json",
}
ROADMAP_CLOSEOUT_FORBIDDEN = {
    "README.md": ("must not be promoted to canonical `main`",),
    "docs/adr/README.md": ("ADR-0005](0005-retire-compatibility-writers-and-profile.md) | Proposed",),
    "docs/adr/0005-retire-compatibility-writers-and-profile.md": ("- Status: Proposed",),
    "docs/compatibility-retirement.md": (
        "compatibility-retirement candidate",
        "candidate must remain unmerged",
    ),
    "docs/workflow-architecture-arbitration.md": ("不得进入 canonical `main`",),
    "docs/external-workflow-research-reconciliation.md": ("不得进入 canonical `main`",),
    "docs/workflow-evolution-roadmap.md": (
        "完成候选",
        "等待 GitHub 合入证据",
    ),
}
ROADMAP_BATCH_IDS = frozenset(
    {
        "runtime-core-v2-qualification",
        "compatibility-retirement",
        "incident-recovery-stability",
        "artifact-log-bundle",
        "domain-pack-read-only",
        "evidence-skill-disclosure",
        "p2-lifecycle-qualification",
    }
)
ROADMAP_WRITERS = frozenset(
    {
        "execute.control_continue",
        "execute.observation_receipt",
        "observe.assurance",
        "phase_record",
        "workflow.next",
    }
)
RELEASE_CANDIDATE_SUPERSEDED_UNPUBLISHED = "superseded-unpublished"
RELEASE_SOURCE_POLICY_NEW_FINAL_SOURCE = "new-final-source"
ROADMAP_P2_ISSUE = 79
ROADMAP_P2_PULL_REQUEST = 80
ROADMAP_P2_MERGE_COMMIT = "2564f3572fd82668dfd90ba3bd2e3439d021ec63"
ROADMAP_P2_PR_CI_RUN = 32933867020
ROADMAP_P2_MAIN_CI_RUN = 32934292608
ROADMAP_POST_P2_REFERENCES = {
    "docs/workflow-evolution-roadmap.md": (
        ROADMAP_P2_MERGE_COMMIT,
        "| P2 生命周期持续资格 | 完成 |",
        f"PR CI run `{ROADMAP_P2_PR_CI_RUN}`",
        f"main CI run `{ROADMAP_P2_MAIN_CI_RUN}`",
        RELEASE_CANDIDATE_SUPERSEDED_UNPUBLISHED,
    ),
    "docs/roadmap-completion-audit.md": (
        f"issues/{ROADMAP_P2_ISSUE}",
        f"pull/{ROADMAP_P2_PULL_REQUEST}",
        ROADMAP_P2_MERGE_COMMIT,
        f"runs/{ROADMAP_P2_PR_CI_RUN}",
        f"runs/{ROADMAP_P2_MAIN_CI_RUN}",
        RELEASE_CANDIDATE_SUPERSEDED_UNPUBLISHED,
    ),
}


def evidence_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def verified_evidence_document(path: Path, *, schema: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid qualification evidence document: {path}") from error
    if not isinstance(document, dict) or document.get("schema") != schema:
        raise SystemExit(f"invalid qualification evidence schema: {path}")
    unsigned = dict(document)
    digest = unsigned.pop("evidence_digest", None)
    if digest != evidence_fingerprint(unsigned):
        raise SystemExit(f"invalid qualification evidence digest: {path}")
    return document


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
    clients = tuple(dict(document.get("clients", {})))
    installer_clients = tuple(ast.literal_eval(assigned_expression(tree, "CLIENTS")))
    if installer_clients != clients:
        raise SystemExit("workflow.json and installer product clients differ")
    expected_mcp_clients = tuple(
        name
        for name, contract in dict(document.get("clients", {})).items()
        if isinstance(contract, dict) and contract.get("mcp") is True
    )
    installer_mcp_clients = tuple(
        ast.literal_eval(assigned_expression(tree, "SUPPORTED_MCP_CLIENTS"))
    )
    if installer_mcp_clients != expected_mcp_clients:
        raise SystemExit("workflow.json and installer MCP clients differ")


def validate_client_and_harness_metadata(document: dict[str, object]) -> None:
    clients = document.get("clients")
    if not isinstance(clients, dict) or not clients:
        raise SystemExit("workflow.json has no supported product clients")
    for name, raw_client in clients.items():
        if not isinstance(name, str) or not name or not isinstance(raw_client, dict):
            raise SystemExit("workflow.json contains an invalid product client")
        if raw_client.get("role") != "supported-product-client":
            raise SystemExit(f"invalid product client role: {name}")
        if not all(isinstance(raw_client.get(field), bool) for field in ("skills", "mcp")):
            raise SystemExit(f"invalid product client capabilities: {name}")

    harnesses = document.get("evaluation_harnesses")
    if not isinstance(harnesses, dict) or not harnesses:
        raise SystemExit("workflow.json has no evaluation harnesses")
    overlap = sorted(set(clients) & set(harnesses))
    if overlap:
        raise SystemExit(
            "evaluation harnesses must not be product clients: "
            + ", ".join(overlap)
        )
    for name, raw_harness in harnesses.items():
        if not isinstance(name, str) or not name or not isinstance(raw_harness, dict):
            raise SystemExit("workflow.json contains an invalid evaluation harness")
        if raw_harness.get("role") != "evaluation-harness":
            raise SystemExit(f"invalid evaluation harness role: {name}")
        for field in ("adapter", "profile"):
            if not isinstance(raw_harness.get(field), str) or not raw_harness[field].strip():
                raise SystemExit(f"evaluation harness {name} requires {field}")
        executable = raw_harness.get("executable")
        if not isinstance(executable, dict) or any(
            not isinstance(executable.get(field), str)
            or not executable[field].strip()
            for field in (
                "command",
                "package",
                "minimum_version",
                "maximum_version_exclusive",
            )
        ):
            raise SystemExit(f"evaluation harness {name} executable contract is invalid")
        version_args = executable.get("version_args")
        if not isinstance(version_args, list) or not version_args or not all(
            isinstance(item, str) and item for item in version_args
        ):
            raise SystemExit(f"evaluation harness {name} version_args are invalid")
        identity_fields = raw_harness.get("model_identity_fields")
        if not isinstance(identity_fields, list) or not identity_fields or any(
            not isinstance(item, str) or not item for item in identity_fields
        ) or len(set(identity_fields)) != len(identity_fields):
            raise SystemExit(f"evaluation harness {name} model identity is invalid")
        scenarios = raw_harness.get("scenarios")
        scenario_ids: set[tuple[str, str]] = set()
        if not isinstance(scenarios, list) or not scenarios:
            raise SystemExit(f"evaluation harness {name} has no scenarios")
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise SystemExit(f"evaluation harness {name} scenario is invalid")
            identity = (str(scenario.get("name", "")), str(scenario.get("version", "")))
            if (
                not all(identity)
                or identity in scenario_ids
                or scenario.get("acceptance")
                not in {"scenario-receipt-v1", "terminal-outcome-v1"}
            ):
                raise SystemExit(f"evaluation harness {name} scenario identity is invalid")
            scenario_ids.add(identity)


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
    validate_client_and_harness_metadata(document)
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
    version_source = (ROOT / "openubmc-kb-mcp" / "src" / "version.js").read_text(encoding="utf-8")
    if f'KNOWLEDGE_MCP_VERSION = "{knowledge_version}"' not in version_source:
        raise SystemExit("KB server version identity differs from package metadata")
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
        Path("openubmc-environment-setup/tests/test_client_config.py"),
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

    validate_roadmap_closeout()


def _verified_lock_only_release(
    *,
    lock_commit: str,
    source_commit: str,
    changed_files: list[str],
    description: str,
) -> dict[str, object]:
    parents = subprocess.run(
        ["git", "-C", str(ROOT), "rev-list", "--parents", "-n", "1", lock_commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if parents != [lock_commit, source_commit]:
        raise SystemExit(f"invalid {description} lock-only parent")
    actual_changed_files = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            lock_commit,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if actual_changed_files != changed_files:
        raise SystemExit(f"invalid {description} lock-only diff")
    return json.loads(
        subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{lock_commit}:release-lock.json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )


def validate_roadmap_closeout(*, verify_git: bool = True) -> None:
    evidence_path = ROOT / "docs" / "roadmap-completion.json"
    if not evidence_path.is_file():
        raise SystemExit("missing roadmap completion evidence: docs/roadmap-completion.json")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SystemExit(f"invalid roadmap completion evidence: {error}") from error
    if not isinstance(evidence, dict):
        raise SystemExit("invalid roadmap completion evidence: expected an object")
    if evidence.get("schema") != "openubmc-agent-workflow.roadmap-completion.v1":
        raise SystemExit("invalid roadmap completion evidence schema")
    if evidence.get("status") != "completed" or evidence.get("closeout_issue") != 70:
        raise SystemExit("invalid roadmap completion evidence status")
    release = evidence.get("release")
    superseded_candidate = (
        release.get("superseded_candidate") if isinstance(release, dict) else None
    )
    planned_release = release.get("planned_release") if isinstance(release, dict) else None
    published_release = (
        release.get("published_release") if isinstance(release, dict) else None
    )
    if (
        not isinstance(release, dict)
        or set(release)
        != {
            "identity_model",
            "mutable_main_policy",
            "superseded_candidate",
            "published_release",
            "planned_release",
        }
        or release.get("identity_model") != "source-plus-lock-only-commit"
        or release.get("mutable_main_policy") != "historical-lock-snapshot"
        or not isinstance(superseded_candidate, dict)
        or not isinstance(published_release, dict)
        or not isinstance(planned_release, dict)
        or published_release.get("release_version") != "2.0.0"
        or published_release.get("tag") != "v2.0.0"
        or published_release.get("github_release_created") is not True
        or published_release.get("release_gate_promotable") is not True
        or planned_release
        != {
            "release_version": "2.0.1",
            "qualification_required": True,
            "source_policy": RELEASE_SOURCE_POLICY_NEW_FINAL_SOURCE,
        }
    ):
        raise SystemExit("invalid roadmap completion evidence release policy")

    canonical = evidence.get("canonical_main")
    qualification = evidence.get("qualification")
    if not isinstance(canonical, dict) or not isinstance(qualification, dict):
        raise SystemExit("invalid roadmap completion evidence identity")
    commit_pattern = re.compile(r"[0-9a-f]{40}")
    merge_commit = canonical.get("merge_commit")
    source_commit = qualification.get("source_commit")
    lock_commit = qualification.get("lock_only_commit")
    published_source_commit = published_release.get("source_commit")
    published_lock_commit = published_release.get("lock_only_commit")
    if any(
        not isinstance(commit, str) or commit_pattern.fullmatch(commit) is None
        for commit in (
            merge_commit,
            source_commit,
            lock_commit,
            published_source_commit,
            published_lock_commit,
        )
    ) or len(
        {
            merge_commit,
            source_commit,
            lock_commit,
            published_source_commit,
            published_lock_commit,
        }
    ) != 5:
        raise SystemExit("invalid roadmap completion evidence commits")
    main_ci = canonical.get("ci_run")
    if (
        not isinstance(main_ci, dict)
        or not isinstance(main_ci.get("id"), int)
        or main_ci.get("id", 0) <= 0
        or main_ci.get("conclusion") != "success"
    ):
        raise SystemExit("invalid roadmap completion evidence main CI")

    release_gate = qualification.get("release_gate")
    execute_ab = qualification.get("execute_ab")
    retirement = qualification.get("compatibility_retirement")
    lock_topology = qualification.get("lock_topology")
    digest_pattern = re.compile(r"sha256:[0-9a-f]{64}")
    if (
        qualification.get("release_version") != "2.0.0"
        or any(
            not isinstance(digest, str) or digest_pattern.fullmatch(digest) is None
            for digest in (
                qualification.get("release_lock_digest"),
                qualification.get("source_tree_digest"),
            )
        )
    ):
        raise SystemExit("invalid roadmap completion evidence release identity")
    if superseded_candidate != {
        "release_version": qualification["release_version"],
        "source_commit": source_commit,
        "lock_only_commit": lock_commit,
        "status": RELEASE_CANDIDATE_SUPERSEDED_UNPUBLISHED,
        "tag_created": False,
        "github_release_created": False,
    }:
        raise SystemExit("invalid roadmap completion evidence release policy")
    if (
        not isinstance(lock_topology, dict)
        or lock_topology.get("parent_source_commit") != source_commit
        or lock_topology.get("changed_files") != ["release-lock.json"]
    ):
        raise SystemExit("invalid roadmap completion evidence lock topology")
    if (
        not isinstance(release_gate, dict)
        or release_gate.get("promotable") is not True
        or release_gate.get("passed_gates") != release_gate.get("total_gates")
        or not isinstance(release_gate.get("total_gates"), int)
        or release_gate.get("total_gates", 0) <= 0
        or not isinstance(release_gate.get("evidence_digest"), str)
        or digest_pattern.fullmatch(release_gate["evidence_digest"]) is None
    ):
        raise SystemExit("invalid roadmap completion evidence release gate")
    if (
        not isinstance(execute_ab, dict)
        or execute_ab.get("decision") != "passed"
        or not isinstance(execute_ab.get("valid_pairs"), int)
        or execute_ab.get("valid_pairs", 0) < 10
        or execute_ab.get("invalid_pairs") != 0
        or not isinstance(execute_ab.get("evidence_digest"), str)
        or digest_pattern.fullmatch(execute_ab["evidence_digest"]) is None
    ):
        raise SystemExit("invalid roadmap completion evidence execute A/B")
    if not isinstance(retirement, dict):
        raise SystemExit("invalid roadmap completion evidence retirement")
    writers = retirement.get("writers_ready")
    if (
        not isinstance(writers, dict)
        or frozenset(writers) != ROADMAP_WRITERS
        or not all(value is True for value in writers.values())
        or retirement.get("profile_ready") is not True
        or retirement.get("historical_telemetry") != "preserved-read-only"
        or retirement.get("old_event_upcasters") != "preserved-read-only"
    ):
        raise SystemExit("invalid roadmap completion evidence retirement")

    continuous = evidence.get("continuous_qualification")
    if not isinstance(continuous, dict):
        raise SystemExit("invalid roadmap continuous qualification evidence")
    continuous_source = continuous.get("source_commit")
    continuous_groups = continuous.get("qualification_groups")
    projection_policy = continuous.get("agent_projection_policy")
    artifact_lifecycle = continuous.get("artifact_lifecycle")
    if (
        continuous.get("schema")
        != "openubmc-agent-workflow.p2-lifecycle-qualification.v1"
        or continuous.get("issue") != 79
        or not isinstance(continuous_source, str)
        or commit_pattern.fullmatch(continuous_source) is None
        or continuous.get("promotable") is not True
        or any(
            not isinstance(digest, str) or digest_pattern.fullmatch(digest) is None
            for digest in (
                continuous.get("evidence_digest"),
                continuous.get("aggregate_evidence_digest"),
                continuous.get("runtime_stability_digest"),
            )
        )
        or not isinstance(continuous_groups, dict)
        or continuous_groups.get("persisted_run_compatibility", 0) < 10
        or continuous_groups.get("semantic_projection_completion", 0) < 16
        or projection_policy
        != {
            "budget_mode": "soft-display-target",
            "observation_receipt_target_bytes": 4096,
            "gate_schema_target_bytes": 4096,
            "turn_target_bytes": 8192,
            "target_exceeded_behavior": "preserve-runtime-semantics",
            "manual_narrowing_required_on_target_exceeded": False,
            "projection_budget_blocker": False,
        }
        or not isinstance(artifact_lifecycle, dict)
        or artifact_lifecycle.get("created_raw_records") != 64
        or artifact_lifecycle.get("restart_record_count") != 66
        or artifact_lifecycle.get("first_gc_deleted_records") != 33
        or artifact_lifecycle.get("second_gc_deleted_records") != 32
        or artifact_lifecycle.get("second_gc_deleted_content") != 1
        or artifact_lifecycle.get("shared_content_deleted_after_final_reference")
        is not True
        or artifact_lifecycle.get("final_audit_record_count") != 1
        or artifact_lifecycle.get("shared_content_preserved_after_partial_gc")
        is not True
    ):
        raise SystemExit("invalid roadmap continuous qualification evidence")

    summary_path = continuous.get("summary_path")
    stability_path = continuous.get("stability_path")
    if not isinstance(summary_path, str) or not isinstance(stability_path, str):
        raise SystemExit("invalid roadmap continuous qualification evidence paths")
    summary = verified_evidence_document(
        ROOT / summary_path,
        schema="openubmc-agent-workflow.p2-lifecycle-qualification.v1",
    )
    stability = verified_evidence_document(
        ROOT / stability_path,
        schema="openubmc-agent-workflow.runtime-stability.v1",
    )
    stability_scenarios = stability.get("scenarios")
    stability_artifact = (
        stability_scenarios.get("artifact_lifecycle")
        if isinstance(stability_scenarios, dict)
        else None
    )
    if (
        summary.get("source_commit") != continuous_source
        or summary.get("promotable") is not True
        or summary.get("qualification_counts") != continuous_groups
        or summary.get("agent_projection_policy") != projection_policy
        or summary.get("artifact_lifecycle") != artifact_lifecycle
        or summary.get("aggregate_evidence_digest")
        != continuous.get("aggregate_evidence_digest")
        or summary.get("runtime_stability_digest")
        != continuous.get("runtime_stability_digest")
        or summary.get("evidence_digest") != continuous.get("evidence_digest")
        or stability.get("source_commit") != continuous_source
        or stability.get("promotable") is not True
        or stability.get("evidence_digest")
        != continuous.get("runtime_stability_digest")
        or not isinstance(stability_artifact, dict)
        or any(
            stability_artifact.get(name) != value
            for name, value in artifact_lifecycle.items()
        )
    ):
        raise SystemExit("roadmap continuous qualification evidence does not match")

    batches = evidence.get("batches")
    if not isinstance(batches, list) or {
        item.get("id") for item in batches if isinstance(item, dict)
    } != ROADMAP_BATCH_IDS:
        raise SystemExit("invalid roadmap completion evidence batches")
    all_merge_commits: list[str] = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise SystemExit("invalid roadmap completion evidence batch")
        commits = batch.get("merge_commits")
        ci_runs = batch.get("ci_runs")
        if (
            not isinstance(batch.get("issues"), list)
            or not batch["issues"]
            or not all(isinstance(value, int) and value > 0 for value in batch["issues"])
            or not isinstance(batch.get("pull_requests"), list)
            or not batch["pull_requests"]
            or not all(isinstance(value, int) and value > 0 for value in batch["pull_requests"])
            or not isinstance(commits, list)
            or not commits
            or not all(isinstance(value, str) and commit_pattern.fullmatch(value) for value in commits)
            or len(batch["pull_requests"]) != len(commits)
            or not isinstance(batch.get("test_seams"), list)
            or not batch["test_seams"]
            or not all(
                isinstance(value, str) and (ROOT / value).is_file()
                for value in batch["test_seams"]
            )
            or not isinstance(ci_runs, list)
            or not ci_runs
            or not all(
                isinstance(run, dict)
                and isinstance(run.get("id"), int)
                and run.get("id", 0) > 0
                and run.get("conclusion") == "success"
                for run in ci_runs
            )
            or batch.get("delivery_process")
            != [
                "isolated-worktree",
                "tdd",
                "standards-review",
                "spec-review",
                "github-ci",
                "merged",
            ]
        ):
            raise SystemExit(f"invalid roadmap completion evidence batch: {batch.get('id')}")
        all_merge_commits.extend(commits)
    if len(all_merge_commits) != len(set(all_merge_commits)):
        raise SystemExit("invalid roadmap completion evidence duplicate merge commit")
    compatibility_batch = next(item for item in batches if item["id"] == "compatibility-retirement")
    p2_batch = next(item for item in batches if item["id"] == "p2-lifecycle-qualification")
    if (
        compatibility_batch.get("qualified_source_commit") != source_commit
    ):
        raise SystemExit("invalid roadmap completion evidence compatibility relationship")
    if (
        merge_commit != ROADMAP_P2_MERGE_COMMIT
        or main_ci["id"] != ROADMAP_P2_MAIN_CI_RUN
        or p2_batch.get("issues") != [ROADMAP_P2_ISSUE]
        or p2_batch.get("pull_requests") != [ROADMAP_P2_PULL_REQUEST]
        or p2_batch.get("merge_commits") != [ROADMAP_P2_MERGE_COMMIT]
        or p2_batch.get("qualified_source_commit") != continuous_source
        or len(p2_batch.get("ci_runs", [])) < 2
        or {
            ROADMAP_P2_PR_CI_RUN,
            ROADMAP_P2_MAIN_CI_RUN,
        }
        - {
            run["id"]
            for run in p2_batch["ci_runs"]
            if isinstance(run, dict) and isinstance(run.get("id"), int)
        }
    ):
        raise SystemExit("invalid roadmap completion evidence P2 delivery evidence")

    if verify_git:
        repository = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
        )
        if repository.returncode or repository.stdout.strip() != "true":
            raise SystemExit("roadmap completion validation requires a git repository")
        commits = {
            merge_commit,
            source_commit,
            published_source_commit,
            published_lock_commit,
            continuous_source,
            *all_merge_commits,
        }
        for commit in commits:
            result = subprocess.run(
                ["git", "-C", str(ROOT), "cat-file", "-e", f"{commit}^{{commit}}"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise SystemExit(f"unresolvable roadmap completion commit: {commit}")
        for ancestor, descendant in (
            (source_commit, merge_commit),
            (merge_commit, "HEAD"),
            (continuous_source, "HEAD"),
            (published_source_commit, "HEAD"),
            *((commit, merge_commit) for commit in all_merge_commits),
        ):
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(ROOT),
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise SystemExit(
                    f"invalid roadmap completion commit ancestry: {ancestor} -> {descendant}"
                )
        # This detached lock-only candidate was superseded before publication.
        # A branch-only clone may omit its unreachable object. Keep checking its
        # recorded identity above, and verify its object whenever it is present.
        historical_lock_available = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{lock_commit}^{{commit}}"],
            check=False,
            capture_output=True,
        ).returncode == 0
        if historical_lock_available:
            locked_release = _verified_lock_only_release(
                lock_commit=lock_commit,
                source_commit=source_commit,
                changed_files=list(lock_topology["changed_files"]),
                description="roadmap completion",
            )
            expected_lock = {
                "release_version": qualification["release_version"],
                "source_commit": source_commit,
                "lock_digest": qualification["release_lock_digest"],
                "source_tree_digest": qualification["source_tree_digest"],
            }
            if any(locked_release.get(key) != value for key, value in expected_lock.items()):
                raise SystemExit("invalid roadmap completion historical release lock")
        published_lock = _verified_lock_only_release(
            lock_commit=published_lock_commit,
            source_commit=published_source_commit,
            changed_files=["release-lock.json"],
            description="published release",
        )
        published_tag = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "v2.0.0^{commit}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if published_tag.returncode or published_tag.stdout.strip() != published_lock_commit:
            raise SystemExit("invalid published release tag identity")
        if (
            published_lock.get("release_version") != "2.0.0"
            or published_lock.get("source_commit") != published_source_commit
        ):
            raise SystemExit("invalid published release lock identity")

    for relative, required in ROADMAP_CLOSEOUT_REFERENCES.items():
        path = ROOT / relative
        if not path.is_file():
            raise SystemExit(f"missing roadmap closeout document: {relative}")
        content = path.read_text(encoding="utf-8")
        if required not in content:
            raise SystemExit(f"roadmap closeout marker missing ({required}): {relative}")
    for relative, forbidden in ROADMAP_CLOSEOUT_FORBIDDEN.items():
        content = (ROOT / relative).read_text(encoding="utf-8").lower()
        for marker in forbidden:
            if marker.lower() in content:
                raise SystemExit(f"obsolete roadmap closeout state ({marker}): {relative}")
    for relative, required_markers in ROADMAP_POST_P2_REFERENCES.items():
        content = (ROOT / relative).read_text(encoding="utf-8")
        for marker in required_markers:
            if marker not in content:
                raise SystemExit(f"post-P2 roadmap marker missing ({marker}): {relative}")


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
    node_root = ROOT / "openubmc-kb-mcp"
    run(
        ["npm", "ci", "--no-audit", "--no-fund"],
        cwd=node_root,
        stage="Node dependencies: openubmc-kb-mcp",
    )
    run(
        [sys.executable, str(ROOT / "scripts" / "diagnosis_chain_qualification.py")],
        stage="Runtime diagnosis chain qualification",
    )
    run(
        [
            sys.executable,
            str(ROOT / "scripts" / "codex_adoption_qualification.py"),
            "--model-identity",
            '{"model":"codex-product-client-qualification"}',
            "--codex-identity",
            '{"version":"codex-cli 0.151.0"}',
        ],
        stage="Codex Adoption Qualification",
    )
    test_roots = sorted(
        path.parent
        for path in ROOT.glob("*/tests/test_*.py")
    )
    for tests in dict.fromkeys(test_roots):
        run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", "test_*.py"],
            stage=f"Python tests: {tests.relative_to(ROOT).as_posix()}",
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
