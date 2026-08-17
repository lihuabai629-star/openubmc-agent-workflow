#!/usr/bin/env python3
"""Install, inspect, repair, update, or remove the openUBMC agent workflow."""

from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stdout
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Literal, Mapping, NamedTuple, TypedDict
import urllib.error
import urllib.request


DEFAULT_REPO_URL = "https://github.com/lihuabai629-star/openubmc-agent-workflow.git"
DEFAULT_REF = "main"
FULL_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
MUTABLE_REFS = frozenset({"head", "main", "master", "develop", "development", "trunk"})
LEGACY_STUDIO_HTTP_URL = "http://localhost:9876/mcp"
KNOWLEDGE_MCP_NAME = "openubmc-kb"
LEGACY_STUDIO_MCP_NAME = "openubmc-studio"
KNOWLEDGE_MCP_VERSION = "1.3.0"
KNOWLEDGE_MCP_INSTALL_SCHEMA = "openubmc-kb.install.v1"
_KNOWLEDGE_DIGEST_DOMAIN = b"openubmc-kb-content-v1\0"
TARGET_RUNTIME_API_VERSION = "openubmc.target-runtime.v1"
TARGET_RUNTIME_MCP_NAME = "openubmc-target-runtime"
TARGET_RUNTIME_INSTALL_SCHEMA = "openubmc-target-runtime.install.v1"
_RUNTIME_DIGEST_DOMAIN = b"openubmc-target-runtime-content-v1\0"
STATE_VERSION = 1
COMMANDS = (
    "install",
    "check",
    "repair",
    "update",
    "rollback",
    "refresh",
    "credentials",
    "uninstall",
)
SkillBundle = tuple[tuple[str, str], ...]


class SkillProfilePolicy(NamedTuple):
    bundle: SkillBundle
    manages_knowledge_mcp: bool


class ResolvedSkillProfile(NamedTuple):
    name: str
    bundle: SkillBundle
    manages_knowledge_mcp: bool


class RecordedInstall(NamedTuple):
    source_root: Path
    source_mode: str
    source_commit: str
    resolved_commit: str
    rollback_commit: str
    repo_url: str
    ref: str
    requested_ref: str
    ref_kind: str
    clients: tuple[str, ...]
    profile: ResolvedSkillProfile
    knowledge_url: str
    target: str
    tool_dirs: tuple[str, ...]
    mcp: dict[str, Any]
    runtime_mcp: dict[str, Any]
    links: dict[str, str]
    preserved_skills: tuple[str, ...]
    profiles: tuple[str, ...]
    runtime: dict[str, Any]


class HttpMcpEntry(TypedDict):
    type: Literal["http"]
    url: str


class StdioMcpEntry(TypedDict):
    type: Literal["stdio"]
    command: str
    args: list[str]

# canonical Skill name -> repository-relative directory
SKILL_BUNDLE: SkillBundle = (
    ("openubmc-environment-setup", "openubmc-environment-setup"),
    ("openubmc-debug", "openubmc-debug"),
    ("openubmc-log-analyzer", "openubmc-log-analyzer"),
    ("openubmc-developer", "openubmc-developer"),
    ("openubmc-build", "openubmc-build"),
    ("openubmc-upgrade", "openubmc-upgrade"),
    ("openubmc-live-patch", "openubmc-live-patch"),
    ("openubmc-dt-testing", "testing"),
    ("openubmc-publish", "openubmc-publish"),
    ("openubmc-lua-component", "lua-component"),
    ("openubmc-qemu-testing", "qemu-testing"),
)
DEFAULT_SKILL_PROFILE = "full"
TARGET_RUNTIME_SKILL_PROFILE = "target-runtime"
TARGET_RUNTIME_SKILL_NAMES = frozenset(
    {
        "openubmc-environment-setup",
        "openubmc-debug",
        "openubmc-log-analyzer",
        "openubmc-developer",
        "openubmc-build",
        "openubmc-upgrade",
        "openubmc-live-patch",
    }
)
TARGET_RUNTIME_SKILL_BUNDLE: SkillBundle = tuple(
    item for item in SKILL_BUNDLE if item[0] in TARGET_RUNTIME_SKILL_NAMES
)
SKILL_PROFILES: dict[str, SkillProfilePolicy] = {
    DEFAULT_SKILL_PROFILE: SkillProfilePolicy(
        bundle=SKILL_BUNDLE,
        manages_knowledge_mcp=True,
    ),
    TARGET_RUNTIME_SKILL_PROFILE: SkillProfilePolicy(
        bundle=TARGET_RUNTIME_SKILL_BUNDLE,
        manages_knowledge_mcp=False,
    ),
}

# Compatibility wrappers remain in the repository for explicit path-based use,
# but must not be present in the normal discovery catalog.
RETIRED_SKILL_LINKS: tuple[tuple[str, str], ...] = (
    ("lua-component", "lua-component"),
    ("openubmc-mdb-interface-dev", "mdb-interface-dev"),
    ("mdb-interface-dev", "mdb-interface-dev"),
    ("openubmc-interface-mapping", "interface-mapping"),
    ("interface-mapping", "interface-mapping"),
    ("openubmc-debugging", "openubmc-debugging"),
)

CLIENTS = ("codex", "claude", "openclaw")
SUPPORTED_MCP_CLIENTS = ("codex", "claude")
REQUIRED_TOOLS = ("bmcgo", "conan", "git", "python3", "ssh")
CONDITIONAL_TOOLS = {
    "sshpass": (
        "password-based SSH, remote log pulling, and Live Patch require sshpass; "
        "key-based SSH remains available"
    ),
}
RECOMMENDED_TOOLS = {
    "rg": "source evidence search uses a slower fallback when ripgrep is unavailable",
}
CLIENT_EXECUTABLES = {
    "codex": "codex",
    "claude": "claude",
    "openclaw": "openclaw",
}
APT_TOOL_PACKAGES = {
    "git": "git",
    "ssh": "openssh-client",
    "sshpass": "sshpass",
    "rg": "ripgrep",
}
CODEX_NPM_PACKAGE = "@openai/codex"
BMCGO_WHEEL_NAME = "hw_ibmc_bmcgo-0.7.51-py3-none-any.whl"
BMCGO_WHEEL_SHA256 = "d8424a2e8a4549ffd5d574288ed7d9016b91b2ae0d5c2463387102250795ae1e"
CREDENTIAL_KEY_ORDER = (
    "OPENUBMC_SSH_USER",
    "OPENUBMC_SSH_PASSWORD",
    "OPENUBMC_TELNET_USER",
    "OPENUBMC_TELNET_PASSWORD",
    "REDFISH_USERNAME",
    "REDFISH_PASSWORD",
    "OPENUBMC_OS_SSH_USER",
    "OPENUBMC_OS_SSH_PASSWORD",
    "OPENUBMC_OS_SSH_PORT",
)
ALLOWED_CREDENTIAL_KEYS = frozenset(CREDENTIAL_KEY_ORDER)
REQUIRED_CREDENTIAL_KEYS = (
    "OPENUBMC_SSH_USER",
    "OPENUBMC_SSH_PASSWORD",
    "REDFISH_USERNAME",
    "REDFISH_PASSWORD",
    "OPENUBMC_OS_SSH_USER",
    "OPENUBMC_OS_SSH_PASSWORD",
)

MARKER_START = "# >>> openUBMC environment setup >>>"
MARKER_END = "# <<< openUBMC environment setup <<<"
OLD_MARKER_START = "# >>> openUBMC environment >>>"
OLD_MARKER_END = "# <<< openUBMC environment <<<"
LEGACY_CREDENTIALS_START = "# >>> openUBMC debug credentials >>>"
LEGACY_CREDENTIALS_END = "# <<< openUBMC debug credentials <<<"

PROFILE_BLOCK = f'''{MARKER_START}
if [ -r "${{XDG_CONFIG_HOME:-$HOME/.config}}/openubmc/env.sh" ]; then
    . "${{XDG_CONFIG_HOME:-$HOME/.config}}/openubmc/env.sh"
fi
{MARKER_END}
'''


class SetupError(RuntimeError):
    """A configuration error safe to show to the user."""


def http_mcp_entry(url: str) -> HttpMcpEntry:
    return {"type": "http", "url": url}


def stdio_mcp_entry(command: str | Path) -> StdioMcpEntry:
    return {"type": "stdio", "command": str(command), "args": []}


def record_created_entry(record: Mapping[str, object] | None) -> bool:
    return record is not None and record.get("created_entry") is True


def record_created_file(record: Mapping[str, object] | None) -> bool:
    return record is not None and record.get("created_file") is True


def valid_client_ownership_record(record: object) -> bool:
    return (
        isinstance(record, Mapping)
        and isinstance(record.get("created_entry"), bool)
        and isinstance(record.get("created_file"), bool)
    )


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--skip-tool-install",
        action="store_true",
        help="do not automatically install missing workflow tools",
    )


def add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", type=Path, help="existing skills repository checkout")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="skills Git repository")
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help="release tag or full commit for a managed checkout",
    )
    parser.add_argument(
        "--source-mode",
        choices=("auto", "linked", "managed"),
        default="auto",
        help="link a local checkout or use an installer-managed clone",
    )


def add_install_options(parser: argparse.ArgumentParser) -> None:
    add_common_options(parser)
    add_source_options(parser)
    parser.add_argument(
        "--clients",
        default="auto",
        help="comma-separated clients: auto, codex, claude, openclaw, or all",
    )
    parser.add_argument("--target", choices=("current", "docker"), default="current")
    parser.add_argument(
        "--skill-profile",
        choices=tuple(SKILL_PROFILES),
        default=None,
        help="Skill link set to manage; existing installs keep their recorded profile",
    )
    parser.add_argument(
        "--preserve-skills",
        default=None,
        help=(
            "comma-separated canonical Skill names whose existing links must be "
            "retained across a source switch; use none to clear the recorded list"
        ),
    )
    parser.add_argument(
        "--kb-url",
        "--studio-url",
        dest="knowledge_url",
        default=None,
        help="knowledge MCP URL; --studio-url remains a compatibility alias",
    )
    parser.add_argument(
        "--kb-config",
        type=Path,
        help="import a private openUBMC KB JSON configuration",
    )
    parser.add_argument("--configure-credentials", action="store_true")
    parser.add_argument("--import-credentials", type=Path)
    parser.add_argument("--skip-credentials", action="store_true")


def new_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_install_options(subparsers.add_parser("install", help="install or refresh configuration"))
    check = subparsers.add_parser("check", help="inspect the installed workflow")
    add_common_options(check)
    check.add_argument("--deep", action="store_true", help="include a full-worktree dirty check")
    for command, help_text in (
        ("repair", "repair configuration without pulling source"),
        ("update", "fast-forward an installer-managed source"),
        ("rollback", "restore the previous known-good managed revision"),
        ("refresh", "record and repair a linked source checkout"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        add_common_options(child)
    credentials = subparsers.add_parser("credentials", help="configure private BMC and OS credentials")
    add_common_options(credentials)
    credentials.add_argument("--import-credentials", type=Path)
    credentials.add_argument("--kb", action="store_true", help="configure openUBMC KB OneID credentials")
    credentials.add_argument("--kb-config", type=Path, help="import a private openUBMC KB JSON configuration")
    uninstall = subparsers.add_parser("uninstall", help="remove installer-managed configuration")
    add_common_options(uninstall)
    uninstall.add_argument("--purge-credentials", action="store_true")
    return parser


def legacy_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    for command in ("install", "check", "repair", "update", "rollback", "refresh", "uninstall"):
        actions.add_argument(f"--{command}", action="store_true")
    add_install_options(parser)
    parser.add_argument("--purge-credentials", action="store_true")
    parser.add_argument("--deep", action="store_true")
    return parser


def apply_argument_defaults(args: argparse.Namespace) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "source": None,
        "repo_url": DEFAULT_REPO_URL,
        "ref": DEFAULT_REF,
        "source_mode": "auto",
        "clients": "auto",
        "target": "current",
        "skill_profile": None,
        "preserve_skills": None,
        "knowledge_url": None,
        "kb_config": None,
        "kb": False,
        "configure_credentials": False,
        "import_credentials": None,
        "skip_credentials": False,
        "purge_credentials": False,
        "non_interactive": False,
        "skip_tool_install": False,
        "dry_run": False,
        "json": False,
        "deep": False,
    }
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    if args.command == "credentials":
        args.configure_credentials = True
    return args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in COMMANDS:
        parser = new_cli_parser()
        args = parser.parse_args(raw)
        args.legacy_cli = False
    else:
        parser = legacy_cli_parser()
        args = parser.parse_args(raw)
        args.command = next(
            (command for command in COMMANDS if getattr(args, command, False)),
            "install",
        )
        args.legacy_cli = True
    args = apply_argument_defaults(args)
    args.ref_explicit = any(
        argument == "--ref" or argument.startswith("--ref=") for argument in raw
    )
    if args.import_credentials and args.skip_credentials:
        parser.error("--import-credentials and --skip-credentials are mutually exclusive")
    return args


def config_root(home: Path) -> Path:
    override = os.environ.get("XDG_CONFIG_HOME", "")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else path.absolute()
    return home / ".config"


def openubmc_config_dir(home: Path) -> Path:
    return config_root(home) / "openubmc"


def state_path(home: Path) -> Path:
    return openubmc_config_dir(home) / "environment-state.json"


def credentials_path(home: Path) -> Path:
    return openubmc_config_dir(home) / "credentials.env"


def managed_source_dir(home: Path) -> Path:
    return home / ".local" / "share" / "openubmc" / "skills"


def runtime_install_root(home: Path) -> Path:
    return home / ".local" / "share" / "openubmc" / "target-runtime"


def runtime_package_path(home: Path) -> Path:
    return runtime_install_root(home) / "openubmc_target_runtime"


def runtime_launcher_path(home: Path) -> Path:
    return runtime_install_root(home) / "openubmc-target-runtime-mcp"


def runtime_manifest_path(home: Path) -> Path:
    return runtime_install_root(home) / "manifest.json"


def knowledge_install_root(home: Path) -> Path:
    return home / ".local" / "share" / "openubmc" / "kb-mcp"


def knowledge_launcher_path(home: Path) -> Path:
    return knowledge_install_root(home) / "openubmc-kb-mcp"


def knowledge_manifest_path(home: Path) -> Path:
    return knowledge_install_root(home) / "manifest.json"


def knowledge_config_path(home: Path) -> Path:
    return openubmc_config_dir(home) / "kb-mcp.json"


def client_skills_dir(home: Path, client: str) -> Path:
    if client == "codex":
        return home / ".agents" / "skills"
    if client == "claude":
        return home / ".claude" / "skills"
    if client == "openclaw":
        return home / ".openclaw" / "skills"
    raise SetupError(f"unsupported client: {client}")


def detected_clients(home: Path) -> list[str]:
    detected = ["codex"]
    if (home / ".claude").exists() or (home / ".claude.json").exists() or shutil.which("claude"):
        detected.append("claude")
    if (home / ".openclaw").exists() or shutil.which("openclaw"):
        detected.append("openclaw")
    return detected


def parse_clients(value: str, home: Path) -> list[str]:
    if value == "auto":
        return detected_clients(home)
    if value == "all":
        return list(CLIENTS)
    clients = []
    for item in value.split(","):
        client = item.strip().lower()
        if not client:
            continue
        if client not in CLIENTS:
            raise SetupError(f"unsupported client: {client}")
        if client not in clients:
            clients.append(client)
    if "codex" not in clients:
        clients.insert(0, "codex")
    return clients


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise SetupError(f"required command is unavailable: {command[0]}") from error


def git_output(root: Path, *arguments: str) -> str:
    result = run_command(["git", "-C", str(root), *arguments])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise SetupError(detail)
    return result.stdout.strip()


def git_commit(root: Path) -> str:
    try:
        return git_output(root, "rev-parse", "HEAD")
    except SetupError:
        return "unversioned"


def resolve_skill_profile(profile: str) -> ResolvedSkillProfile:
    try:
        policy = SKILL_PROFILES[profile]
    except KeyError as error:
        raise SetupError(f"unsupported Skill profile: {profile}") from error
    return ResolvedSkillProfile(
        name=profile,
        bundle=policy.bundle,
        manages_knowledge_mcp=policy.manages_knowledge_mcp,
    )


def parse_preserved_skills(
    value: object,
    bundle: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise SetupError("preserve_skills must be a comma-separated string")
    names = tuple(
        dict.fromkeys(
            item.strip()
            for item in value.split(",")
            if item.strip()
        )
    )
    if not names or names == ("none",):
        return ()
    if "none" in names:
        raise SetupError("preserve_skills cannot mix none with Skill names")
    available = {canonical for canonical, _relative in bundle}
    unknown = sorted(set(names) - available)
    if unknown:
        raise SetupError(
            "unknown preserved Skill name(s): " + ", ".join(unknown)
        )
    return names


def skill_profile_from_state(state: Mapping[str, object]) -> ResolvedSkillProfile:
    value = state.get("skill_profile", DEFAULT_SKILL_PROFILE)
    if not isinstance(value, str) or value not in SKILL_PROFILES:
        raise SetupError(f"unsupported Skill profile in installer state: {value!r}")
    return resolve_skill_profile(value)


def materialize_skill_bundle(
    bundle: Iterable[tuple[str, str]],
) -> SkillBundle:
    resolved = tuple(bundle)
    if not resolved:
        raise SetupError("Skill bundle must not be empty")
    return resolved


def bundle_git_paths(
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> tuple[str, ...]:
    resolved = materialize_skill_bundle(bundle)
    paths = [relative for _, relative in resolved]
    paths.append("openubmc-target-runtime")
    if resolved == SKILL_BUNDLE:
        paths.append("openubmc-kb-mcp")
    return tuple(dict.fromkeys(paths))


def git_dirty(root: Path, *, paths: Iterable[str] | None = None) -> bool:
    scoped_paths = tuple(paths or ())
    if not scoped_paths:
        try:
            return bool(git_output(root, "status", "--porcelain"))
        except SetupError:
            return False
    tracked = run_command(
        ["git", "-C", str(root), "diff-index", "--quiet", "HEAD", "--", *scoped_paths]
    )
    if tracked.returncode == 1:
        return True
    if tracked.returncode != 0:
        return False
    untracked = run_command(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *scoped_paths,
        ]
    )
    return untracked.returncode == 0 and bool(untracked.stdout.strip())


def normalized_repo_url(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")


def read_skill_name(skill_file: Path) -> str:
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SetupError(f"missing YAML frontmatter: {skill_file}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SetupError(f"unterminated YAML frontmatter: {skill_file}")
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            return value.strip().strip("\"'")
    raise SetupError(f"missing Skill name: {skill_file}")


def validate_source(
    root: Path,
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> Path:
    resolved_bundle = materialize_skill_bundle(bundle)
    root = root.expanduser().absolute()
    if not root.is_dir():
        raise SetupError(f"skills source does not exist: {root}")
    errors = []
    for canonical, relative in resolved_bundle:
        skill_file = root / relative / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"missing {relative}/SKILL.md")
            continue
        actual = read_skill_name(skill_file)
        if actual != canonical:
            errors.append(f"{relative}/SKILL.md declares {actual!r}, expected {canonical!r}")
    if errors:
        raise SetupError("invalid skills source: " + "; ".join(errors))
    return root


def validate_release_source(root: Path, dry_run: bool) -> None:
    validator = root / "scripts" / "validate_workflow.py"
    if not validator.is_file():
        raise SetupError(f"release validator is missing: {validator}")
    if dry_run:
        print(f"would validate release contract in {root}")
        return
    result = run_command(
        [sys.executable, str(validator), "--release-contract-only"],
        cwd=root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SetupError(detail or "release contract validation failed")


def release_ref_kind(value: object) -> Literal["tag", "commit"]:
    candidate = str(value).strip() if value is not None else ""
    lowered = candidate.lower()
    if FULL_COMMIT.fullmatch(candidate):
        return "commit"
    if (
        not candidate
        or lowered in MUTABLE_REFS
        or lowered.startswith("refs/heads/")
        or candidate.startswith("-")
        or candidate.endswith(("/", ".", ".lock"))
        or ".." in candidate
        or "@{" in candidate
        or any(character.isspace() or character in "~^:?*[\\" for character in candidate)
    ):
        raise SetupError("--ref must name an explicit release tag or full commit")
    return "tag"


def iter_runtime_source_files(package_root: Path):
    root = package_root.resolve()
    if not root.is_dir() or not (root / "__init__.py").is_file():
        raise SetupError(f"canonical Target Runtime is unavailable: {root}")
    found = False
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise SetupError(
                f"Target Runtime source must not contain symbolic links: {relative}"
            )
        if path.is_file():
            found = True
            yield path, relative
    if not found:
        raise SetupError(f"Target Runtime package contains no Python sources: {root}")


def runtime_content_digest(package_root: Path) -> str:
    digest = hashlib.sha256(_RUNTIME_DIGEST_DOMAIN)
    for path, relative in iter_runtime_source_files(package_root):
        content = path.read_bytes()
        encoded_path = relative.as_posix().encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def read_runtime_api_version(package_root: Path) -> str:
    contracts = package_root.resolve() / "contracts.py"
    try:
        tree = ast.parse(contracts.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise SetupError(f"Target Runtime API metadata is unavailable: {contracts}") from error
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Name) and target.id == "RUNTIME_API_VERSION"
            for target in targets
        ):
            return value.value
    raise SetupError(f"Target Runtime API constant is missing: {contracts}")


def build_runtime_plan(
    home: Path,
    source: Path,
    *,
    allow_missing_source: bool = False,
) -> dict[str, str]:
    source_package = source / "openubmc-target-runtime" / "openubmc_target_runtime"
    mcp_entrypoint = source / "openubmc-debug" / "scripts" / "target_runtime_mcp.py"
    if allow_missing_source and not source.exists():
        api_version = TARGET_RUNTIME_API_VERSION
        content_digest = "planned"
    else:
        api_version = read_runtime_api_version(source_package)
        if api_version != TARGET_RUNTIME_API_VERSION:
            raise SetupError(
                "canonical Target Runtime API mismatch: "
                f"expected {TARGET_RUNTIME_API_VERSION}, found {api_version}"
            )
        content_digest = runtime_content_digest(source_package)
        if not mcp_entrypoint.is_file():
            raise SetupError(f"Target Runtime MCP entrypoint is missing: {mcp_entrypoint}")
    return {
        "schema_version": TARGET_RUNTIME_INSTALL_SCHEMA,
        "api_version": api_version,
        "content_digest": content_digest,
        "source_package_path": str(source_package),
        "package_path": str(runtime_package_path(home)),
        "launcher_path": str(runtime_launcher_path(home)),
        "manifest_path": str(runtime_manifest_path(home)),
        "mcp_entrypoint": str(mcp_entrypoint),
    }


def render_runtime_launcher(plan: dict[str, str]) -> str:
    package = json.dumps(plan["package_path"])
    entrypoint = json.dumps(plan["mcp_entrypoint"])
    expected_api = json.dumps(plan["api_version"])
    expected_digest = json.dumps(plan["content_digest"])
    return f'''#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import runpy
import sys

PACKAGE_ROOT = Path({package})
MCP_ENTRYPOINT = Path({entrypoint})
EXPECTED_API = {expected_api}
EXPECTED_DIGEST = {expected_digest}
DIGEST_DOMAIN = b"openubmc-target-runtime-content-v1\\0"


def fail(reason: str) -> None:
    raise SystemExit(
        "Target Runtime installation validation failed before remote execution: "
        + reason
        + "; run openubmc-environment-setup repair"
    )


def runtime_api() -> str:
    contracts = PACKAGE_ROOT / "contracts.py"
    try:
        tree = ast.parse(contracts.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        fail("Runtime API metadata is missing or invalid")
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "RUNTIME_API_VERSION" for target in targets):
            return value.value
    fail("Runtime API constant is missing")


def runtime_digest() -> str:
    if not PACKAGE_ROOT.is_dir() or not (PACKAGE_ROOT / "__init__.py").is_file():
        fail("Runtime package is missing")
    digest = hashlib.sha256(DIGEST_DOMAIN)
    files = [
        path for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.relative_to(PACKAGE_ROOT).parts
    ]
    if not files:
        fail("Runtime package contains no Python sources")
    for path in files:
        if path.is_symlink() or not path.is_file():
            fail("Runtime package contains an invalid source path")
        relative = path.relative_to(PACKAGE_ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


if runtime_api() != EXPECTED_API:
    fail("Runtime API mismatch")
if runtime_digest() != EXPECTED_DIGEST:
    fail("Runtime content digest mismatch")
if not MCP_ENTRYPOINT.is_file():
    fail("MCP entrypoint is missing")

config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
credentials = config_root / "openubmc" / "credentials.env"
if credentials.is_file():
    os.environ.setdefault("OPENUBMC_CREDENTIALS_FILE", str(credentials))
sys.path.insert(0, str(PACKAGE_ROOT.parent))
sys.path.insert(0, str(MCP_ENTRYPOINT.parent))
runpy.run_path(str(MCP_ENTRYPOINT), run_name="__main__")
'''


def deploy_runtime(plan: dict[str, str], dry_run: bool) -> dict[str, str]:
    source_package = Path(plan["source_package_path"])
    package = Path(plan["package_path"])
    launcher = Path(plan["launcher_path"])
    manifest = Path(plan["manifest_path"])
    if dry_run:
        print(f"would deploy Target Runtime to {package}")
        print(f"would write Target Runtime MCP launcher {launcher}")
        return dict(plan)

    root = package.parent
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise SetupError(f"Target Runtime install root must be a real directory: {root}")
    if package.is_symlink() or (package.exists() and not package.is_dir()):
        raise SetupError(f"Target Runtime package path must be a real directory: {package}")
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    staged_root = Path(tempfile.mkdtemp(prefix=".runtime-", dir=root))
    staged_package = staged_root / package.name
    try:
        for source_file, relative in iter_runtime_source_files(source_package):
            destination = staged_package / relative
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
        staged_digest = runtime_content_digest(staged_package)
        if staged_digest != plan["content_digest"]:
            raise SetupError("copied Target Runtime content digest changed during install")
        if package.exists():
            shutil.rmtree(package)
        os.replace(staged_package, package)
    finally:
        shutil.rmtree(staged_root, ignore_errors=True)

    atomic_write(
        manifest,
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        0o600,
    )
    atomic_write(launcher, render_runtime_launcher(plan), 0o755)
    return dict(plan)


def iter_knowledge_source_files(root: Path):
    package_root = root.resolve()
    required = (package_root / "package.json", package_root / "package-lock.json")
    if not all(path.is_file() for path in required) or not (package_root / "src/server.js").is_file():
        raise SetupError(f"canonical openUBMC KB MCP is unavailable: {package_root}")
    paths = [*required, *sorted((package_root / "src").rglob("*.js"))]
    for path in paths:
        relative = path.relative_to(package_root)
        if path.is_symlink() or not path.is_file():
            raise SetupError(f"openUBMC KB MCP contains an invalid source path: {relative}")
        yield path, relative


def knowledge_content_digest(root: Path) -> str:
    digest = hashlib.sha256(_KNOWLEDGE_DIGEST_DOMAIN)
    for path, relative in iter_knowledge_source_files(root):
        content = path.read_bytes()
        encoded_path = relative.as_posix().encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def node_major(node: Path | str) -> int:
    result = run_command([str(node), "--version"])
    if result.returncode != 0:
        return 0
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){1,2}", result.stdout.strip())
    return int(match.group(1)) if match else 0


def resolve_node(home: Path, tool_dirs: Iterable[str]) -> Path | None:
    candidates = [home / ".local" / "bin" / "node"]
    discovered = shutil.which("node", path=tool_search_path(tool_dirs))
    if discovered:
        candidates.append(Path(discovered))
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def install_knowledge_dependencies(
    home: Path,
    source: Path,
    tool_dirs: Iterable[str],
    *,
    dry_run: bool,
) -> Path:
    package_root = source / "openubmc-kb-mcp"
    if dry_run and not source.exists():
        print("would install openUBMC KB MCP Node.js dependencies")
        return home / ".local" / "bin" / "node"
    tuple(iter_knowledge_source_files(package_root))
    node = resolve_node(home, tool_dirs)
    if node is None:
        raise SetupError("Node.js is unavailable after dependency installation")
    if node_major(node) < 20:
        npm = shutil.which("npm", path=tool_search_path(tool_dirs))
        if not npm:
            raise SetupError("Node.js 20 or newer is required and npm is unavailable")
        if dry_run:
            print(f"would install Node.js 20 under {home / '.local'}")
            return home / ".local" / "bin" / "node"
        command = [
            npm,
            "install",
            "--global",
            "--prefix",
            str(home / ".local"),
            "node@20",
        ]
        result = run_command(command, env={**os.environ, "HOME": str(home)})
        if result.returncode != 0:
            raise command_error(command, result)
        node = resolve_node(home, tool_dirs)
        if node is None or node_major(node) < 20:
            raise SetupError("automatic Node.js 20 installation did not produce a usable runtime")
        print(f"installed Node.js 20 under {home / '.local'}")
    dependency = package_root / "node_modules" / "@modelcontextprotocol" / "sdk" / "package.json"
    if dependency.is_file():
        return node
    if dry_run:
        print(f"would install openUBMC KB MCP dependencies in {package_root}")
        return node
    npm = shutil.which("npm", path=tool_search_path(tool_dirs))
    if not npm:
        raise SetupError("npm is unavailable after dependency installation")
    command = [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"]
    result = run_command(command, cwd=package_root, env={**os.environ, "HOME": str(home)})
    if result.returncode != 0:
        raise command_error(command, result)
    if not dependency.is_file():
        raise SetupError("openUBMC KB MCP dependency installation is incomplete")
    print("installed openUBMC KB MCP dependencies")
    return node


def build_knowledge_plan(
    home: Path,
    source: Path,
    node: Path,
    *,
    allow_missing_source: bool = False,
) -> dict[str, str]:
    package_root = source / "openubmc-kb-mcp"
    digest = "planned" if allow_missing_source and not source.exists() else knowledge_content_digest(package_root)
    return {
        "schema_version": KNOWLEDGE_MCP_INSTALL_SCHEMA,
        "version": KNOWLEDGE_MCP_VERSION,
        "content_digest": digest,
        "source_path": str(package_root),
        "server_path": str(package_root / "src" / "server.js"),
        "node_path": str(node),
        "config_path": str(knowledge_config_path(home)),
        "launcher_path": str(knowledge_launcher_path(home)),
        "manifest_path": str(knowledge_manifest_path(home)),
    }


def render_knowledge_launcher(plan: Mapping[str, str]) -> str:
    source = json.dumps(plan["source_path"])
    server = json.dumps(plan["server_path"])
    node = json.dumps(plan["node_path"])
    config = json.dumps(plan["config_path"])
    expected_digest = json.dumps(plan["content_digest"])
    return f'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
from pathlib import Path

SOURCE_ROOT = Path({source})
SERVER = Path({server})
NODE = Path({node})
CONFIG = Path({config})
EXPECTED_DIGEST = {expected_digest}
DIGEST_DOMAIN = b"openubmc-kb-content-v1\\0"


def fail(reason: str) -> None:
    raise SystemExit(
        "openUBMC KB MCP installation validation failed: "
        + reason
        + "; run openubmc-environment-setup repair"
    )


files = [SOURCE_ROOT / "package.json", SOURCE_ROOT / "package-lock.json", *sorted((SOURCE_ROOT / "src").rglob("*.js"))]
if not files or any(path.is_symlink() or not path.is_file() for path in files):
    fail("source files are missing or invalid")
digest = hashlib.sha256(DIGEST_DOMAIN)
for path in files:
    relative = path.relative_to(SOURCE_ROOT).as_posix().encode("utf-8")
    content = path.read_bytes()
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
if "sha256:" + digest.hexdigest() != EXPECTED_DIGEST:
    fail("content digest mismatch")
if not NODE.is_file() or not os.access(NODE, os.X_OK):
    fail("Node.js runtime is missing")
os.environ.setdefault("OPENUBMC_KB_CONFIG", str(CONFIG))
os.execv(str(NODE), [str(NODE), str(SERVER), "--config", str(CONFIG)])
'''


def deploy_knowledge_mcp(plan: dict[str, str], dry_run: bool) -> dict[str, str]:
    launcher = Path(plan["launcher_path"])
    manifest = Path(plan["manifest_path"])
    if dry_run:
        print(f"would write openUBMC KB MCP launcher {launcher}")
        return dict(plan)
    launcher.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    atomic_write(
        manifest,
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        0o600,
    )
    atomic_write(launcher, render_knowledge_launcher(plan), 0o755)
    return dict(plan)


def default_knowledge_config() -> dict[str, object]:
    return {"username": "", "password": ""}


def ensure_knowledge_config(home: Path, source: Path | None, dry_run: bool) -> str:
    destination = knowledge_config_path(home)
    configured_source = source
    if configured_source is None:
        environment_path = os.environ.get("OPENUBMC_KB_CONFIG", "").strip()
        configured_source = Path(environment_path).expanduser() if environment_path else None
    if configured_source is not None:
        configured_source = configured_source.expanduser().absolute()
        if configured_source.is_symlink() or not configured_source.is_file():
            raise SetupError(f"openUBMC KB configuration must be a regular file: {configured_source}")
        try:
            document = json.loads(configured_source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SetupError("openUBMC KB configuration must contain valid JSON") from error
        if not isinstance(document, dict):
            raise SetupError("openUBMC KB configuration must be a JSON object")
        if dry_run:
            print(f"would import openUBMC KB configuration to {destination}")
        else:
            atomic_write(
                destination,
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                0o600,
            )
        return "imported"
    if destination.is_file() and not destination.is_symlink():
        if not dry_run and stat.S_IMODE(destination.stat().st_mode) != 0o600:
            os.chmod(destination, 0o600)
        return "preserved"
    if destination.exists():
        raise SetupError(f"openUBMC KB configuration path is not a regular file: {destination}")
    if dry_run:
        print(f"would create openUBMC KB configuration {destination}")
    else:
        atomic_write(
            destination,
            json.dumps(default_knowledge_config(), ensure_ascii=False, indent=2) + "\n",
            0o600,
        )
    return "created"


def local_repository_from_script(
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    try:
        return validate_source(candidate, bundle)
    except SetupError:
        return None


def clone_source(
    destination: Path,
    repo_url: str,
    ref: str,
    dry_run: bool,
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> Path:
    ref_kind = release_ref_kind(ref)
    if dry_run:
        print(f"would clone {repo_url}@{ref} to {destination}")
        return destination
    if destination.exists():
        raise SetupError(f"managed source destination already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    initialized = run_command(["git", "init", "--quiet", str(destination)])
    if initialized.returncode != 0:
        raise SetupError(initialized.stderr.strip() or "unable to initialize skills repository")
    try:
        remote = run_command(
            ["git", "-C", str(destination), "remote", "add", "origin", repo_url]
        )
        if remote.returncode != 0:
            raise SetupError(remote.stderr.strip() or "unable to configure skills repository")
        fetch_ref = ref if ref_kind == "commit" else f"refs/tags/{ref}"
        fetched = run_command(
            [
                "git",
                "-C",
                str(destination),
                "fetch",
                "--no-tags",
                "--depth",
                "1",
                "origin",
                fetch_ref,
            ]
        )
        if fetched.returncode != 0:
            label = "full commit" if ref_kind == "commit" else "release tag"
            detail = fetched.stderr.strip() or fetched.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise SetupError(f"unable to fetch {label} {ref}{suffix}")
        resolved = git_output(destination, "rev-parse", "FETCH_HEAD^{commit}")
        checkout = run_command(
            ["git", "-C", str(destination), "checkout", "--detach", resolved]
        )
        if checkout.returncode != 0:
            raise SetupError(
                checkout.stderr.strip() or f"unable to checkout release revision {resolved}"
            )
        return validate_source(destination, bundle)
    except (OSError, SetupError):
        shutil.rmtree(destination, ignore_errors=True)
        raise


def update_managed_source(
    root: Path,
    repo_url: str,
    ref: str,
    dry_run: bool,
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> None:
    remote = git_output(root, "remote", "get-url", "origin")
    if normalized_repo_url(remote) != normalized_repo_url(repo_url):
        raise SetupError(f"managed source origin differs from configured repository: {remote}")
    if git_dirty(root, paths=bundle_git_paths(bundle)):
        raise SetupError(f"refusing to update dirty skills checkout: {root}")
    if dry_run:
        print(f"would fast-forward {root} from origin/{ref}")
        return
    fetch = run_command(["git", "-C", str(root), "fetch", "origin", ref])
    if fetch.returncode != 0:
        raise SetupError(fetch.stderr.strip() or "unable to fetch skills repository")
    checkout = run_command(["git", "-C", str(root), "checkout", ref])
    if checkout.returncode != 0:
        raise SetupError(checkout.stderr.strip() or f"unable to checkout {ref}")
    merge = run_command(["git", "-C", str(root), "merge", "--ff-only", f"origin/{ref}"])
    if merge.returncode != 0:
        raise SetupError(merge.stderr.strip() or "unable to fast-forward skills repository")


def checkout_managed_release(
    root: Path,
    repo_url: str,
    ref: str,
    dry_run: bool,
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> str:
    ref_kind = release_ref_kind(ref)
    remote = git_output(root, "remote", "get-url", "origin")
    if normalized_repo_url(remote) != normalized_repo_url(repo_url):
        raise SetupError(f"managed source origin differs from configured repository: {remote}")
    if git_dirty(root, paths=bundle_git_paths(bundle)):
        raise SetupError(f"refusing to update dirty skills checkout: {root}")
    if dry_run:
        print(f"would checkout immutable release {repo_url}@{ref} in {root}")
        return "planned"
    fetch_ref = ref if ref_kind == "commit" else f"refs/tags/{ref}"
    fetched = run_command(
        [
            "git",
            "-C",
            str(root),
            "fetch",
            "--no-tags",
            "--depth",
            "1",
            "origin",
            fetch_ref,
        ]
    )
    if fetched.returncode != 0:
        label = "full commit" if ref_kind == "commit" else "release tag"
        detail = fetched.stderr.strip() or fetched.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise SetupError(f"unable to fetch {label} {ref}{suffix}")
    resolved = git_output(root, "rev-parse", "FETCH_HEAD^{commit}")
    checkout = run_command(["git", "-C", str(root), "checkout", "--detach", resolved])
    if checkout.returncode != 0:
        raise SetupError(
            checkout.stderr.strip() or f"unable to checkout release revision {resolved}"
        )
    return resolved


def checkout_managed_revision(
    root: Path,
    commit: str,
    dry_run: bool,
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise SetupError("recorded rollback revision is invalid")
    if git_dirty(root, paths=bundle_git_paths(bundle)):
        raise SetupError(f"refusing to roll back dirty skills checkout: {root}")
    verify = run_command(["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"])
    if verify.returncode != 0:
        raise SetupError(f"recorded rollback revision is unavailable: {commit}")
    if dry_run:
        print(f"would restore managed source {root} to {commit}")
        return
    checkout = run_command(["git", "-C", str(root), "checkout", "--detach", commit])
    if checkout.returncode != 0:
        raise SetupError(checkout.stderr.strip() or f"unable to restore revision {commit}")


def source_mode_from_state(state: Mapping[str, object]) -> str:
    mode = state.get("source_mode")
    if isinstance(mode, str) and mode in {"linked", "managed"}:
        return mode
    if mode is not None:
        raise SetupError(f"unsupported source mode in installer state: {mode!r}")
    return "managed" if state.get("managed_checkout") is True else "linked"


def ref_kind_from_state(state: Mapping[str, object], source_mode: str) -> str:
    value = state.get("ref_kind")
    if value is None:
        return "legacy-branch" if source_mode == "managed" else "linked"
    if isinstance(value, str) and value in {"tag", "commit", "legacy-branch", "linked"}:
        return value
    raise SetupError(f"unsupported ref kind in installer state: {value!r}")


def resolve_source(
    args: argparse.Namespace,
    *,
    update: bool = False,
    bundle: Iterable[tuple[str, str]] = SKILL_BUNDLE,
) -> tuple[Path, str]:
    resolved_bundle = materialize_skill_bundle(bundle)
    requested_mode = args.source_mode
    if requested_mode == "managed" and args.source:
        raise SetupError("--source cannot be combined with --source-mode managed")
    if args.source:
        root = validate_source(args.source, resolved_bundle)
        return root, "linked"
    if requested_mode != "managed":
        local = local_repository_from_script(resolved_bundle)
        if local is not None:
            return local, "linked"
        if requested_mode == "linked":
            raise SetupError("linked source mode requires --source or a valid local repository")
    if not args.ref_explicit:
        raise SetupError("managed installation requires --ref with a release tag or full commit")
    release_ref_kind(args.ref)
    destination = managed_source_dir(args.home)
    if destination.exists():
        root = validate_source(destination, resolved_bundle)
        checkout_managed_release(
            root,
            args.repo_url,
            args.ref,
            args.dry_run,
            bundle=resolved_bundle,
        )
        return root, "managed"
    return (
        clone_source(
            destination,
            args.repo_url,
            args.ref,
            args.dry_run,
            resolved_bundle,
        ),
        "managed",
    )


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    target_mode = existing_mode if mode is None else mode
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(temporary, target_mode)
        os.replace(temporary, path)
        os.chmod(path, target_mode)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def backup_path(home: Path) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return openubmc_config_dir(home) / "backups" / timestamp


def backup_file(path: Path, root: Path, dry_run: bool) -> None:
    if not path.exists() or path.is_symlink():
        return
    relative = Path(str(path).lstrip(os.sep).replace(":", "_"))
    destination = root / relative
    if dry_run:
        print(f"would back up {path} to {destination}")
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(path, destination)


def same_target(link: Path, target: Path) -> bool:
    if not link.is_symlink():
        return False
    return Path(os.path.realpath(link)) == Path(os.path.realpath(target))


def remaining_links_into_source(
    home: Path,
    clients: Iterable[str],
    source: Path,
    managed_links: Iterable[str],
) -> list[Path]:
    source_root = Path(os.path.realpath(source))
    planned_removals = {Path(path) for path in managed_links}
    consumers: list[Path] = []
    for client in clients:
        if client not in CLIENTS:
            continue
        skills_root = client_skills_dir(home, client)
        if not skills_root.is_dir() or skills_root.is_symlink():
            continue
        for link in skills_root.iterdir():
            if link in planned_removals or not link.is_symlink():
                continue
            target = Path(os.path.realpath(link))
            try:
                target.relative_to(source_root)
            except ValueError:
                continue
            consumers.append(link)
    return sorted(consumers)


def ensure_link(link: Path, target: Path, backups: Path, dry_run: bool) -> None:
    if same_target(link, target):
        return
    if link.exists() or link.is_symlink():
        if link.is_symlink():
            if dry_run:
                print(f"would replace stale link {link}")
            else:
                link.unlink()
        else:
            backup_file(link, backups, dry_run)
            if dry_run:
                print(f"would replace conflicting path {link} after backup")
            else:
                if link.is_dir():
                    shutil.rmtree(link)
                else:
                    link.unlink()
    if dry_run:
        print(f"would link {link} -> {target}")
        return
    link.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)


def valid_preserved_skill_target(target: Path) -> bool:
    return target.is_dir() and (target / "SKILL.md").is_file()


def current_skill_link_target(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    target = Path(os.path.realpath(link))
    return target if valid_preserved_skill_target(target) else None


def resolve_preserved_link_targets(
    home: Path,
    clients: Iterable[str],
    preserved_skills: Iterable[str],
    recorded_links: Mapping[str, str] | None = None,
    *,
    prefer_recorded: bool = False,
) -> dict[str, Path]:
    preserved = set(preserved_skills)
    if not preserved:
        return {}
    recorded_links = recorded_links or {}
    targets: dict[str, Path] = {}
    for client in clients:
        destination_root = client_skills_dir(home, client)
        for canonical in sorted(preserved):
            link = destination_root / canonical
            current = current_skill_link_target(link)
            recorded_value = recorded_links.get(str(link), "")
            recorded_candidate = Path(recorded_value) if recorded_value else None
            recorded = (
                recorded_candidate
                if recorded_candidate is not None
                and valid_preserved_skill_target(recorded_candidate)
                else None
            )
            target = recorded if prefer_recorded else (current or recorded)
            if target is None:
                raise SetupError(
                    f"preserved Skill target is unavailable for {canonical}: {link}"
                )
            targets[str(link)] = target
    return targets


def install_links(
    home: Path,
    source: Path,
    clients: Iterable[str],
    backups: Path,
    dry_run: bool,
    bundle: tuple[tuple[str, str], ...] = SKILL_BUNDLE,
    preserved_targets: Mapping[str, Path] | None = None,
) -> dict[str, str]:
    managed: dict[str, str] = {}
    preserved_targets = preserved_targets or {}
    for client in clients:
        destination_root = client_skills_dir(home, client)
        for retired_name, relative in RETIRED_SKILL_LINKS:
            retired_link = destination_root / retired_name
            retired_target = source / relative
            if same_target(retired_link, retired_target):
                if dry_run:
                    print(f"would remove retired Skill link {retired_link}")
                else:
                    retired_link.unlink()
        for canonical, relative in bundle:
            link = destination_root / canonical
            target = preserved_targets.get(str(link), source / relative)
            ensure_link(link, target, backups, dry_run)
            managed[str(link)] = str(target)
        legacy = destination_root / "openubmc-environment"
        if legacy.is_symlink():
            if dry_run:
                print(f"would remove legacy Skill link {legacy}")
            else:
                legacy.unlink()
    return managed


def _remove_marked_block(text: str, start: str, end: str) -> str:
    pattern = re.compile(
        rf"(?:^|\n){re.escape(start)}\n.*?{re.escape(end)}\n?", re.DOTALL
    )
    return pattern.sub("\n", text)


def replace_profile_hook(text: str) -> str:
    cleaned = text
    for start, end in (
        (MARKER_START, MARKER_END),
        (OLD_MARKER_START, OLD_MARKER_END),
        (LEGACY_CREDENTIALS_START, LEGACY_CREDENTIALS_END),
    ):
        cleaned = _remove_marked_block(cleaned, start, end)
    cleaned = cleaned.strip()
    return f"{cleaned}\n\n{PROFILE_BLOCK}" if cleaned else PROFILE_BLOCK


def remove_profile_hook(text: str) -> str:
    cleaned = text
    for start, end in (
        (MARKER_START, MARKER_END),
        (OLD_MARKER_START, OLD_MARKER_END),
        (LEGACY_CREDENTIALS_START, LEGACY_CREDENTIALS_END),
    ):
        cleaned = _remove_marked_block(cleaned, start, end)
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


def profile_paths(home: Path) -> list[Path]:
    paths = [home / ".bashrc", home / ".profile"]
    for bash_login in (home / ".bash_profile", home / ".bash_login"):
        if bash_login.exists():
            paths.append(bash_login)
    shell = os.environ.get("SHELL", "")
    if shell.endswith("zsh") or (home / ".zshrc").exists() or (home / ".zprofile").exists():
        paths.extend((home / ".zshrc", home / ".zprofile"))
    return paths


def private_file_shell_function() -> str:
    return r'''_openubmc_private_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ -r "$1" ] || return 1
    _openubmc_meta="$(stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1" 2>/dev/null || true)"
    _openubmc_uid="${_openubmc_meta%% *}"
    _openubmc_mode="${_openubmc_meta#* }"
    [ "${_openubmc_uid}" = "$(id -u)" ] || return 1
    case "${_openubmc_mode}" in
        600) return 0 ;;
    esac
    return 1
}
'''


def render_env(tool_dirs: Iterable[str]) -> str:
    directories = [item for item in tool_dirs if item]
    lines = [
        "# Managed by openubmc-environment-setup. Safe to source repeatedly.",
        "",
        "# Removed compatibility variables must not leak from an older login shell.",
        "unset OPENUBMC_BUILD_SKILL_ROOT OPENUBMC_DEBUG_SKILL_ROOT OPENUBMC_UPGRADE_SKILL_ROOT",
        "",
    ]
    if directories:
        joined = ":".join(shlex.quote(item) for item in directories)
        lines.extend((f"export PATH={joined}:\"$PATH\"", ""))
    lines.append(private_file_shell_function().rstrip())
    lines.extend(
        (
            "",
            '_openubmc_credentials="${XDG_CONFIG_HOME:-$HOME/.config}/openubmc/credentials.env"',
            'if _openubmc_private_file "${_openubmc_credentials}"; then',
            '    export OPENUBMC_CREDENTIALS_FILE="${_openubmc_credentials}"',
            'elif [ "${OPENUBMC_CREDENTIALS_FILE:-}" = "${_openubmc_credentials}" ]; then',
            "    unset OPENUBMC_CREDENTIALS_FILE",
            "fi",
            '_openubmc_kb_config="${XDG_CONFIG_HOME:-$HOME/.config}/openubmc/kb-mcp.json"',
            'if _openubmc_private_file "${_openubmc_kb_config}"; then',
            '    export OPENUBMC_KB_CONFIG="${_openubmc_kb_config}"',
            'elif [ "${OPENUBMC_KB_CONFIG:-}" = "${_openubmc_kb_config}" ]; then',
            "    unset OPENUBMC_KB_CONFIG",
            "fi",
            "unset _openubmc_credentials _openubmc_kb_config _openubmc_meta _openubmc_uid _openubmc_mode",
            "unset -f _openubmc_private_file 2>/dev/null || true",
            "",
        )
    )
    return "\n".join(lines)


def tool_search_path(tool_dirs: Iterable[str]) -> str:
    entries = [item for item in tool_dirs if item]
    current = os.environ.get("PATH", "")
    if current:
        entries.append(current)
    return os.pathsep.join(entries)


def user_tool_bin(home: Path) -> Path:
    return home / ".local" / "bin"


def command_error(command: list[str], result: subprocess.CompletedProcess[str]) -> SetupError:
    detail = result.stderr.strip() or result.stdout.strip() or "command failed"
    return SetupError(f"{' '.join(command)} failed: {detail}")


def privileged_command(command: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return command
    sudo = shutil.which("sudo")
    if not sudo:
        raise SetupError("automatic system package installation requires root or sudo")
    return [sudo, "-n", *command]


def install_apt_packages(packages: Iterable[str], *, dry_run: bool) -> None:
    selected = sorted(set(packages))
    if not selected:
        return
    if not shutil.which("apt-get"):
        raise SetupError(
            "automatic dependency installation currently requires apt-get"
        )
    if dry_run:
        print("would install system packages: " + ", ".join(selected))
        return
    environment = dict(os.environ)
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    update = privileged_command(["apt-get", "update"])
    result = run_command(update, env=environment)
    if result.returncode != 0:
        raise command_error(update, result)
    install = privileged_command(
        ["apt-get", "install", "-y", "--no-install-recommends", *selected]
    )
    result = run_command(install, env=environment)
    if result.returncode != 0:
        raise command_error(install, result)
    print("installed system packages: " + ", ".join(selected))


def python_pip_available() -> bool:
    result = run_command([sys.executable, "-m", "pip", "--version"])
    return result.returncode == 0


def discover_bmcgo_wheel(source: Path) -> Path:
    configured = os.environ.get("OPENUBMC_BMCGO_PACKAGE", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        source / "openubmc-environment-setup" / "assets" / BMCGO_WHEEL_NAME,
        Path(__file__).resolve().parents[1] / "assets" / BMCGO_WHEEL_NAME,
        Path("/home/workspace/tool") / BMCGO_WHEEL_NAME,
    ]
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != BMCGO_WHEEL_SHA256:
            raise SetupError(f"bmcgo package digest mismatch: {candidate}")
        return candidate
    raise SetupError("bundled bmcgo package is unavailable")


def install_python_workflow_tools(
    home: Path,
    source: Path,
    tool_dirs: Iterable[str],
    *,
    dry_run: bool,
) -> None:
    search_path = tool_search_path(tool_dirs)
    missing_bmcgo = shutil.which("bmcgo", path=search_path) is None
    missing_conan = shutil.which("conan", path=search_path) is None
    if not missing_bmcgo and not missing_conan:
        return
    packages: list[str] = []
    if missing_bmcgo:
        packages.append(
            BMCGO_WHEEL_NAME
            if dry_run and not source.exists()
            else str(discover_bmcgo_wheel(source))
        )
    if missing_conan:
        packages.append("conan")
    if dry_run:
        names = [Path(item).name if item.endswith(".whl") else item for item in packages]
        print("would install Python workflow packages: " + ", ".join(names))
        return
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["PYTHONUSERBASE"] = str(home / ".local")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--upgrade",
        "--disable-pip-version-check",
        "--break-system-packages",
        *packages,
    ]
    result = run_command(command, env=environment)
    if (
        result.returncode != 0
        and "no such option: --break-system-packages"
        in (result.stderr + result.stdout).lower()
    ):
        command = [item for item in command if item != "--break-system-packages"]
        result = run_command(command, env=environment)
    if result.returncode != 0:
        raise command_error(command, result)
    print("installed Python workflow packages")


def install_codex_client(
    home: Path,
    tool_dirs: Iterable[str],
    *,
    dry_run: bool,
) -> None:
    if shutil.which("codex", path=tool_search_path(tool_dirs)):
        return
    prefix = home / ".local"
    if dry_run:
        print(f"would install Codex under {prefix}")
        return
    npm = shutil.which("npm")
    if not npm:
        raise SetupError("npm is unavailable after system dependency installation")
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    command = [
        npm,
        "install",
        "--global",
        "--prefix",
        str(prefix),
        CODEX_NPM_PACKAGE,
    ]
    result = run_command(command, env=environment)
    if result.returncode != 0:
        raise command_error(command, result)
    print(f"installed Codex under {prefix}")


def install_bootstrap_tools(
    home: Path,
    clients: Iterable[str],
    tool_dirs: Iterable[str],
    *,
    dry_run: bool,
    knowledge_mcp: bool = False,
) -> None:
    search_path = tool_search_path(tool_dirs)
    apt_packages = [
        package
        for tool, package in APT_TOOL_PACKAGES.items()
        if shutil.which(tool, path=search_path) is None
    ]
    if (
        shutil.which("bmcgo", path=search_path) is None
        or shutil.which("conan", path=search_path) is None
    ) and not python_pip_available():
        apt_packages.append("python3-pip")
    if "codex" in set(clients) and shutil.which("codex", path=search_path) is None:
        if shutil.which("npm", path=search_path) is None:
            apt_packages.extend(("nodejs", "npm"))
    if knowledge_mcp:
        if shutil.which("node", path=search_path) is None:
            apt_packages.append("nodejs")
        if shutil.which("npm", path=search_path) is None:
            apt_packages.append("npm")
    install_apt_packages(apt_packages, dry_run=dry_run)
    if "codex" in set(clients):
        install_codex_client(home, tool_dirs, dry_run=dry_run)


def inspect_tooling(
    tool_dirs: Iterable[str], clients: Iterable[str]
) -> dict[str, Any]:
    search_path = tool_search_path(tool_dirs)
    selected_clients = set(clients)

    def availability(tools: Iterable[str]) -> dict[str, bool]:
        return {
            tool: shutil.which(tool, path=search_path) is not None
            for tool in tools
        }

    required = availability(REQUIRED_TOOLS)
    conditional = availability(CONDITIONAL_TOOLS)
    recommended = availability(RECOMMENDED_TOOLS)
    client_tools = {
        client: shutil.which(executable, path=search_path) is not None
        for client, executable in CLIENT_EXECUTABLES.items()
        if client in selected_clients
    }
    return {
        "ready": all(required.values()),
        "client_ready": all(client_tools.values()),
        "required": required,
        "conditional": conditional,
        "recommended": recommended,
        "clients": client_tools,
    }


def tooling_next_actions(
    tooling: Mapping[str, object], *, credentials_ok: bool
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    repairable: list[str] = []
    if not credentials_ok:
        actions.append(
            {
                "code": "configure_credentials",
                "detail": (
                    "run python3 \"$HOME/.agents/skills/"
                    "openubmc-environment-setup/scripts/install_environment.py\" "
                    "credentials"
                ),
            }
        )
    for tool, available in dict(tooling.get("required", {})).items():
        if available is not True:
            repairable.append(str(tool))
    for tool, available in dict(tooling.get("conditional", {})).items():
        if available is not True:
            repairable.append(str(tool))
    for tool, available in dict(tooling.get("recommended", {})).items():
        if available is not True:
            repairable.append(str(tool))
    for client, available in dict(tooling.get("clients", {})).items():
        if available is not True and client == "codex":
            repairable.append("codex")
        elif available is not True:
            executable = CLIENT_EXECUTABLES.get(str(client), str(client))
            actions.append(
                {
                    "code": "install_external_client",
                    "client": str(client),
                    "detail": (
                        f"install {executable} to launch {client} directly in this "
                        "environment; Skill and MCP configuration is already staged"
                    ),
                }
            )
    if repairable:
        actions.append(
            {
                "code": "repair_tooling",
                "tools": ",".join(sorted(set(repairable))),
                "detail": (
                    "run python3 \"$HOME/.agents/skills/"
                    "openubmc-environment-setup/scripts/install_environment.py\" "
                    "repair --non-interactive; the installer will add the tools "
                    "automatically"
                ),
            }
        )
    return actions


def knowledge_next_actions(
    report: Mapping[str, object]
) -> list[dict[str, str]]:
    if report.get("managed") is not True or report.get("healthy") is True:
        return []
    return [
        {
            "code": "configure_openubmc_kb",
            "detail": (
                "configure a standalone openubmc-kb stdio MCP entry or make the "
                "recorded HTTP endpoint available"
            ),
        }
    ]


def resolve_tool_dirs(
    non_interactive: bool, existing_dirs: Iterable[str] = ()
) -> tuple[list[str], list[str]]:
    del non_interactive
    tool_dirs = [
        item for item in dict.fromkeys(existing_dirs) if item and Path(item).is_dir()
    ]
    missing: list[str] = []
    for tool in REQUIRED_TOOLS:
        found = shutil.which(tool, path=tool_search_path(tool_dirs))
        if found:
            continue
        missing.append(tool)
    return tool_dirs, missing


def install_environment_files(
    home: Path, tool_dirs: Iterable[str], backups: Path, dry_run: bool
) -> list[str]:
    config_dir = openubmc_config_dir(home)
    env_file = config_dir / "env.sh"
    profiles = profile_paths(home)
    for path in (env_file, *profiles):
        if path.is_symlink():
            raise SetupError(f"refusing to replace symbolic link: {path}")
    if dry_run:
        print(f"would write {env_file}")
    else:
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(config_dir, 0o700)
        atomic_write(env_file, render_env(tool_dirs), 0o600)
    for profile in profiles:
        original = profile.read_text(encoding="utf-8", errors="ignore") if profile.exists() else ""
        updated = replace_profile_hook(original)
        if updated == original:
            continue
        backup_file(profile, backups, dry_run)
        if dry_run:
            print(f"would update {profile}")
        else:
            atomic_write(profile, updated, None if profile.exists() else 0o644)
    return [str(path) for path in profiles]


def validate_openubmc_config_dir(home: Path) -> None:
    config_dir = openubmc_config_dir(home)
    if config_dir.is_symlink() or (config_dir.exists() and not config_dir.is_dir()):
        raise SetupError(f"configuration directory must be a real directory: {config_dir}")
    if config_dir.exists():
        metadata = config_dir.stat()
        if metadata.st_uid != os.getuid():
            raise SetupError(f"configuration directory has the wrong owner: {config_dir}")


def validate_environment_paths(home: Path) -> None:
    validate_openubmc_config_dir(home)
    env_file = openubmc_config_dir(home) / "env.sh"
    for path in (state_path(home), env_file, *profile_paths(home)):
        if path.is_symlink():
            raise SetupError(f"refusing to replace symbolic link: {path}")
        if path.exists() and not path.is_file():
            raise SetupError(f"managed file path is not a regular file: {path}")


def parse_credentials_value(value: str, *, line_number: int) -> str:
    cooked = value.strip()
    if "\0" in cooked:
        raise SetupError(f"invalid credential value on line {line_number}")
    if not cooked:
        return ""
    if cooked[0] in {"'", '"'}:
        if len(cooked) < 2 or cooked[-1] != cooked[0]:
            raise SetupError(f"malformed quoted credential on line {line_number}")
        return cooked[1:-1]
    if cooked[-1] in {"'", '"'}:
        raise SetupError(f"malformed quoted credential on line {line_number}")
    return cooked


def normalize_credentials(
    values: dict[str, str], *, require_complete: bool = False
) -> dict[str, str]:
    normalized = dict(values)
    bmc_user = normalized.get("OPENUBMC_SSH_USER", "")
    redfish_user = normalized.get("REDFISH_USERNAME", "")
    if bmc_user and redfish_user and bmc_user != redfish_user:
        raise SetupError("BMC SSH and Redfish usernames must match")
    shared_user = bmc_user or redfish_user
    if shared_user:
        normalized["OPENUBMC_SSH_USER"] = shared_user
        normalized["REDFISH_USERNAME"] = shared_user

    bmc_password = normalized.get("OPENUBMC_SSH_PASSWORD", "")
    redfish_password = normalized.get("REDFISH_PASSWORD", "")
    if bmc_password and redfish_password and bmc_password != redfish_password:
        raise SetupError("BMC SSH and Redfish passwords must match")
    shared_password = bmc_password or redfish_password
    if shared_password:
        normalized["OPENUBMC_SSH_PASSWORD"] = shared_password
        normalized["REDFISH_PASSWORD"] = shared_password

    if require_complete:
        missing = [key for key in REQUIRED_CREDENTIAL_KEYS if not normalized.get(key)]
        if missing:
            raise SetupError("credentials are missing: " + ", ".join(missing))
    return normalized


def parse_credentials(
    content: str, *, require_complete: bool = False
) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = raw.partition("=")
        key = key.strip()
        if not separator or key not in ALLOWED_CREDENTIAL_KEYS:
            raise SetupError(f"unsupported credential key on line {number}")
        value = parse_credentials_value(raw_value, line_number=number)
        if key in values and values[key] != value:
            raise SetupError(f"conflicting credential key: {key}")
        values[key] = value
    return normalize_credentials(values, require_complete=require_complete)


def read_credentials_file(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise SetupError(f"credentials must be a regular non-symlink file: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.getuid():
        raise SetupError(f"credentials file has the wrong owner: {path}")
    try:
        return parse_credentials(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise SetupError(f"unable to read credentials: {path}") from error


def credentials_status(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    try:
        values = read_credentials_file(path)
    except SetupError as error:
        return False, str(error)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        return False, f"permissions are {mode:04o}, expected 0600"
    missing = [key for key in REQUIRED_CREDENTIAL_KEYS if not values.get(key)]
    if missing:
        return False, "missing keys: " + ", ".join(missing)
    return True, "configured"


def render_credentials(values: dict[str, str]) -> str:
    normalized = normalize_credentials(values)
    return "".join(
        f"{key}={normalized[key]}\n"
        for key in CREDENTIAL_KEY_ORDER
        if key in normalized
    )


def write_credentials_file(path: Path, values: dict[str, str], dry_run: bool) -> None:
    normalized = normalize_credentials(values, require_complete=True)
    content = render_credentials(normalized)
    parse_credentials(content, require_complete=True)
    if dry_run:
        print(f"would write private credentials to {path}")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    atomic_write(path, content, 0o600)


def prompt_missing_credentials(existing: dict[str, str]) -> dict[str, str]:
    values = normalize_credentials(existing)
    if not values.get("OPENUBMC_SSH_USER") or not values.get("OPENUBMC_SSH_PASSWORD"):
        bmc_user = input("BMC SSH/Redfish username: ").strip()
        bmc_password = getpass.getpass("BMC SSH/Redfish password: ")
        values["OPENUBMC_SSH_USER"] = bmc_user
        values["OPENUBMC_SSH_PASSWORD"] = bmc_password
        values["REDFISH_USERNAME"] = bmc_user
        values["REDFISH_PASSWORD"] = bmc_password
    if not values.get("OPENUBMC_OS_SSH_USER") or not values.get("OPENUBMC_OS_SSH_PASSWORD"):
        values["OPENUBMC_OS_SSH_USER"] = input("OS SSH username: ").strip()
        values["OPENUBMC_OS_SSH_PASSWORD"] = getpass.getpass("OS SSH password: ")
    return normalize_credentials(values, require_complete=True)


def prepare_credentials(
    args: argparse.Namespace, *, repair_only: bool = False
) -> dict[str, Any]:
    destination = credentials_path(args.home)
    if args.skip_credentials:
        if repair_only:
            if not destination.exists():
                return {"destination": destination, "result": "missing"}
            values = read_credentials_file(destination)
            missing = [key for key in REQUIRED_CREDENTIAL_KEYS if not values.get(key)]
            return {
                "destination": destination,
                "result": "missing" if missing else "repaired",
                "chmod": stat.S_IMODE(destination.stat().st_mode) != 0o600,
            }
        return {"destination": destination, "result": "skipped"}
    existing: dict[str, str] = {}
    chmod_needed = False
    if destination.exists():
        existing = read_credentials_file(destination)
        chmod_needed = stat.S_IMODE(destination.stat().st_mode) != 0o600
    if args.import_credentials:
        source = args.import_credentials.expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise SetupError(f"credential import must be a regular non-symlink file: {source}")
        metadata = source.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise SetupError("credential import must be owned by the current user with mode 0600")
        values = dict(existing)
        values.update(parse_credentials(source.read_text(encoding="utf-8")))
        values = normalize_credentials(values, require_complete=True)
        return {
            "destination": destination,
            "result": "imported",
            "values": values,
        }
    missing = [key for key in REQUIRED_CREDENTIAL_KEYS if not existing.get(key)]
    if not missing:
        return {
            "destination": destination,
            "result": "preserved",
            "chmod": chmod_needed,
        }
    if repair_only:
        return {
            "destination": destination,
            "result": "missing",
            "chmod": chmod_needed,
        }
    if args.non_interactive or not sys.stdin.isatty():
        if args.configure_credentials:
            raise SetupError("credential configuration requires a TTY or --import-credentials")
        return {
            "destination": destination,
            "result": "missing",
            "chmod": chmod_needed,
        }
    if args.dry_run:
        return {
            "destination": destination,
            "result": "planned",
            "missing_count": len(missing),
            "chmod": chmod_needed,
        }
    values = prompt_missing_credentials(existing)
    return {
        "destination": destination,
        "result": "configured",
        "values": values,
    }


def apply_credentials_plan(plan: dict[str, Any], dry_run: bool) -> str:
    destination = Path(plan["destination"])
    values = plan.get("values")
    if isinstance(values, dict):
        write_credentials_file(destination, values, dry_run)
    elif plan.get("chmod"):
        if dry_run:
            print(f"would set mode 0600 on {destination}")
        else:
            os.chmod(destination, 0o600)
    if plan.get("result") == "planned":
        print(
            f"would request {plan.get('missing_count', 0)} missing credential fields "
            "through hidden TTY input"
        )
    return str(plan["result"])


def configure_credentials(args: argparse.Namespace, *, repair_only: bool = False) -> str:
    return apply_credentials_plan(
        prepare_credentials(args, repair_only=repair_only), args.dry_run
    )


def backup_config_file(path: Path, backups: Path, dry_run: bool) -> None:
    backup_file(path, backups, dry_run)


def migrate_legacy_toml_mcp_name(
    path: Path,
    backups: Path,
    dry_run: bool,
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    original = path.read_text(encoding="utf-8", errors="strict")
    lines = original.splitlines()
    legacy_header = f"[mcp_servers.{LEGACY_STUDIO_MCP_NAME}]"
    current_header = f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]"
    legacy_bounds = toml_section_bounds(lines, legacy_header)
    if legacy_bounds is None:
        return False
    if toml_section_bounds(lines, current_header) is not None:
        raise SetupError(
            f"both {LEGACY_STUDIO_MCP_NAME} and {KNOWLEDGE_MCP_NAME} MCP entries "
            f"exist in {path}"
        )
    lines[legacy_bounds[0]] = current_header
    updated = "\n".join(lines).rstrip() + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(
            f"would rename {LEGACY_STUDIO_MCP_NAME} to "
            f"{KNOWLEDGE_MCP_NAME} in {path}"
        )
    else:
        atomic_write(path, updated, None)
    return True


def migrate_legacy_json_mcp_name(
    path: Path,
    backups: Path,
    dry_run: bool,
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid JSON client configuration: {path}") from error
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or LEGACY_STUDIO_MCP_NAME not in servers:
        return False
    if KNOWLEDGE_MCP_NAME in servers:
        raise SetupError(
            f"both {LEGACY_STUDIO_MCP_NAME} and {KNOWLEDGE_MCP_NAME} MCP entries "
            f"exist in {path}"
        )
    servers[KNOWLEDGE_MCP_NAME] = servers.pop(LEGACY_STUDIO_MCP_NAME)
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(
            f"would rename {LEGACY_STUDIO_MCP_NAME} to "
            f"{KNOWLEDGE_MCP_NAME} in {path}"
        )
    else:
        atomic_write(path, updated, None)
    return True


def migrate_legacy_mcp_names(
    home: Path,
    clients: Iterable[str],
    backups: Path,
    dry_run: bool,
) -> None:
    for client in clients:
        if client == "codex":
            migrate_legacy_toml_mcp_name(
                home / ".codex" / "config.toml",
                backups,
                dry_run,
            )
        elif client == "claude":
            migrate_legacy_json_mcp_name(
                home / ".claude.json",
                backups,
                dry_run,
            )


def toml_section_bounds(lines: list[str], header: str) -> tuple[int, int] | None:
    matches = [index for index, line in enumerate(lines) if line.strip() == header]
    if len(matches) > 1:
        raise SetupError(f"duplicate TOML section: {header}")
    if not matches:
        return None
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.fullmatch(r"\s*\[+[^]]+]\]?\s*", lines[index]):
            end = index
            break
    return start, end


def toml_knowledge_mcp_entry(path: Path) -> dict[str, str] | None:
    if not path.is_file() or path.is_symlink():
        return None
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    bounds = toml_section_bounds(lines, f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]")
    if bounds is None:
        return None
    start, end = bounds
    matches = [
        re.fullmatch(r"\s*url\s*=\s*(['\"])(.*?)\1\s*", lines[index])
        for index in range(start + 1, end)
    ]
    urls = [match.group(2) for match in matches if match]
    commands = [
        match.group(2)
        for index in range(start + 1, end)
        if (
            match := re.fullmatch(
                r"\s*command\s*=\s*(['\"])(.*?)\1\s*", lines[index]
            )
        )
    ]
    if len(urls) == 1 and not commands:
        return {"transport": "http", "url": urls[0]}
    if len(commands) == 1 and not urls:
        return {"transport": "stdio", "command": commands[0]}
    raise SetupError(
        f"{KNOWLEDGE_MCP_NAME} TOML section must contain one string URL or one string command"
    )


def toml_string_array_field(
    lines: list[str],
    start: int,
    end: int,
    name: str,
) -> list[str] | None:
    assignment = re.compile(rf"\s*{re.escape(name)}\s*=\s*(.*)")
    for index in range(start + 1, end):
        match = assignment.fullmatch(lines[index])
        if match is None:
            continue
        raw = match.group(1).strip()
        while raw.count("[") > raw.count("]") and index + 1 < end:
            index += 1
            raw += "\n" + lines[index]
        try:
            parsed = ast.literal_eval(raw)
        except (SyntaxError, ValueError) as error:
            raise SetupError(
                f"{KNOWLEDGE_MCP_NAME} TOML {name} must be a string array"
            ) from error
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise SetupError(
                f"{KNOWLEDGE_MCP_NAME} TOML {name} must be a string array"
            )
        return parsed
    return None


def is_known_legacy_knowledge_stdio(
    command: object,
    args: object,
) -> bool:
    command_name = Path(str(command)).name.casefold()
    if command_name not in {"node", "node.exe"}:
        return False
    if not isinstance(args, list) or not all(
        isinstance(item, str) for item in args
    ):
        return False
    return any(
        item.replace("\\", "/").casefold().endswith(
            "/openubmc-standalone-mcp/src/server.js"
        )
        for item in args
    )


def toml_has_known_legacy_knowledge_stdio(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    bounds = toml_section_bounds(lines, f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]")
    if bounds is None:
        return False
    entry = toml_knowledge_mcp_entry(path)
    if entry is None or entry.get("transport") != "stdio":
        return False
    args = toml_string_array_field(lines, bounds[0], bounds[1], "args")
    return is_known_legacy_knowledge_stdio(entry.get("command"), args)


def toml_mcp_url(path: Path) -> str | None:
    entry = toml_knowledge_mcp_entry(path)
    if entry is None or entry.get("transport") != "http":
        return None
    return entry["url"]


def upsert_toml_mcp(
    path: Path,
    url: str,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    file_existed = path.exists()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    header = f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]"
    bounds = toml_section_bounds(lines, header)
    created_entry = record_created_entry(prior)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((header, f"url = {json.dumps(url)}"))
        created_entry = True
    else:
        start, end = bounds
        existing = toml_knowledge_mcp_entry(path)
        if existing is not None and existing.get("transport") == "stdio":
            if not created_entry:
                return {
                    "path": str(path),
                    "ownership": "external",
                    "transport": "stdio",
                    "created_entry": False,
                    "created_file": False,
                }
        current = existing.get("url") if existing is not None else None
        if existing is None or existing.get("transport") != "http" or current != url:
            if not created_entry:
                raise SetupError(
                    f"existing {KNOWLEDGE_MCP_NAME} MCP URL differs in {path}: {current!r}"
                )
            lines[start:end] = (header, f"url = {json.dumps(url)}")
    updated = "\n".join(lines).rstrip() + "\n"
    if updated != original:
        backup_config_file(path, backups, dry_run)
        if dry_run:
            print(f"would register {KNOWLEDGE_MCP_NAME} in {path}")
        else:
            atomic_write(path, updated, None)
    return {
        "path": str(path),
        "url": url,
        "created_entry": created_entry,
        "created_file": record_created_file(prior) or not file_existed,
    }


def remove_toml_mcp(
    path: Path, record: dict[str, Any], backups: Path, dry_run: bool
) -> None:
    if not record_created_entry(record) or not path.exists() or path.is_symlink():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    header = f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]"
    bounds = toml_section_bounds(lines, header)
    if bounds is None:
        return
    if toml_mcp_url(path) != record.get("url"):
        print(f"warning: preserving changed {KNOWLEDGE_MCP_NAME} MCP entry in {path}")
        return
    start, end = bounds
    updated_lines = lines[:start] + lines[end:]
    updated = "\n".join(updated_lines).strip()
    updated = updated + "\n" if updated else ""
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would remove {KNOWLEDGE_MCP_NAME} from {path}")
    elif not updated and record_created_file(record):
        path.unlink()
    else:
        atomic_write(path, updated, None)


def json_mcp_entry(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid JSON client configuration: {path}") from error
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"mcpServers must be an object: {path}")
    entry = servers.get(KNOWLEDGE_MCP_NAME)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise SetupError(f"{KNOWLEDGE_MCP_NAME} MCP entry must be an object: {path}")
    return entry


def upsert_json_mcp(
    path: Path,
    url: str,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    file_existed = path.exists()
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SetupError(f"invalid JSON client configuration: {path}") from error
    else:
        document = {}
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"mcpServers must be an object: {path}")
    existing = servers.get(KNOWLEDGE_MCP_NAME)
    created_entry = record_created_entry(prior)
    if existing is None:
        servers[KNOWLEDGE_MCP_NAME] = http_mcp_entry(url)
        created_entry = True
    elif isinstance(existing, dict) and existing.get("url") == url:
        return {
            "path": str(path),
            "url": url,
            "created_entry": created_entry,
            "created_file": record_created_file(prior),
        }
    elif created_entry:
        servers[KNOWLEDGE_MCP_NAME] = http_mcp_entry(url)
    elif not isinstance(existing, dict):
        raise SetupError(f"{KNOWLEDGE_MCP_NAME} MCP entry must be an object: {path}")
    else:
        raise SetupError(
            f"existing {KNOWLEDGE_MCP_NAME} MCP URL differs in {path}: {existing.get('url')!r}"
        )
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would register {KNOWLEDGE_MCP_NAME} in {path}")
    else:
        atomic_write(path, updated, None)
    return {
        "path": str(path),
        "url": url,
        "created_entry": created_entry,
        "created_file": record_created_file(prior) or not file_existed,
    }


def remove_json_mcp(
    path: Path, record: dict[str, Any], backups: Path, dry_run: bool
) -> None:
    if not record_created_entry(record) or not path.exists() or path.is_symlink():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or KNOWLEDGE_MCP_NAME not in servers:
        return
    entry = servers.get(KNOWLEDGE_MCP_NAME)
    if not isinstance(entry, dict) or entry.get("url") != record.get("url"):
        print(f"warning: preserving changed {KNOWLEDGE_MCP_NAME} MCP entry in {path}")
        return
    servers.pop(KNOWLEDGE_MCP_NAME, None)
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would remove {KNOWLEDGE_MCP_NAME} from {path}")
    elif record_created_file(record) and document == {"mcpServers": {}}:
        path.unlink()
    else:
        atomic_write(path, updated, None)


def remove_toml_knowledge_mcp(
    path: Path, record: dict[str, Any], backups: Path, dry_run: bool
) -> None:
    if not record_created_entry(record) or not path.exists() or path.is_symlink():
        return
    try:
        current = toml_knowledge_mcp_entry(path)
    except SetupError:
        current = None
    expected = (
        {"transport": "stdio", "command": record.get("command")}
        if record.get("command")
        else {"transport": "http", "url": record.get("url")}
    )
    if current != expected:
        print(f"warning: preserving changed {KNOWLEDGE_MCP_NAME} MCP entry in {path}")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    bounds = toml_section_bounds(lines, f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]")
    if bounds is None:
        return
    start, end = bounds
    updated = "\n".join(lines[:start] + lines[end:]).strip()
    updated = updated + "\n" if updated else ""
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would remove {KNOWLEDGE_MCP_NAME} from {path}")
    elif not updated and record_created_file(record):
        path.unlink()
    else:
        atomic_write(path, updated, None)


def remove_json_knowledge_mcp(
    path: Path, record: dict[str, Any], backups: Path, dry_run: bool
) -> None:
    if not record_created_entry(record) or not path.exists() or path.is_symlink():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return
    expected = (
        stdio_mcp_entry(str(record.get("command")))
        if record.get("command")
        else http_mcp_entry(str(record.get("url")))
    )
    if servers.get(KNOWLEDGE_MCP_NAME) != expected:
        print(f"warning: preserving changed {KNOWLEDGE_MCP_NAME} MCP entry in {path}")
        return
    servers.pop(KNOWLEDGE_MCP_NAME, None)
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would remove {KNOWLEDGE_MCP_NAME} from {path}")
    elif record_created_file(record) and document == {"mcpServers": {}}:
        path.unlink()
    else:
        atomic_write(path, updated, None)


def toml_stdio_mcp_entry(path: Path) -> dict[str, object] | None:
    if not path.is_file() or path.is_symlink():
        return None
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    header = f"[mcp_servers.{TARGET_RUNTIME_MCP_NAME}]"
    bounds = toml_section_bounds(lines, header)
    if bounds is None:
        return None
    start, end = bounds
    command_matches = [
        match
        for index in range(start + 1, end)
        if (
            match := re.fullmatch(
                r"\s*command\s*=\s*(['\"])(.*?)\1\s*", lines[index]
            )
        )
    ]
    args_matches = [
        line
        for line in lines[start + 1 : end]
        if re.fullmatch(r"\s*args\s*=\s*\[\s*]\s*", line)
    ]
    if len(command_matches) != 1 or len(args_matches) != 1:
        raise SetupError(
            f"{TARGET_RUNTIME_MCP_NAME} TOML section must contain one command and args = []"
        )
    return stdio_mcp_entry(command_matches[0].group(2))


def upsert_toml_stdio_mcp(
    path: Path,
    launcher: Path,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    file_existed = path.exists()
    original = path.read_text(encoding="utf-8") if file_existed else ""
    lines = original.splitlines()
    header = f"[mcp_servers.{TARGET_RUNTIME_MCP_NAME}]"
    bounds = toml_section_bounds(lines, header)
    expected = stdio_mcp_entry(launcher)
    created_entry = record_created_entry(prior)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((header, f"command = {json.dumps(str(launcher))}", "args = []"))
        created_entry = True
    else:
        current = toml_stdio_mcp_entry(path)
        if current == expected:
            return {
                "path": str(path),
                "command": str(launcher),
                "args": [],
                "created_entry": created_entry,
                "created_file": record_created_file(prior),
            }
        if not created_entry:
            raise SetupError(
                f"existing {TARGET_RUNTIME_MCP_NAME} MCP command differs in {path}"
            )
        start, end = bounds
        lines[start:end] = (
            header,
            f"command = {json.dumps(str(launcher))}",
            "args = []",
        )
    updated = "\n".join(lines).rstrip() + "\n"
    if updated != original:
        backup_config_file(path, backups, dry_run)
        if dry_run:
            print(f"would register {TARGET_RUNTIME_MCP_NAME} in {path}")
        else:
            atomic_write(path, updated, None)
    return {
        "path": str(path),
        "command": str(launcher),
        "args": [],
        "created_entry": created_entry,
        "created_file": record_created_file(prior) or not file_existed,
    }


def remove_toml_stdio_mcp(
    path: Path, record: dict[str, Any], backups: Path, dry_run: bool
) -> None:
    if not record_created_entry(record) or not path.exists() or path.is_symlink():
        return
    try:
        current = toml_stdio_mcp_entry(path)
    except SetupError:
        current = None
    expected = {
        "type": "stdio",
        "command": record.get("command"),
        "args": record.get("args", []),
    }
    if current != expected:
        print(f"warning: preserving changed {TARGET_RUNTIME_MCP_NAME} MCP entry in {path}")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    header = f"[mcp_servers.{TARGET_RUNTIME_MCP_NAME}]"
    bounds = toml_section_bounds(lines, header)
    if bounds is None:
        return
    start, end = bounds
    updated_lines = lines[:start] + lines[end:]
    updated = "\n".join(updated_lines).strip()
    updated = updated + "\n" if updated else ""
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would remove {TARGET_RUNTIME_MCP_NAME} from {path}")
    elif not updated and record_created_file(record):
        path.unlink()
    else:
        atomic_write(path, updated, None)


def json_named_mcp_entry(path: Path, name: str) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid JSON client configuration: {path}") from error
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"mcpServers must be an object: {path}")
    entry = servers.get(name)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise SetupError(f"{name} MCP entry must be an object: {path}")
    return entry


def upsert_json_stdio_mcp(
    path: Path,
    launcher: Path,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    file_existed = path.exists()
    if file_existed:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SetupError(f"invalid JSON client configuration: {path}") from error
    else:
        document = {}
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"mcpServers must be an object: {path}")
    expected = stdio_mcp_entry(launcher)
    existing = servers.get(TARGET_RUNTIME_MCP_NAME)
    created_entry = record_created_entry(prior)
    if existing is None:
        servers[TARGET_RUNTIME_MCP_NAME] = expected
        created_entry = True
    elif existing == expected:
        return {
            "path": str(path),
            "command": str(launcher),
            "args": [],
            "created_entry": created_entry,
            "created_file": record_created_file(prior),
        }
    elif created_entry:
        servers[TARGET_RUNTIME_MCP_NAME] = expected
    else:
        raise SetupError(
            f"existing {TARGET_RUNTIME_MCP_NAME} MCP command differs in {path}"
        )
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would register {TARGET_RUNTIME_MCP_NAME} in {path}")
    else:
        atomic_write(path, updated, None)
    return {
        "path": str(path),
        "command": str(launcher),
        "args": [],
        "created_entry": created_entry,
        "created_file": record_created_file(prior) or not file_existed,
    }


def remove_json_stdio_mcp(
    path: Path, record: dict[str, Any], backups: Path, dry_run: bool
) -> None:
    if not record_created_entry(record) or not path.exists() or path.is_symlink():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return
    expected = {
        "type": "stdio",
        "command": record.get("command"),
        "args": record.get("args", []),
    }
    if servers.get(TARGET_RUNTIME_MCP_NAME) != expected:
        print(f"warning: preserving changed {TARGET_RUNTIME_MCP_NAME} MCP entry in {path}")
        return
    servers.pop(TARGET_RUNTIME_MCP_NAME, None)
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would remove {TARGET_RUNTIME_MCP_NAME} from {path}")
    elif record_created_file(record) and document == {"mcpServers": {}}:
        path.unlink()
    else:
        atomic_write(path, updated, None)


def configure_runtime_mcp(
    home: Path,
    clients: Iterable[str],
    launcher: Path,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    managed: dict[str, dict[str, Any]] = {}
    prior = prior or {}
    for client in clients:
        previous = prior.get(client)
        if not isinstance(previous, dict):
            previous = None
        if client == "codex":
            managed[client] = upsert_toml_stdio_mcp(
                home / ".codex" / "config.toml",
                launcher,
                backups,
                dry_run,
                previous,
            )
        elif client == "claude":
            managed[client] = upsert_json_stdio_mcp(
                home / ".claude.json", launcher, backups, dry_run, previous
            )
        elif client == "openclaw":
            managed[client] = {"adapter_available": False, "command": str(launcher)}
    return managed


def configure_mcp(
    home: Path,
    clients: Iterable[str],
    url: str,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    managed: dict[str, dict[str, Any]] = {}
    prior = prior or {}
    for client in clients:
        previous = prior.get(client)
        if not isinstance(previous, dict):
            previous = None
        if client == "codex":
            managed[client] = upsert_toml_mcp(
                home / ".codex" / "config.toml", url, backups, dry_run, previous
            )
        elif client == "claude":
            managed[client] = upsert_json_mcp(
                home / ".claude.json", url, backups, dry_run, previous
            )
        elif client == "openclaw":
            print("warning: OpenClaw Skill links installed; MCP registration requires a supported OpenClaw config adapter")
            managed[client] = {"adapter_available": False, "url": url}
    return managed


def upsert_toml_knowledge_stdio_mcp(
    path: Path,
    launcher: Path,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    file_existed = path.exists()
    original = path.read_text(encoding="utf-8") if file_existed else ""
    lines = original.splitlines()
    header = f"[mcp_servers.{KNOWLEDGE_MCP_NAME}]"
    bounds = toml_section_bounds(lines, header)
    created_entry = record_created_entry(prior)
    expected_command = str(launcher)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((header, f"command = {json.dumps(expected_command)}", "args = []"))
        created_entry = True
    else:
        existing = toml_knowledge_mcp_entry(path)
        if existing == {"transport": "stdio", "command": expected_command}:
            return {
                "path": str(path),
                "transport": "stdio",
                "command": expected_command,
                "args": [],
                "created_entry": created_entry,
                "created_file": record_created_file(prior),
            }
        if (
            not created_entry
            and existing is not None
            and existing.get("transport") == "http"
            and existing.get("url") == LEGACY_STUDIO_HTTP_URL
        ):
            created_entry = True
        elif not created_entry and toml_has_known_legacy_knowledge_stdio(path):
            created_entry = True
        elif not created_entry:
            return {
                "path": str(path),
                "ownership": "external",
                "transport": str(existing.get("transport", "unknown")) if existing else "unknown",
                "created_entry": False,
                "created_file": False,
            }
        start, end = bounds
        lines[start:end] = (
            header,
            f"command = {json.dumps(expected_command)}",
            "args = []",
        )
    updated = "\n".join(lines).rstrip() + "\n"
    if updated != original:
        backup_config_file(path, backups, dry_run)
        if dry_run:
            print(f"would register {KNOWLEDGE_MCP_NAME} in {path}")
        else:
            atomic_write(path, updated, None)
    return {
        "path": str(path),
        "transport": "stdio",
        "command": expected_command,
        "args": [],
        "created_entry": created_entry,
        "created_file": record_created_file(prior) or not file_existed,
    }


def upsert_json_knowledge_stdio_mcp(
    path: Path,
    launcher: Path,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {path}")
    file_existed = path.exists()
    if file_existed:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SetupError(f"invalid JSON client configuration: {path}") from error
    else:
        document = {}
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"mcpServers must be an object: {path}")
    expected = stdio_mcp_entry(launcher)
    existing = servers.get(KNOWLEDGE_MCP_NAME)
    created_entry = record_created_entry(prior)
    if existing is None:
        servers[KNOWLEDGE_MCP_NAME] = expected
        created_entry = True
    elif existing == expected:
        return {
            "path": str(path),
            "transport": "stdio",
            "command": str(launcher),
            "args": [],
            "created_entry": created_entry,
            "created_file": record_created_file(prior),
        }
    elif (
        not created_entry
        and isinstance(existing, dict)
        and existing.get("url") == LEGACY_STUDIO_HTTP_URL
    ):
        created_entry = True
        servers[KNOWLEDGE_MCP_NAME] = expected
    elif (
        not created_entry
        and isinstance(existing, dict)
        and is_known_legacy_knowledge_stdio(
            existing.get("command"),
            existing.get("args"),
        )
    ):
        created_entry = True
        servers[KNOWLEDGE_MCP_NAME] = expected
    elif not created_entry:
        return {
            "path": str(path),
            "ownership": "external",
            "transport": "stdio" if isinstance(existing, dict) and "command" in existing else "http",
            "created_entry": False,
            "created_file": False,
        }
    else:
        servers[KNOWLEDGE_MCP_NAME] = expected
    updated = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup_config_file(path, backups, dry_run)
    if dry_run:
        print(f"would register {KNOWLEDGE_MCP_NAME} in {path}")
    else:
        atomic_write(path, updated, None)
    return {
        "path": str(path),
        "transport": "stdio",
        "command": str(launcher),
        "args": [],
        "created_entry": created_entry,
        "created_file": record_created_file(prior) or not file_existed,
    }


def configure_knowledge_mcp(
    home: Path,
    clients: Iterable[str],
    launcher: Path,
    backups: Path,
    dry_run: bool,
    prior: dict[str, Any] | None = None,
    *,
    url: str | None = None,
) -> dict[str, dict[str, Any]]:
    if url:
        return configure_mcp(home, clients, url, backups, dry_run, prior)
    managed: dict[str, dict[str, Any]] = {}
    prior = prior or {}
    for client in clients:
        previous = prior.get(client)
        if not isinstance(previous, dict):
            previous = None
        if client == "codex":
            managed[client] = upsert_toml_knowledge_stdio_mcp(
                home / ".codex" / "config.toml",
                launcher,
                backups,
                dry_run,
                previous,
            )
        elif client == "claude":
            managed[client] = upsert_json_knowledge_stdio_mcp(
                home / ".claude.json",
                launcher,
                backups,
                dry_run,
                previous,
            )
        elif client == "openclaw":
            managed[client] = {"adapter_available": False, "command": str(launcher)}
    return managed


def knowledge_mcp_transport_summary(
    clients: Iterable[str], records: Mapping[str, object]
) -> tuple[list[str], list[str], list[str]]:
    external: list[str] = []
    managed_stdio: list[str] = []
    managed_http: list[str] = []
    for client in clients:
        if client not in SUPPORTED_MCP_CLIENTS:
            continue
        record = records.get(client)
        if isinstance(record, Mapping) and record.get("ownership") == "external":
            external.append(client)
        elif isinstance(record, Mapping) and record.get("command"):
            managed_stdio.append(client)
        else:
            managed_http.append(client)
    return external, managed_stdio, managed_http


def inherit_created_file_ownership(
    records: dict[str, dict[str, Any]],
    owners: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    for client, record in records.items():
        owner = owners.get(client)
        if isinstance(owner, Mapping) and record_created_file(owner):
            record["created_file"] = True
    return records


def recover_runtime_mcp_ownership(
    home: Path,
    clients: Iterable[str],
    prior: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    """Recover ownership omitted by state written before runtime_mcp existed."""
    recovered = {
        client: dict(record)
        for client, record in prior.items()
        if isinstance(client, str) and isinstance(record, dict)
    }
    launcher = runtime_launcher_path(home)
    expected = stdio_mcp_entry(launcher)
    for client in clients:
        if client not in SUPPORTED_MCP_CLIENTS:
            continue
        if valid_client_ownership_record(recovered.get(client)):
            continue
        if client == "codex":
            path = home / ".codex" / "config.toml"
            try:
                current = toml_stdio_mcp_entry(path)
            except (OSError, UnicodeError, SetupError):
                current = None
        else:
            path = home / ".claude.json"
            try:
                current = json_named_mcp_entry(path, TARGET_RUNTIME_MCP_NAME)
            except SetupError:
                current = None
        if current == expected:
            recovered[client] = {
                "path": str(path),
                "command": str(launcher),
                "args": [],
                "created_entry": True,
                # The entry can be recovered exactly from its installer-owned
                # launcher path. File ownership cannot, so preserve the file.
                "created_file": False,
            }
    return recovered


def validate_mcp_configuration(
    home: Path,
    clients: Iterable[str],
    url: str,
    prior: Mapping[str, object] | None = None,
) -> None:
    prior = prior or {}
    for client in clients:
        previous = prior.get(client)
        managed_entry = (
            record_created_entry(previous)
            if isinstance(previous, Mapping)
            else False
        )
        if client == "codex":
            path = home / ".codex" / "config.toml"
            if path.is_symlink():
                raise SetupError(f"refusing to replace symbolic link: {path}")
            if path.exists() and not path.is_file():
                raise SetupError(f"client configuration is not a regular file: {path}")
            entry = toml_knowledge_mcp_entry(path)
            current = entry.get("url") if entry is not None else None
            if (
                entry is not None
                and entry.get("transport") == "http"
                and current != url
                and not managed_entry
            ):
                raise SetupError(
                    f"existing {KNOWLEDGE_MCP_NAME} MCP URL differs in {path}: {current!r}"
                )
        elif client == "claude":
            path = home / ".claude.json"
            if path.is_symlink():
                raise SetupError(f"refusing to replace symbolic link: {path}")
            if path.exists() and not path.is_file():
                raise SetupError(f"client configuration is not a regular file: {path}")
            current = json_mcp_entry(path)
            if current is not None and current.get("url") != url and not managed_entry:
                raise SetupError(
                    f"existing {KNOWLEDGE_MCP_NAME} MCP URL differs in {path}: "
                    f"{current.get('url')!r}"
                )


def validate_knowledge_mcp_configuration(
    home: Path,
    clients: Iterable[str],
    launcher: Path,
    prior: Mapping[str, object] | None = None,
    *,
    url: str | None = None,
) -> None:
    if url:
        validate_mcp_configuration(home, clients, url, prior)
        return
    del launcher
    prior = prior or {}
    for client in clients:
        previous = prior.get(client)
        managed_entry = record_created_entry(previous) if isinstance(previous, Mapping) else False
        if client == "codex":
            path = home / ".codex" / "config.toml"
            if path.is_symlink():
                raise SetupError(f"refusing to replace symbolic link: {path}")
            if path.exists() and not path.is_file():
                raise SetupError(f"client configuration is not a regular file: {path}")
            entry = toml_knowledge_mcp_entry(path)
            if (
                entry is not None
                and entry.get("transport") == "http"
                and entry.get("url") != LEGACY_STUDIO_HTTP_URL
                and not managed_entry
            ):
                raise SetupError(
                    f"existing {KNOWLEDGE_MCP_NAME} MCP URL differs in {path}: {entry.get('url')!r}"
                )
        elif client == "claude":
            path = home / ".claude.json"
            if path.is_symlink():
                raise SetupError(f"refusing to replace symbolic link: {path}")
            if path.exists() and not path.is_file():
                raise SetupError(f"client configuration is not a regular file: {path}")
            entry = json_mcp_entry(path)
            if (
                isinstance(entry, Mapping)
                and "url" in entry
                and entry.get("url") != LEGACY_STUDIO_HTTP_URL
                and not managed_entry
            ):
                raise SetupError(
                    f"existing {KNOWLEDGE_MCP_NAME} MCP URL differs in {path}: {entry.get('url')!r}"
                )


def validate_runtime_mcp_configuration(
    home: Path,
    clients: Iterable[str],
    launcher: Path,
    prior: dict[str, Any] | None = None,
) -> None:
    prior = prior or {}
    expected = stdio_mcp_entry(launcher)
    for client in clients:
        previous = prior.get(client)
        managed_entry = (
            record_created_entry(previous)
            if isinstance(previous, Mapping)
            else False
        )
        if client == "codex":
            path = home / ".codex" / "config.toml"
            if path.is_symlink():
                raise SetupError(f"refusing to replace symbolic link: {path}")
            if path.exists() and not path.is_file():
                raise SetupError(f"client configuration is not a regular file: {path}")
            current = toml_stdio_mcp_entry(path)
        elif client == "claude":
            path = home / ".claude.json"
            if path.is_symlink():
                raise SetupError(f"refusing to replace symbolic link: {path}")
            if path.exists() and not path.is_file():
                raise SetupError(f"client configuration is not a regular file: {path}")
            current = json_named_mcp_entry(path, TARGET_RUNTIME_MCP_NAME)
        else:
            continue
        if current is not None and current != expected and not managed_entry:
            raise SetupError(
                f"existing {TARGET_RUNTIME_MCP_NAME} MCP command differs in {path}"
            )


def validate_link_plan(
    home: Path,
    source: Path,
    clients: Iterable[str],
    *,
    allow_missing_targets: bool = False,
    bundle: tuple[tuple[str, str], ...] = SKILL_BUNDLE,
) -> None:
    for client in clients:
        for canonical, relative in bundle:
            target = source / relative
            if not allow_missing_targets and not target.is_dir():
                raise SetupError(f"Skill target is missing: {target}")
            link = client_skills_dir(home, client) / canonical
            if link.exists() and not link.is_symlink():
                if not link.is_file() and not link.is_dir():
                    raise SetupError(f"conflicting Skill path cannot be backed up: {link}")
                if not os.access(link, os.R_OK):
                    raise SetupError(f"conflicting Skill path cannot be backed up: {link}")


def knowledge_http_health(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    health_url = url[:-4] + "/health" if url.endswith("/mcp") else url.rstrip("/") + "/health"
    request = urllib.request.Request(health_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(4096).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as error:
        return False, str(error)
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        return False, "health endpoint did not return JSON"
    if document.get("status") != "ok":
        return False, "health endpoint did not report status=ok"
    tools = document.get("tools", "unknown")
    return True, f"ok ({tools} tools)"


def save_state(home: Path, state: dict[str, object], dry_run: bool) -> None:
    path = state_path(home)
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if dry_run:
        print(f"would write installer state to {path}")
        return
    atomic_write(path, content, 0o600)


def load_state(home: Path) -> dict[str, object]:
    path = state_path(home)
    if path.is_symlink() or not path.is_file():
        raise SetupError(f"installer state is missing: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SetupError(f"installer state must be owned by the current user with mode 0600: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid installer state: {path}") from error
    if not isinstance(document, dict):
        raise SetupError(f"installer state must be an object: {path}")
    if document.get("version") != STATE_VERSION:
        raise SetupError(f"unsupported installer state version: {document.get('version')}")
    return document


def try_load_state(home: Path) -> dict[str, object] | None:
    try:
        return load_state(home)
    except SetupError:
        return None


def recorded_string_list(
    state: Mapping[str, object], key: str
) -> tuple[str, ...]:
    value = state.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SetupError(f"{key} in installer state must be a list of strings")
    return tuple(value)


def recorded_string_mapping(
    state: Mapping[str, object], key: str
) -> dict[str, str]:
    value = state.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(item_key, str) or not isinstance(item_value, str)
        for item_key, item_value in value.items()
    ):
        raise SetupError(f"{key} in installer state must map strings to strings")
    return dict(value)


def recorded_object(state: Mapping[str, object], key: str) -> dict[str, Any]:
    value = state.get(key, {})
    if not isinstance(value, dict):
        raise SetupError(f"{key} in installer state must be an object")
    return dict(value)


def recorded_client_records(
    state: Mapping[str, object], key: str
) -> dict[str, dict[str, Any]]:
    records = recorded_object(state, key)
    invalid = [
        repr(name)
        for name, value in records.items()
        if not isinstance(name, str) or not isinstance(value, dict)
    ]
    if invalid:
        raise SetupError(
            f"{key} in installer state has invalid client records: "
            + ", ".join(sorted(invalid))
        )
    return {name: dict(value) for name, value in records.items()}


def source_root_from_state(state: Mapping[str, object]) -> Path:
    value = state.get("source_root")
    if not isinstance(value, str) or not value.strip():
        raise SetupError("source_root in installer state must be a non-empty string")
    return Path(value)


def decode_recorded_install(state: Mapping[str, object]) -> RecordedInstall:
    profile = skill_profile_from_state(state)
    source_mode = source_mode_from_state(state)
    source_commit = str(state.get("source_commit", ""))
    requested_ref = str(state.get("requested_ref", state.get("ref", "")))
    preserved_skills = parse_preserved_skills(
        ",".join(recorded_string_list(state, "preserved_skills")),
        profile.bundle,
    )
    return RecordedInstall(
        source_root=source_root_from_state(state),
        source_mode=source_mode,
        source_commit=source_commit,
        resolved_commit=str(state.get("resolved_commit", source_commit)),
        rollback_commit=str(state.get("rollback_commit", "")),
        repo_url=str(state.get("repo_url", DEFAULT_REPO_URL)),
        ref=str(state.get("ref", requested_ref or DEFAULT_REF)),
        requested_ref=requested_ref,
        ref_kind=ref_kind_from_state(state, source_mode),
        clients=recorded_string_list(state, "clients"),
        profile=profile,
        knowledge_url=str(
            state.get("knowledge_url", state.get("studio_url", ""))
        ),
        target=str(state.get("target", "current")),
        tool_dirs=recorded_string_list(state, "tool_dirs"),
        mcp=recorded_client_records(state, "mcp"),
        runtime_mcp=recorded_client_records(state, "runtime_mcp"),
        links=recorded_string_mapping(state, "links"),
        preserved_skills=preserved_skills,
        profiles=recorded_string_list(state, "profiles"),
        runtime=recorded_object(state, "runtime"),
    )


def missing_client_ownership_records(
    recorded: RecordedInstall,
) -> tuple[str, ...]:
    missing: list[str] = []
    for client in dict.fromkeys(recorded.clients):
        if client not in SUPPORTED_MCP_CLIENTS:
            continue
        if (
            recorded.profile.manages_knowledge_mcp
            and not valid_client_ownership_record(recorded.mcp.get(client))
        ):
            missing.append(f"mcp.{client}")
        if not valid_client_ownership_record(recorded.runtime_mcp.get(client)):
            missing.append(f"runtime_mcp.{client}")
    return tuple(missing)


def perform_install(
    args: argparse.Namespace,
    *,
    update: bool = False,
    recorded_state: RecordedInstall | None = None,
    repair_only: bool = False,
) -> int:
    home = args.home.expanduser().absolute()
    args.home = home
    if recorded_state is not None:
        prior_install = recorded_state
    else:
        prior_document = try_load_state(home)
        prior_install = (
            decode_recorded_install(prior_document)
            if prior_document is not None
            else None
        )
    if args.knowledge_url is None:
        prior_url = prior_install.knowledge_url if prior_install is not None else ""
        args.knowledge_url = prior_url if prior_url and prior_url != LEGACY_STUDIO_HTTP_URL else None
    if args.skill_profile is not None:
        selected_policy = resolve_skill_profile(str(args.skill_profile))
    elif prior_install is not None:
        selected_policy = prior_install.profile
    else:
        selected_policy = resolve_skill_profile(DEFAULT_SKILL_PROFILE)
    selected_profile = selected_policy.name
    selected_bundle = selected_policy.bundle
    if args.preserve_skills is not None:
        preserved_skills = parse_preserved_skills(
            args.preserve_skills,
            selected_bundle,
        )
    elif prior_install is not None:
        preserved_skills = parse_preserved_skills(
            ",".join(prior_install.preserved_skills),
            selected_bundle,
        )
    else:
        preserved_skills = ()
    args.skill_profile = selected_profile
    manage_knowledge_mcp = selected_policy.manages_knowledge_mcp
    prior_source: Path | None = None
    prior_source_mode: str | None = None
    if args.source is None and prior_install and args.source_mode == "auto":
        prior_source = prior_install.source_root
        prior_source_mode = prior_install.source_mode
        args.repo_url = prior_install.repo_url
        args.ref = prior_install.ref
        args.target = prior_install.target
    clients = parse_clients(args.clients, home)
    if prior_install:
        clients = list(dict.fromkeys([*prior_install.clients, *clients]))
    prior_mcp = dict(prior_install.mcp) if prior_install else {}
    prior_runtime_mcp = (
        recover_runtime_mcp_ownership(
            home,
            prior_install.clients,
            prior_install.runtime_mcp,
        )
        if prior_install
        else {}
    )
    validate_environment_paths(home)
    if manage_knowledge_mcp:
        validate_knowledge_mcp_configuration(
            home,
            clients,
            knowledge_launcher_path(home),
            prior_mcp,
            url=args.knowledge_url,
        )
    validate_runtime_mcp_configuration(
        home,
        clients,
        runtime_launcher_path(home),
        prior_runtime_mcp,
    )
    credential_plan = prepare_credentials(args, repair_only=repair_only)
    recorded_tool_dirs: Iterable[str] = (
        prior_install.tool_dirs if prior_install else ()
    )
    tool_dirs = list(dict.fromkeys([*recorded_tool_dirs, str(user_tool_bin(home))]))
    if not args.skip_tool_install:
        install_bootstrap_tools(
            home,
            clients,
            tool_dirs,
            dry_run=args.dry_run,
            knowledge_mcp=manage_knowledge_mcp,
        )

    if prior_source is not None and prior_source_mode is not None:
        source = validate_source(prior_source, selected_bundle)
        source_mode = prior_source_mode
        if update and source_mode == "managed":
            if prior_install is not None and prior_install.ref_kind in {"tag", "commit"}:
                checkout_managed_release(
                    source,
                    args.repo_url,
                    args.ref,
                    args.dry_run,
                    bundle=selected_bundle,
                )
            else:
                update_managed_source(
                    source,
                    args.repo_url,
                    args.ref,
                    args.dry_run,
                    bundle=selected_bundle,
                )
    else:
        source, source_mode = resolve_source(
            args, update=update, bundle=selected_bundle
        )
    if update and source_mode == "managed" and source.exists() and not args.dry_run:
        source = validate_source(source, selected_bundle)
    if source_mode == "managed" and source.exists():
        validate_release_source(source, args.dry_run)
    planned_missing_source = (
        args.dry_run and source_mode == "managed" and not source.exists()
    )
    if not args.skip_tool_install:
        install_python_workflow_tools(
            home,
            source,
            tool_dirs,
            dry_run=args.dry_run,
        )
    knowledge_node: Path | None = None
    if manage_knowledge_mcp:
        if args.skip_tool_install:
            knowledge_node = resolve_node(home, tool_dirs) or (home / ".local" / "bin" / "node")
        else:
            knowledge_node = install_knowledge_dependencies(
                home,
                source,
                tool_dirs,
                dry_run=args.dry_run,
            )
    tool_dirs, missing_tools = resolve_tool_dirs(
        args.non_interactive,
        tool_dirs,
    )
    tooling = inspect_tooling(tool_dirs, clients)
    if not args.dry_run and not args.skip_tool_install:
        unresolved = list(missing_tools)
        unresolved.extend(
            tool
            for tool, available in tooling["conditional"].items()
            if not available
        )
        unresolved.extend(
            tool
            for tool, available in tooling["recommended"].items()
            if not available
        )
        if "codex" in clients and not tooling["clients"].get("codex", False):
            unresolved.append("codex")
        if unresolved:
            raise SetupError(
                "automatic workflow tool installation did not complete: "
                + ", ".join(sorted(set(unresolved)))
            )
    runtime_plan = build_runtime_plan(
        home,
        source,
        allow_missing_source=planned_missing_source,
    )
    knowledge_plan = (
        build_knowledge_plan(
            home,
            source,
            knowledge_node or (home / ".local" / "bin" / "node"),
            allow_missing_source=planned_missing_source,
        )
        if manage_knowledge_mcp
        else {}
    )
    validate_link_plan(
        home,
        source,
        clients,
        allow_missing_targets=planned_missing_source,
        bundle=selected_bundle,
    )
    preserved_targets = resolve_preserved_link_targets(
        home,
        clients,
        preserved_skills,
        prior_install.links if prior_install is not None else None,
        prefer_recorded=(
            args.preserve_skills is None and prior_install is not None
        ),
    )
    backups = backup_path(home)
    migrate_legacy_mcp_names(home, clients, backups, args.dry_run)
    managed_links = install_links(
        home,
        source,
        clients,
        backups,
        args.dry_run,
        selected_bundle,
        preserved_targets,
    )
    profiles = install_environment_files(home, tool_dirs, backups, args.dry_run)
    credential_result = apply_credentials_plan(credential_plan, args.dry_run)
    runtime_state = deploy_runtime(runtime_plan, args.dry_run)
    if manage_knowledge_mcp:
        ensure_knowledge_config(home, args.kb_config, args.dry_run)
        knowledge_state = deploy_knowledge_mcp(knowledge_plan, args.dry_run)
    else:
        knowledge_state = {}
    mcp_state = (
        configure_knowledge_mcp(
            home,
            clients,
            Path(knowledge_state["launcher_path"]),
            backups,
            args.dry_run,
            prior_mcp,
            url=args.knowledge_url,
        )
        if manage_knowledge_mcp
        else dict(prior_mcp)
    )
    runtime_mcp_state = inherit_created_file_ownership(
        configure_runtime_mcp(
            home,
            clients,
            Path(runtime_state["launcher_path"]),
            backups,
            args.dry_run,
            prior_runtime_mcp,
        ),
        mcp_state,
    )
    current_source_commit = git_commit(source) if source.exists() else "planned"
    source_commit = (
        prior_install.source_commit
        if (
            args.command == "repair"
            and prior_install is not None
            and prior_install.source_commit
        )
        else current_source_commit
    )
    rollback_commit = (
        prior_install.source_commit
        if (
            prior_install is not None
            and source_commit != prior_install.source_commit
            and source_mode == "managed"
            and args.command in {"install", "update", "rollback"}
        )
        else (prior_install.rollback_commit if prior_install is not None else "")
    )
    ref_kind = (
        release_ref_kind(args.ref)
        if source_mode == "managed" and (args.ref_explicit or prior_install is None)
        else (
            prior_install.ref_kind
            if source_mode == "managed" and prior_install is not None
            else "linked"
        )
    )
    requested_ref = str(args.ref) if source_mode == "managed" else ""
    state = {
        "version": STATE_VERSION,
        "repo_url": args.repo_url,
        "ref": args.ref,
        "requested_ref": requested_ref,
        "ref_kind": ref_kind,
        "source_root": str(source),
        "source_commit": source_commit,
        "resolved_commit": source_commit,
        "rollback_commit": rollback_commit,
        "source_dirty": (
            git_dirty(source, paths=bundle_git_paths(selected_bundle))
            if source.exists()
            else False
        ),
        "source_mode": source_mode,
        "managed_checkout": source_mode == "managed",
        "skill_profile": selected_profile,
        "clients": clients,
        "links": managed_links,
        "preserved_skills": list(preserved_skills),
        "profiles": profiles,
        "tool_dirs": tool_dirs,
        "mcp": mcp_state,
        "knowledge_mcp": knowledge_state,
        "runtime": runtime_state,
        "runtime_mcp": runtime_mcp_state,
        "knowledge_url": args.knowledge_url or "",
        "target": args.target,
    }
    setattr(args, "_planned_workflow_state", state)
    setattr(args, "_tooling_report", tooling)
    setattr(args, "_credential_result", credential_result)
    save_state(home, state, args.dry_run)
    if args.skip_tool_install:
        if missing_tools:
            print(
                "warning: required workflow tools are missing from PATH: "
                + ", ".join(missing_tools)
            )
        if not tooling["conditional"]["sshpass"]:
            print(
                "warning: sshpass is missing; password SSH, remote log pulling, "
                "and Live Patch are unavailable"
            )
        if not tooling["recommended"]["rg"]:
            print(
                "warning: rg is missing; source evidence search will use a slower "
                "fallback"
            )
    if credential_result == "missing":
        _, credential_detail = credentials_status(credentials_path(home))
        print(
            "warning: credentials are incomplete "
            f"({credential_detail}); run the credentials command"
        )
    knowledge_report: dict[str, object]
    if manage_knowledge_mcp:
        external_clients, managed_stdio, managed_http = knowledge_mcp_transport_summary(
            clients,
            mcp_state,
        )
        if managed_stdio:
            healthy, detail, tools, configured = knowledge_mcp_health(
                Path(knowledge_state["launcher_path"]), home
            ) if not args.dry_run else (True, "planned", [], False)
            knowledge_report = {
                "managed": True,
                "healthy": healthy,
                "configured": configured,
                "transport": "stdio",
                "detail": detail,
                "tools": tools,
                "clients": managed_stdio,
            }
            print(f"{KNOWLEDGE_MCP_NAME}: {detail}")
        elif managed_http:
            healthy, detail = knowledge_http_health(str(args.knowledge_url))
            knowledge_report = {
                "managed": True,
                "healthy": healthy,
                "transport": "http",
                "detail": detail,
                "url": args.knowledge_url,
            }
            if healthy:
                print(f"{KNOWLEDGE_MCP_NAME}: {detail}")
            else:
                print(f"warning: {KNOWLEDGE_MCP_NAME} is unavailable: {detail}")
        elif external_clients:
            knowledge_report = {
                "managed": True,
                "healthy": True,
                "transport": "external-stdio",
                "detail": (
                    "external stdio configuration preserved; the client starts it "
                    "on demand"
                ),
                "clients": external_clients,
            }
            print(
                f"{KNOWLEDGE_MCP_NAME}: external stdio configuration preserved; "
                "the client starts it on demand"
            )
        else:
            knowledge_report = {
                "managed": True,
                "healthy": False,
                "transport": "unavailable",
                "detail": "no supported MCP client adapter",
            }
            print(
                f"warning: {KNOWLEDGE_MCP_NAME} has no supported MCP client adapter"
            )
    else:
        knowledge_report = {
            "managed": False,
            "healthy": False,
            "transport": "external",
            "detail": f"not managed by {selected_profile} profile",
        }
        print(f"{KNOWLEDGE_MCP_NAME}: not managed by {selected_profile} profile")
    setattr(args, "_knowledge_mcp_report", knowledge_report)
    completed_action = {
        "install": "installed",
        "repair": "repaired",
        "update": "updated",
        "refresh": "refreshed",
    }.get(args.command, "installed")
    planned_action = {
        "install": "would be installed",
        "repair": "would be repaired",
        "update": "would be updated",
        "refresh": "would be refreshed",
    }.get(args.command, "would be installed")
    print(
        f"openUBMC workflow {planned_action if args.dry_run else completed_action}: "
        f"skills={len(selected_bundle)} profile={selected_profile} "
        f"clients={','.join(clients)} source={source_mode} "
        f"commit={state['source_commit']}"
        + (
            " preserved=" + ",".join(preserved_skills)
            if preserved_skills
            else ""
        )
    )
    return 0


def check_toml_mcp(
    path: Path, url: str, record: dict[str, Any] | None = None
) -> bool:
    try:
        entry = toml_knowledge_mcp_entry(path)
    except (OSError, UnicodeError, SetupError):
        return False
    if record and record.get("ownership") == "external":
        return entry is not None
    if record and record.get("command"):
        return entry == {"transport": "stdio", "command": record.get("command")}
    return (
        entry is not None
        and entry.get("transport") == "http"
        and entry.get("url") == url
    )


def check_json_mcp(
    path: Path, url: str, record: dict[str, Any] | None = None
) -> bool:
    try:
        entry = json_mcp_entry(path)
    except SetupError:
        return False
    if not isinstance(entry, dict):
        return False
    if record and record.get("ownership") == "external":
        return True
    if record and record.get("command"):
        return entry == stdio_mcp_entry(str(record.get("command")))
    return entry.get("url") == url


def check_toml_runtime_mcp(path: Path, launcher: Path) -> bool:
    try:
        return toml_stdio_mcp_entry(path) == stdio_mcp_entry(launcher)
    except (OSError, UnicodeError, SetupError):
        return False


def check_json_runtime_mcp(path: Path, launcher: Path) -> bool:
    try:
        return json_named_mcp_entry(
            path,
            TARGET_RUNTIME_MCP_NAME,
        ) == stdio_mcp_entry(launcher)
    except SetupError:
        return False


def inspect_runtime_installation(state: dict[str, object]) -> dict[str, object]:
    recorded = state.get("runtime")
    if not isinstance(recorded, dict):
        return {
            "healthy": False,
            "matches_installed_state": False,
            "api_version": "unknown",
            "content_digest": "unknown",
            "detail": "Runtime state is missing; run openubmc-environment-setup repair",
        }
    package = Path(str(recorded.get("package_path", "")))
    launcher = Path(str(recorded.get("launcher_path", "")))
    manifest = Path(str(recorded.get("manifest_path", "")))
    expected_api = str(recorded.get("api_version", ""))
    expected_digest = str(recorded.get("content_digest", ""))
    try:
        actual_api = read_runtime_api_version(package)
        actual_digest = runtime_content_digest(package)
        manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    except (SetupError, OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "healthy": False,
            "matches_installed_state": False,
            "api_version": "unknown",
            "content_digest": "unknown",
            "package_path": str(package),
            "launcher_path": str(launcher),
            "detail": f"{error}; run openubmc-environment-setup repair",
        }
    manifest_matches = isinstance(manifest_document, dict) and all(
        manifest_document.get(key) == recorded.get(key)
        for key in (
            "schema_version",
            "api_version",
            "content_digest",
            "package_path",
            "launcher_path",
            "mcp_entrypoint",
        )
    )
    launcher_ready = launcher.is_file() and not launcher.is_symlink() and os.access(
        launcher, os.X_OK
    )
    matches = (
        expected_api == TARGET_RUNTIME_API_VERSION
        and actual_api == expected_api
        and actual_digest == expected_digest
        and manifest_matches
        and launcher_ready
    )
    detail = (
        "ok"
        if matches
        else "Runtime API, digest, manifest, or launcher mismatch; "
        "run openubmc-environment-setup repair"
    )
    return {
        "healthy": matches,
        "matches_installed_state": matches,
        "api_version": actual_api,
        "content_digest": actual_digest,
        "expected_api_version": expected_api,
        "expected_content_digest": expected_digest,
        "package_path": str(package),
        "launcher_path": str(launcher),
        "manifest_path": str(manifest),
        "detail": detail,
    }


def runtime_mcp_health(
    launcher: Path,
    home: Path,
    timeout: float = 15.0,
) -> tuple[bool, str, list[str]]:
    if not launcher.is_file() or launcher.is_symlink() or not os.access(launcher, os.X_OK):
        return False, "launcher is missing or not executable", []
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":'
        '{"name":"openubmc-environment-setup","version":"1"}}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":'
        '{"name":"runtime_status","arguments":{}}}\n'
    )
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    try:
        result = subprocess.run(
            [str(launcher)],
            input=requests,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error), []
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "launcher failed"
        return False, detail, []
    responses: dict[object, dict[str, object]] = {}
    try:
        for line in result.stdout.splitlines():
            document = json.loads(line)
            if isinstance(document, dict):
                responses[document.get("id")] = document
    except json.JSONDecodeError:
        return False, "MCP launcher returned invalid JSON", []
    initialize = responses.get(1, {}).get("result", {})
    tools_result = responses.get(2, {}).get("result", {})
    status_result = responses.get(3, {}).get("result", {})
    if not all(isinstance(value, dict) for value in (initialize, tools_result, status_result)):
        return False, "MCP initialize, tools/list, or runtime_status response is missing", []
    server = initialize.get("serverInfo", {})
    if not isinstance(server, dict) or server.get("version") != TARGET_RUNTIME_API_VERSION:
        return False, "MCP Runtime API version mismatch", []
    tool_entries = tools_result.get("tools", [])
    if not isinstance(tool_entries, list):
        return False, "MCP tools/list result is invalid", []
    tools_found = sorted(
        str(entry.get("name"))
        for entry in tool_entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    )
    required = {
        "debug_run",
        "debug_collect",
        "log_bundle_collect",
        "live_patch_run",
        "upgrade_run",
        "case_read",
        "evidence_read",
        "case_close",
        "case_forget",
        "phase_record",
        "workflow.advance",
        "workflow.next",
        "runtime_status",
    }
    if not required.issubset(tools_found):
        return False, "MCP domain tools are incomplete", tools_found
    if status_result.get("isError") is not False:
        return False, "MCP runtime_status call failed", tools_found
    structured = status_result.get("structuredContent")
    if not isinstance(structured, dict):
        return False, "MCP runtime_status structured result is missing", tools_found
    if structured.get("api_version") != TARGET_RUNTIME_API_VERSION:
        return False, "MCP runtime_status API version mismatch", tools_found
    return True, f"ok ({len(tools_found)} domain tools)", tools_found


def knowledge_mcp_health(
    launcher: Path,
    home: Path,
    timeout: float = 15.0,
) -> tuple[bool, str, list[str], bool]:
    if not launcher.is_file() or launcher.is_symlink() or not os.access(launcher, os.X_OK):
        return False, "launcher is missing or not executable", [], False
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":'
        '{"name":"openubmc-environment-setup","version":"1"}}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":'
        '{"name":"openubmc_kb_status","arguments":{}}}\n'
    )
    environment = {**os.environ, "HOME": str(home)}
    try:
        result = subprocess.run(
            [str(launcher)],
            input=requests,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error), [], False
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "launcher failed"
        return False, detail, [], False
    responses: dict[object, dict[str, object]] = {}
    try:
        for line in result.stdout.splitlines():
            document = json.loads(line)
            if isinstance(document, dict):
                responses[document.get("id")] = document
    except json.JSONDecodeError:
        return False, "MCP launcher returned invalid JSON", [], False
    initialize = responses.get(1, {}).get("result", {})
    tools_result = responses.get(2, {}).get("result", {})
    status_result = responses.get(3, {}).get("result", {})
    if not all(isinstance(value, dict) for value in (initialize, tools_result, status_result)):
        return False, "MCP initialize, tools/list, or status response is missing", [], False
    server = initialize.get("serverInfo", {})
    if not isinstance(server, dict) or server.get("version") != KNOWLEDGE_MCP_VERSION:
        return False, "openUBMC KB MCP version mismatch", [], False
    entries = tools_result.get("tools", [])
    if not isinstance(entries, list):
        return False, "openUBMC KB MCP tools/list result is invalid", [], False
    tools_found = sorted(
        str(entry.get("name"))
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    )
    required = {"openubmc_kb_query", "openubmc_kb_status", "openubmc_kb_list"}
    if not required.issubset(tools_found):
        return False, "openUBMC KB MCP tools are incomplete", tools_found, False
    if status_result.get("isError") is True:
        return False, "openUBMC KB status call failed", tools_found, False
    structured = status_result.get("structuredContent")
    payload = structured.get("result") if isinstance(structured, Mapping) else None
    configured = bool(payload.get("configured")) if isinstance(payload, Mapping) else False
    return True, f"ok ({len(tools_found)} read-only tools; configured={str(configured).lower()})", tools_found, configured


def collect_check_report(args: argparse.Namespace) -> dict[str, Any]:
    home = args.home.expanduser().absolute()
    checks: list[dict[str, Any]] = []
    messages: list[str] = []

    def record(
        name: str,
        ok: bool,
        detail: str,
        message: str,
        *,
        category: str = "core",
        blocking: bool = True,
    ) -> None:
        checks.append(
            {
                "name": name,
                "ok": ok,
                "detail": detail,
                "category": category,
                "blocking": blocking,
            }
        )
        messages.append(message)

    try:
        state = load_state(home)
    except SetupError as error:
        record(
            "state",
            False,
            str(error),
            f"state: missing or invalid ({error})",
        )
        return {
            "ok": False,
            "readiness": {
                "core": False,
                "credentials": False,
                "runtime": False,
                "mcp": False,
                "engine": False,
                "knowledge": False,
                "studio": False,
                "tooling": False,
                "client": False,
                "password_ssh": False,
                "source_search": False,
            },
            "source": {"mode": "unknown"},
            "runtime": {"healthy": False, "detail": "not checked"},
            "runtime_mcp": {"healthy": False, "detail": "not checked", "tools": []},
            "engines": {"mcp": False, "cli": False, "one_shot": False},
            "tooling": {
                "ready": False,
                "client_ready": False,
                "required": {},
                "conditional": {},
                "recommended": {},
                "clients": {},
            },
            "next_actions": [],
            "checks": checks,
            "knowledge_mcp": {"healthy": False, "detail": "not checked"},
            "studio": {"healthy": False, "detail": "not checked"},
            "_messages": messages,
        }

    try:
        selected_policy = skill_profile_from_state(state)
        selected_profile = selected_policy.name
        selected_bundle = selected_policy.bundle
        record(
            "skill_profile",
            True,
            selected_profile,
            f"Skill profile: {selected_profile}",
        )
    except SetupError as error:
        selected_policy = resolve_skill_profile(DEFAULT_SKILL_PROFILE)
        selected_profile = selected_policy.name
        selected_bundle = selected_policy.bundle
        record(
            "skill_profile",
            False,
            str(error),
            f"Skill profile: invalid ({error})",
        )
    manage_knowledge_mcp = selected_policy.manages_knowledge_mcp
    try:
        preserved_skills = parse_preserved_skills(
            ",".join(recorded_string_list(state, "preserved_skills")),
            selected_bundle,
        )
        record(
            "preserved_skills",
            True,
            ", ".join(preserved_skills) if preserved_skills else "none",
            "preserved Skills: "
            + (", ".join(preserved_skills) if preserved_skills else "none"),
        )
    except SetupError as error:
        preserved_skills = ()
        record(
            "preserved_skills",
            False,
            str(error),
            f"preserved Skills: invalid ({error})",
        )

    try:
        source = source_root_from_state(state)
    except SetupError as error:
        source = home / ".invalid-openubmc-source"
        record(
            "source_root",
            False,
            str(error),
            f"source root: invalid ({error})",
        )
    try:
        source_mode = source_mode_from_state(state)
        record(
            "source_mode",
            True,
            source_mode,
            f"source mode: {source_mode}",
        )
    except SetupError as error:
        source_mode = "unknown"
        record(
            "source_mode",
            False,
            str(error),
            f"source mode: invalid ({error})",
        )
    requested_ref = str(state.get("requested_ref", state.get("ref", "")))
    resolved_commit = str(
        state.get("resolved_commit", state.get("source_commit", ""))
    )
    try:
        ref_kind = ref_kind_from_state(state, source_mode)
        if source_mode == "managed" and ref_kind in {"tag", "commit"}:
            if release_ref_kind(requested_ref) != ref_kind:
                raise SetupError(
                    "recorded requested ref does not match its immutable ref kind"
                )
            if not FULL_COMMIT.fullmatch(resolved_commit):
                raise SetupError("recorded resolved release commit is not complete")
        record(
            "source_revision",
            True,
            f"{ref_kind} {requested_ref or resolved_commit}",
            f"source revision: {ref_kind} {requested_ref or resolved_commit}",
        )
    except SetupError as error:
        ref_kind = "unknown"
        record(
            "source_revision",
            False,
            str(error),
            f"source revision: invalid ({error})",
        )
    try:
        validate_source(source, selected_bundle)
        source_valid = True
        record("source", True, str(source), f"source: ok ({source})")
    except SetupError as error:
        source_valid = False
        record("source", False, str(error), f"source: failed ({error})")

    expected_commit = str(state.get("source_commit", ""))
    actual_commit = git_commit(source) if source.exists() else "missing"
    commit_ok = not expected_commit or actual_commit == expected_commit
    if not commit_ok:
        record(
            "source_commit",
            False,
            f"installed={expected_commit}, current={actual_commit}",
            f"source commit: changed (installed={expected_commit}, current={actual_commit})",
        )
    else:
        record(
            "source_commit",
            True,
            actual_commit,
            f"source commit: {actual_commit}",
        )

    dirty_paths = None if args.deep else bundle_git_paths(selected_bundle)
    dirty = git_dirty(source, paths=dirty_paths) if source.exists() else False
    dirty_scope = "full" if args.deep else "bundle"
    if dirty and source_mode == "managed":
        record(
            "source_worktree",
            False,
            f"dirty managed checkout ({dirty_scope} scope)",
            "source worktree: dirty managed checkout",
        )
    elif dirty:
        record(
            "source_worktree",
            False,
            f"dirty linked checkout ({dirty_scope} scope)",
            "source worktree: dirty user-managed checkout (non-blocking)",
            blocking=False,
        )
    else:
        record(
            "source_worktree",
            True,
            f"clean ({dirty_scope} scope)",
            "source worktree: clean",
        )

    runtime_report = inspect_runtime_installation(state)
    runtime_ok = bool(runtime_report.get("healthy"))
    record(
        "target_runtime",
        runtime_ok,
        str(runtime_report.get("detail", "unknown")),
        "Target Runtime: " + str(runtime_report.get("detail", "unknown")),
    )

    client_values = state.get("clients", [])
    if not isinstance(client_values, list) or not client_values:
        clients = []
        record("clients", False, "missing or invalid", "clients: missing or invalid")
    else:
        invalid_clients = [
            repr(client)
            for client in client_values
            if not isinstance(client, str) or client not in CLIENTS
        ]
        clients = [
            client
            for client in client_values
            if isinstance(client, str) and client in CLIENTS
        ]
        if invalid_clients:
            detail = "invalid entries: " + ", ".join(invalid_clients)
            record("clients", False, detail, "clients: " + detail)
        else:
            detail = ", ".join(map(str, clients))
            record("clients", True, detail, "clients: " + detail)

    try:
        links = recorded_string_mapping(state, "links")
    except SetupError as error:
        links = {}
        record("link_state", False, str(error), f"link state: invalid ({error})")
    expected_links = [
        (
            str(client_skills_dir(home, str(client)) / canonical),
            canonical,
            relative,
        )
        for client in clients
        if client in CLIENTS
        for canonical, relative in selected_bundle
    ]
    for link_text, canonical, relative in sorted(expected_links):
        link = Path(link_text)
        preserved = canonical in preserved_skills
        recorded_target = links.get(link_text, "")
        target_text = recorded_target if preserved else str(source / relative)
        target = Path(target_text) if target_text else source / relative
        if not recorded_target or recorded_target != target_text:
            record(
                f"link:{link}",
                False,
                "missing from installer state",
                f"link {link}: missing from installer state",
            )
            continue
        if preserved and not valid_preserved_skill_target(target):
            record(
                f"link:{link}",
                False,
                f"preserved target unavailable: {target}",
                f"link {link}: preserved target unavailable ({target})",
            )
            continue
        if same_target(link, target):
            detail = f"preserved: {target}" if preserved else "ok"
            record(f"link:{link}", True, detail, f"link {link}: {detail}")
        else:
            record(
                f"link:{link}",
                False,
                "missing or stale",
                f"link {link}: missing or stale",
            )
    for client in clients:
        if client not in CLIENTS:
            continue
        for retired_name, relative in RETIRED_SKILL_LINKS:
            retired = client_skills_dir(home, str(client)) / retired_name
            if same_target(retired, source / relative):
                record(
                    f"retired_link:{retired}",
                    False,
                    "still present",
                    f"retired link {retired}: still present",
                )
        legacy = client_skills_dir(home, str(client)) / "openubmc-environment"
        if legacy.is_symlink():
            record(
                f"legacy_link:{legacy}",
                False,
                "still present",
                f"legacy link {legacy}: still present",
            )

    try:
        tool_dirs = list(recorded_string_list(state, "tool_dirs"))
    except SetupError as error:
        tool_dirs = []
        record(
            "tool_directories",
            False,
            str(error),
            f"tool directories: invalid state ({error})",
        )
    config_dir = openubmc_config_dir(home)
    config_dir_ok = (
        config_dir.is_dir()
        and not config_dir.is_symlink()
        and config_dir.stat().st_uid == os.getuid()
        and stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    )
    config_detail = "ok" if config_dir_ok else "missing or unsafe"
    record(
        "configuration_directory",
        config_dir_ok,
        config_detail,
        f"configuration directory: {config_detail}",
    )
    env_file = config_dir / "env.sh"
    expected_env = render_env(map(str, tool_dirs))
    env_ok = (
        env_file.is_file()
        and not env_file.is_symlink()
        and stat.S_IMODE(env_file.stat().st_mode) == 0o600
        and env_file.read_text(encoding="utf-8") == expected_env
    )
    env_detail = "ok" if env_ok else "missing, changed, or unsafe"
    record("environment_hook", env_ok, env_detail, f"environment hook: {env_detail}")

    profile_values = state.get("profiles", [])
    if not isinstance(profile_values, list):
        profiles = []
        record(
            "profiles",
            False,
            "invalid state",
            "profiles: invalid state",
        )
    else:
        invalid_profiles = [
            repr(profile)
            for profile in profile_values
            if not isinstance(profile, str)
        ]
        profiles = [
            profile for profile in profile_values if isinstance(profile, str)
        ]
        if invalid_profiles:
            detail = "invalid entries: " + ", ".join(invalid_profiles)
            record("profiles", False, detail, "profiles: " + detail)
    required_profiles = {str(home / ".bashrc"), str(home / ".profile")}
    if not required_profiles.issubset(set(map(str, profiles))):
        record(
            "profiles",
            False,
            "installer state is incomplete",
            "profiles: installer state is incomplete",
        )
    for profile_text in profiles:
        profile = Path(profile_text)
        content = profile.read_text(encoding="utf-8", errors="ignore") if profile.is_file() else ""
        installed = (
            not profile.is_symlink()
            and content.count(MARKER_START) == 1
            and content.count(MARKER_END) == 1
            and OLD_MARKER_START not in content
            and LEGACY_CREDENTIALS_START not in content
        )
        detail = "ok" if installed else "missing hook"
        record(f"profile:{profile}", installed, detail, f"profile {profile}: {detail}")

    credentials_ok, credentials_detail = credentials_status(credentials_path(home))
    record(
        "credentials",
        credentials_ok,
        credentials_detail,
        f"credentials: {credentials_detail}",
        category="credentials",
    )
    tooling = inspect_tooling(map(str, tool_dirs), clients)
    for tool, available in tooling["required"].items():
        detail = "ok" if available else "missing"
        record(f"tool:{tool}", available, detail, f"tool {tool}: {detail}")
    for tool, available in tooling["conditional"].items():
        detail = "ok" if available else CONDITIONAL_TOOLS[tool]
        record(
            f"tool:{tool}",
            available,
            detail,
            f"tool {tool}: {detail}",
            category="tooling",
            blocking=False,
        )
    for tool, available in tooling["recommended"].items():
        detail = "ok" if available else RECOMMENDED_TOOLS[tool]
        record(
            f"tool:{tool}",
            available,
            detail,
            f"tool {tool}: {detail}",
            category="tooling",
            blocking=False,
        )
    for client, available in tooling["clients"].items():
        detail = (
            "ok"
            if available
            else (
                "missing; Skill and MCP configuration is staged, but the client "
                "cannot be launched directly in this environment"
            )
        )
        record(
            f"client:{client}",
            available,
            detail,
            f"client {client}: {detail}",
            category="client",
            blocking=False,
        )

    def state_record_map(key: str, label: str) -> dict[str, Any]:
        try:
            return recorded_client_records(state, key)
        except SetupError as error:
            record(key, False, str(error), f"{label} state: invalid ({error})")
            return {}

    url = str(state.get("knowledge_url", state.get("studio_url", "")))
    knowledge_mcp_state = state_record_map("mcp", "openUBMC KB MCP")
    runtime_mcp_state = state_record_map("runtime_mcp", "Target Runtime MCP")
    configured_external: list[str] = []
    configured_managed_stdio: list[str] = []
    configured_managed_http: list[str] = []
    if manage_knowledge_mcp:
        for client in clients:
            if (
                client in SUPPORTED_MCP_CLIENTS
                and not valid_client_ownership_record(
                    knowledge_mcp_state.get(client)
                )
            ):
                detail = "ownership missing or invalid in installer state; run repair"
                record(
                    f"mcp:{client}",
                    False,
                    detail,
                    f"mcp {client}: {detail}",
                )
                continue
            client_record = knowledge_mcp_state.get(client, {})
            if not isinstance(client_record, dict):
                client_record = {}
            if client == "codex":
                configured = check_toml_mcp(
                    home / ".codex" / "config.toml", url, client_record
                )
            elif client == "claude":
                configured = check_json_mcp(home / ".claude.json", url, client_record)
            else:
                record(
                    "mcp:openclaw",
                    False,
                    "adapter unavailable",
                    "mcp openclaw: adapter unavailable (non-blocking)",
                    blocking=False,
                )
                continue
            external = client_record.get("ownership") == "external"
            detail = "external (preserved)" if configured and external else (
                "ok" if configured else "missing or stale"
            )
            record(f"mcp:{client}", configured, detail, f"mcp {client}: {detail}")
            if configured and external:
                configured_external.append(client)
            elif configured and client_record.get("command"):
                configured_managed_stdio.append(client)
            elif configured and client in SUPPORTED_MCP_CLIENTS:
                configured_managed_http.append(client)
    else:
        record(
            "knowledge_configuration",
            True,
            "not managed by this profile",
            (
                f"{KNOWLEDGE_MCP_NAME} configuration: not managed by "
                f"{selected_profile} profile"
            ),
            category="knowledge",
            blocking=False,
        )

    launcher = Path(str(runtime_report.get("launcher_path", runtime_launcher_path(home))))
    runtime_mcp_configured = True
    for client in clients:
        if (
            client in SUPPORTED_MCP_CLIENTS
            and not valid_client_ownership_record(
                runtime_mcp_state.get(client)
            )
        ):
            configured = False
            config_detail = (
                "ownership missing or invalid in installer state; run repair"
            )
        elif client == "codex":
            configured = check_toml_runtime_mcp(
                home / ".codex" / "config.toml", launcher
            )
            config_detail = "ok" if configured else "missing or stale"
        elif client == "claude":
            configured = check_json_runtime_mcp(home / ".claude.json", launcher)
            config_detail = "ok" if configured else "missing or stale"
        else:
            record(
                "runtime_mcp:openclaw",
                False,
                "adapter unavailable",
                "Target Runtime MCP openclaw: adapter unavailable (non-blocking)",
                blocking=False,
            )
            continue
        runtime_mcp_configured = runtime_mcp_configured and configured
        record(
            f"runtime_mcp:{client}",
            configured,
            config_detail,
            f"Target Runtime MCP {client}: {config_detail}",
        )

    if runtime_ok:
        runtime_mcp_healthy, runtime_mcp_detail, runtime_tools = runtime_mcp_health(
            launcher, home
        )
    else:
        runtime_mcp_healthy, runtime_mcp_detail, runtime_tools = (
            False,
            "Runtime installation is not ready",
            [],
        )
    runtime_mcp_ready = runtime_mcp_configured and runtime_mcp_healthy
    record(
        "runtime_mcp_health",
        runtime_mcp_ready,
        runtime_mcp_detail,
        f"Target Runtime MCP health: {runtime_mcp_detail}",
    )

    if "openubmc-debug" in preserved_skills:
        debug_roots = {
            Path(links[str(client_skills_dir(home, client) / "openubmc-debug")])
            for client in clients
            if str(client_skills_dir(home, client) / "openubmc-debug") in links
        }
    else:
        debug_roots = {source / "openubmc-debug"}
    context_cli_ready = bool(debug_roots) and all(
        (debug_root / relative).is_file()
        for debug_root in debug_roots
        for relative in (
            "scripts/_target_runtime_adapter.py",
            "scripts/target_runtime_cli.py",
            "scripts/target_runtime_mcp.py",
            "scripts/workflow_remote.py",
        )
    )
    context_cli_detail = (
        "ok" if context_cli_ready else "missing Debug Context Runtime CLI adapter"
    )
    record(
        "context_cli_engine",
        context_cli_ready,
        context_cli_detail,
        f"Target Runtime Context CLI: {context_cli_detail}",
    )
    engine_ready = runtime_mcp_ready or context_cli_ready
    if manage_knowledge_mcp:
        if configured_managed_stdio:
            knowledge_state = state.get("knowledge_mcp", {})
            knowledge_launcher = Path(
                str(
                    knowledge_state.get("launcher_path", knowledge_launcher_path(home))
                    if isinstance(knowledge_state, Mapping)
                    else knowledge_launcher_path(home)
                )
            )
            healthy, detail, knowledge_tools, configured_credentials = knowledge_mcp_health(
                knowledge_launcher, home
            )
            health_text = detail if healthy else "unavailable (non-blocking): " + detail
            knowledge_report = {
                "managed": True,
                "healthy": healthy,
                "configured": configured_credentials,
                "transport": "stdio",
                "detail": detail,
                "tools": knowledge_tools,
                "clients": configured_managed_stdio,
            }
        elif configured_managed_http:
            healthy, detail = knowledge_http_health(url)
            health_text = detail if healthy else "unavailable (non-blocking): " + detail
            knowledge_report: dict[str, object] = {
                "url": url,
                "managed": True,
                "healthy": healthy,
                "transport": "http",
                "detail": detail,
            }
        elif configured_external:
            healthy = True
            detail = (
                "external stdio configuration preserved for "
                + ", ".join(configured_external)
                + "; the client starts it on demand"
            )
            health_text = detail
            knowledge_report = {
                "url": url,
                "managed": True,
                "healthy": True,
                "transport": "external-stdio",
                "detail": detail,
                "clients": configured_external,
            }
        else:
            healthy = False
            detail = "no supported MCP client adapter is configured"
            health_text = "unavailable (non-blocking): " + detail
            knowledge_report = {
                "url": url,
                "managed": True,
                "healthy": False,
                "transport": "unavailable",
                "detail": detail,
            }
        record(
            "knowledge_health",
            healthy,
            detail,
            f"{KNOWLEDGE_MCP_NAME} health: {health_text}",
            category="knowledge",
            blocking=False,
        )
    else:
        healthy = False
        detail = f"not managed by {selected_profile} profile"
        knowledge_report = {
            "url": url,
            "managed": False,
            "healthy": False,
            "transport": "external",
            "detail": detail,
        }

    core_ok = all(
        check["ok"]
        for check in checks
        if check["category"] == "core" and check["blocking"]
    )
    return {
        "ok": core_ok and credentials_ok,
        "readiness": {
            "core": core_ok,
            "credentials": credentials_ok,
            "runtime": runtime_ok,
            "mcp": runtime_mcp_ready,
            "engine": engine_ready,
            "knowledge": healthy,
            "studio": healthy,
            "tooling": bool(tooling["ready"]),
            "client": bool(tooling["client_ready"]),
            "password_ssh": bool(tooling["conditional"]["sshpass"]),
            "source_search": bool(tooling["recommended"]["rg"]),
        },
        "source": {
            "path": str(source),
            "mode": source_mode,
            "valid": source_valid,
            "requested_ref": requested_ref,
            "ref_kind": ref_kind,
            "resolved_commit": resolved_commit,
            "expected_commit": expected_commit,
            "current_commit": actual_commit,
            "dirty": dirty,
            "dirty_scope": dirty_scope,
        },
        "skill_profile": selected_profile,
        "preserved_skills": list(preserved_skills),
        "clients": [str(client) for client in clients],
        "runtime": runtime_report,
        "runtime_mcp": {
            "healthy": runtime_mcp_ready,
            "configured": runtime_mcp_configured,
            "detail": runtime_mcp_detail,
            "tools": runtime_tools,
        },
        "engines": {
            "mcp": runtime_mcp_ready,
            "cli": context_cli_ready,
            "one_shot": context_cli_ready,
        },
        "tooling": tooling,
        "next_actions": [
            *tooling_next_actions(
                tooling,
                credentials_ok=credentials_ok,
            ),
            *knowledge_next_actions(knowledge_report),
        ],
        "checks": checks,
        "knowledge_mcp": knowledge_report,
        "studio": knowledge_report,
        "_messages": messages,
    }


def perform_check(args: argparse.Namespace) -> int:
    report = collect_check_report(args)
    messages = report.pop("_messages", [])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for message in messages:
            print(message)
    return 0 if report["ok"] else 1


def restore_recorded_lifecycle(
    args: argparse.Namespace,
) -> RecordedInstall:
    home = args.home.expanduser().absolute()
    recorded = decode_recorded_install(load_state(home))
    if args.command in {"update", "rollback"} and recorded.source_mode != "managed":
        raise SetupError(
            "source is linked; update or restore the checkout yourself, then use refresh"
        )
    if args.command == "refresh" and recorded.source_mode != "linked":
        raise SetupError("source is installer-managed; use update instead of refresh")
    if args.command not in {"repair", "update", "rollback", "refresh"}:
        raise SetupError(f"unsupported recorded lifecycle command: {args.command}")

    args.home = home
    args.source = None
    args.repo_url = recorded.repo_url
    args.ref = recorded.ref
    args.clients = ",".join(recorded.clients or ("codex",))
    args.skill_profile = recorded.profile.name
    args.knowledge_url = recorded.knowledge_url
    args.target = recorded.target
    args.skip_credentials = True
    return recorded


def perform_recorded_lifecycle(args: argparse.Namespace) -> int:
    recorded = restore_recorded_lifecycle(args)
    if args.command == "rollback":
        if not recorded.rollback_commit:
            raise SetupError("no previous known-good managed revision is recorded")
        checkout_managed_revision(
            recorded.source_root,
            recorded.rollback_commit,
            args.dry_run,
            recorded.profile.bundle,
        )
    return perform_install(
        args,
        update=args.command == "update",
        recorded_state=recorded,
        repair_only=True,
    )


def perform_repair(args: argparse.Namespace) -> int:
    return perform_recorded_lifecycle(args)


def perform_update(args: argparse.Namespace) -> int:
    return perform_recorded_lifecycle(args)


def perform_rollback(args: argparse.Namespace) -> int:
    return perform_recorded_lifecycle(args)


def perform_refresh(args: argparse.Namespace) -> int:
    return perform_recorded_lifecycle(args)


def perform_credentials(args: argparse.Namespace) -> int:
    home = args.home.expanduser().absolute()
    args.home = home
    validate_openubmc_config_dir(home)
    if args.kb or args.kb_config is not None:
        if args.kb_config is not None:
            result = ensure_knowledge_config(home, args.kb_config, args.dry_run)
        else:
            if args.non_interactive or not sys.stdin.isatty():
                raise SetupError("openUBMC KB credential configuration requires a TTY or --kb-config")
            path = knowledge_config_path(home)
            document = default_knowledge_config()
            if path.is_file() and not path.is_symlink():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise SetupError(f"invalid openUBMC KB configuration: {path}") from error
                if isinstance(current, dict):
                    document.update(current)
            current_username = str(document.get("username", "")).strip()
            prompt = "openUBMC OneID username"
            if current_username:
                prompt += f" [{current_username}]"
            username = input(prompt + ": ").strip() or current_username
            password = getpass.getpass("openUBMC OneID password: ")
            if not username or not password:
                raise SetupError("openUBMC KB username and password are required")
            document["username"] = username
            document["password"] = password
            if args.dry_run:
                print(f"would update openUBMC KB credentials in {path}")
            else:
                atomic_write(
                    path,
                    json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    0o600,
                )
            result = "configured"
        print(f"openUBMC KB credentials: {result}")
        return 0
    result = apply_credentials_plan(prepare_credentials(args), args.dry_run)
    print(f"credentials: {result}")
    return 0


def perform_uninstall(args: argparse.Namespace) -> int:
    home = args.home.expanduser().absolute()
    state = load_state(home)
    recorded = decode_recorded_install(state)
    missing_ownership = missing_client_ownership_records(recorded)
    if missing_ownership:
        raise SetupError(
            "installer state has missing or invalid client ownership records: "
            + ", ".join(missing_ownership)
            + "; run repair before uninstalling"
        )
    backups = backup_path(home)
    links = recorded.links
    preserved_link_paths = {
        str(client_skills_dir(home, client) / canonical)
        for client in recorded.clients
        if client in CLIENTS
        for canonical in recorded.preserved_skills
    }
    managed_link_paths: set[str] = set()
    for link_text, target_text in sorted(links.items()):
        link = Path(link_text)
        target = Path(target_text)
        if link_text in preserved_link_paths:
            if same_target(link, target):
                print(f"preserving external Skill link {link}")
            continue
        if same_target(link, target):
            managed_link_paths.add(str(link))
            if args.dry_run:
                print(f"would remove managed link {link}")
            else:
                link.unlink()
    for profile_text in recorded.profiles:
        profile = Path(profile_text)
        if not profile.is_file() or profile.is_symlink():
            continue
        original = profile.read_text(encoding="utf-8", errors="ignore")
        updated = remove_profile_hook(original)
        if updated != original:
            backup_file(profile, backups, args.dry_run)
            if args.dry_run:
                print(f"would remove environment hook from {profile}")
            else:
                atomic_write(profile, updated, None)
    env_file = openubmc_config_dir(home) / "env.sh"
    if env_file.is_file() and not env_file.is_symlink():
        expected = render_env(recorded.tool_dirs)
        if env_file.read_text(encoding="utf-8", errors="ignore") != expected:
            print(f"warning: preserving changed environment file {env_file}")
        elif args.dry_run:
            print(f"would remove {env_file}")
        else:
            env_file.unlink()
    for client in recorded.clients:
        record = recorded.mcp.get(client, {})
        runtime_record = recorded.runtime_mcp.get(client, {})
        if client == "codex":
            if recorded.profile.manages_knowledge_mcp:
                remove_toml_knowledge_mcp(
                    home / ".codex" / "config.toml",
                    record,
                    backups,
                    args.dry_run,
                )
            remove_toml_stdio_mcp(
                home / ".codex" / "config.toml",
                runtime_record,
                backups,
                args.dry_run,
            )
        elif client == "claude":
            if recorded.profile.manages_knowledge_mcp:
                remove_json_knowledge_mcp(
                    home / ".claude.json",
                    record,
                    backups,
                    args.dry_run,
                )
            remove_json_stdio_mcp(
                home / ".claude.json", runtime_record, backups, args.dry_run
            )
    runtime_state = recorded.runtime
    if runtime_state:
        install_root = runtime_install_root(home)
        recorded_package = Path(str(runtime_state.get("package_path", "")))
        recorded_launcher = Path(str(runtime_state.get("launcher_path", "")))
        if (
            recorded_package == runtime_package_path(home)
            and recorded_launcher == runtime_launcher_path(home)
        ):
            if install_root.is_symlink():
                print(f"warning: preserving unexpected Target Runtime symlink {install_root}")
            elif install_root.exists():
                if args.dry_run:
                    print(f"would remove Target Runtime installation {install_root}")
                else:
                    shutil.rmtree(install_root)
    if recorded.profile.manages_knowledge_mcp:
        install_root = knowledge_install_root(home)
        knowledge_state = state.get("knowledge_mcp", {})
        recorded_launcher = Path(
            str(knowledge_state.get("launcher_path", ""))
            if isinstance(knowledge_state, Mapping)
            else ""
        )
        if recorded_launcher == knowledge_launcher_path(home):
            if install_root.is_symlink():
                print(f"warning: preserving unexpected openUBMC KB MCP symlink {install_root}")
            elif install_root.exists():
                if args.dry_run:
                    print(f"would remove openUBMC KB MCP installation {install_root}")
                else:
                    shutil.rmtree(install_root)
    if args.purge_credentials:
        for path in (credentials_path(home), knowledge_config_path(home)):
            if path.is_file() and not path.is_symlink():
                if args.dry_run:
                    print(f"would remove credentials {path}")
                else:
                    path.unlink()
    source = recorded.source_root
    if (
        recorded.source_mode == "managed"
        and source == managed_source_dir(home)
    ):
        if source.is_symlink():
            print(f"warning: preserving unexpected managed source symlink {source}")
        elif source.exists():
            consumers = remaining_links_into_source(
                home,
                CLIENTS,
                source,
                managed_link_paths,
            )
            if consumers:
                detail = ", ".join(str(link) for link in consumers)
                print(
                    "warning: preserving managed source checkout still used by "
                    f"unmanaged Skill links: {detail}"
                )
            else:
                try:
                    validate_source(
                        source,
                        recorded.profile.bundle,
                    )
                    remote = git_output(source, "remote", "get-url", "origin")
                    expected_remote = recorded.repo_url
                    if normalized_repo_url(remote) != normalized_repo_url(expected_remote):
                        raise SetupError(
                            "managed source origin no longer matches installer state"
                        )
                except SetupError as error:
                    print(
                        f"warning: preserving unverifiable managed source {source}: {error}"
                    )
                else:
                    if git_dirty(source):
                        print(f"warning: preserving dirty managed source checkout {source}")
                    elif args.dry_run:
                        print(f"would remove managed source checkout {source}")
                    else:
                        shutil.rmtree(source)
    path = state_path(home)
    if args.dry_run:
        print(f"would remove installer state {path}")
    else:
        path.unlink(missing_ok=True)
    print("openUBMC workflow would be removed" if args.dry_run else "openUBMC workflow removed")
    return 0


def workflow_json_summary(
    state: Mapping[str, object], *, installed: bool
) -> dict[str, object]:
    recorded = decode_recorded_install(state)
    return {
        "installed": installed,
        "skill_profile": recorded.profile.name,
        "skill_count": len(recorded.profile.bundle),
        "preserved_skills": list(recorded.preserved_skills),
        "clients": list(recorded.clients),
        "source": {
            "path": str(recorded.source_root),
            "mode": recorded.source_mode,
            "commit": recorded.source_commit,
            "requested_ref": recorded.requested_ref,
            "ref_kind": recorded.ref_kind,
            "resolved_commit": recorded.resolved_commit,
            "rollback_commit": recorded.rollback_commit,
        },
        "runtime": {
            "api_version": str(recorded.runtime.get("api_version", "")),
            "content_digest": str(recorded.runtime.get("content_digest", "")),
        },
        "openubmc_kb_managed": recorded.profile.manages_knowledge_mcp,
        "openubmc_kb": {
            "version": str(recorded_object(state, "knowledge_mcp").get("version", "")),
            "launcher": str(recorded_object(state, "knowledge_mcp").get("launcher_path", "")),
        },
    }


def lifecycle_json_payload(
    args: argparse.Namespace,
    result: int,
    output: str,
) -> dict[str, object]:
    home = args.home.expanduser().absolute()
    payload: dict[str, object] = {
        "ok": result == 0,
        "command": args.command,
        "dry_run": bool(args.dry_run),
        "messages": [line for line in output.splitlines() if line.strip()],
    }
    credentials_ok, credentials_detail = credentials_status(credentials_path(home))
    payload["credentials"] = {
        "configured": credentials_ok,
        "detail": credentials_detail,
        "preserved": args.command == "uninstall" and not bool(args.purge_credentials),
    }
    tooling = getattr(args, "_tooling_report", None)
    if not isinstance(tooling, dict):
        current_state = try_load_state(home)
        if current_state is not None:
            current_install = decode_recorded_install(current_state)
            tooling = inspect_tooling(
                current_install.tool_dirs,
                current_install.clients,
            )
        else:
            tooling = inspect_tooling((), ())
    payload["tooling"] = tooling
    knowledge_report = getattr(args, "_knowledge_mcp_report", None)
    if not isinstance(knowledge_report, dict):
        knowledge_report = {
            "managed": False,
            "healthy": False,
            "transport": "unknown",
            "detail": "not checked by this lifecycle command",
        }
    payload["knowledge_mcp"] = knowledge_report
    payload["next_actions"] = (
        []
        if args.command == "uninstall" and result == 0
        else [
            *tooling_next_actions(
                tooling,
                credentials_ok=credentials_ok,
            ),
            *knowledge_next_actions(knowledge_report),
        ]
    )
    state = try_load_state(home)
    if state is None:
        payload["workflow"] = {"installed": False}
    else:
        payload["workflow"] = workflow_json_summary(state, installed=True)
    planned_state = getattr(args, "_planned_workflow_state", None)
    if args.dry_run and isinstance(planned_state, dict):
        planned = workflow_json_summary(planned_state, installed=False)
        planned.pop("installed", None)
        planned["action"] = args.command
        payload["planned_workflow"] = planned
    return payload


def perform_json_lifecycle(
    args: argparse.Namespace,
    operation: Callable[[argparse.Namespace], int],
) -> int:
    output = io.StringIO()
    with redirect_stdout(output):
        result = operation(args)
    print(
        json.dumps(
            lifecycle_json_payload(args, result, output.getvalue()),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        operations = {
            "install": perform_install,
            "check": perform_check,
            "repair": perform_repair,
            "update": perform_update,
            "rollback": perform_rollback,
            "refresh": perform_refresh,
            "credentials": perform_credentials,
            "uninstall": perform_uninstall,
        }
        operation = operations[args.command]
        if args.json and args.command != "check":
            return perform_json_lifecycle(args, operation)
        return operation(args)
    except (SetupError, OSError, ValueError) as error:
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "command": args.command, "error": str(error)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
