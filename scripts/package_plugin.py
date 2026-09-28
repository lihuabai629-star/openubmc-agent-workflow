#!/usr/bin/env python3
"""Assemble a Codex plugin from one immutable repository commit."""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGED_PROMPT_BUDGET_BYTES = 128 * 1024


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.PIPE)


def source_files(source: Path, ref: str) -> tuple[str, dict[str, bytes]]:
    commit = git(source, 'rev-parse', '--verify', f'{ref}^{{commit}}').decode().strip()
    files = {}
    with tarfile.open(fileobj=io.BytesIO(git(source, 'archive', commit))) as archive:
        for entry in archive:
            if entry.isdir():
                continue
            if not entry.isfile():
                raise ValueError(f'source contains unsupported entry: {entry.name}')
            files[entry.name] = archive.extractfile(entry).read()
    return commit, files


def protect_python_entrypoints(payload: dict[str, bytes]) -> None:
    helper = 'skills/openubmc-debug/scripts/_plugin_entrypoint.py'
    for name, content in list(payload.items()):
        path = PurePosixPath(name)
        if path.suffix != '.py' or 'tests' in path.parts or name == 'scripts/pluginctl.py':
            continue
        tree = ast.parse(content)
        has_main = any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                       and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__'
                       for node in tree.body)
        if not has_main and not (path.parent.name in {'scripts', 'tools'} and not path.name.startswith('_')):
            continue
        header = 0
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                header = node.end_lineno
            elif isinstance(node, ast.ImportFrom) and node.module == '__future__':
                header = node.end_lineno
            else:
                break
        relative = posixpath.relpath(helper, str(path.parent))
        guard = ("\nif __name__ == '__main__':\n"
                 "    import sys as _openubmc_sys\n"
                 "    _openubmc_sys.dont_write_bytecode = True\n"
                 "    import runpy as _openubmc_runpy\n"
                 "    from pathlib import Path as _openubmc_Path\n"
                 f"    _openubmc_guard = _openubmc_Path(__file__).parent / {relative!r}\n"
                 "    _openubmc_cache = _openubmc_runpy.run_path(str(_openubmc_guard))['initialize'](__file__)\n\n")
        lines = content.decode().splitlines(keepends=True)
        payload[name] = (''.join(lines[:header]) + guard + ''.join(lines[header:])).encode()


def enforce_prompt_budget(payload: dict[str, bytes]) -> None:
    prompt_files = {
        name: content
        for name, content in payload.items()
        if name.startswith('skills/')
        and (name.endswith('/SKILL.md') or name.endswith('/agents/openai.yaml'))
    }
    prompt_bytes = sum(len(content) for content in prompt_files.values())
    if prompt_bytes > PACKAGED_PROMPT_BUDGET_BYTES:
        raise ValueError(
            'packaged prompt budget exceeded: '
            f'{prompt_bytes} > {PACKAGED_PROMPT_BUDGET_BYTES} bytes'
        )


def assemble(source: Path, ref: str) -> dict[str, bytes]:
    commit, source_content = source_files(source, ref)
    workflow = json.loads(source_content['workflow.json'])
    payload = {}
    skill_names = []
    for skill in workflow['skills']:
        if skill['name'] not in workflow['profiles']['full']:
            continue
        skill_names.append(skill['name'])
        root = skill['path']
        manifest = json.loads(source_content[f'{root}/skill.json'])
        for relative in sorted(set(manifest['files']) | {'skill.json'}):
            if PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts:
                raise ValueError(f'invalid Skill member: {relative}')
            payload[f'skills/{root}/{relative}'] = source_content[f'{root}/{relative}']
    for name, content in source_content.items():
        if name.startswith(('openubmc-target-runtime/openubmc_target_runtime/', 'openubmc-target-runtime/tools/')):
            if '__pycache__' in PurePosixPath(name).parts or not name.endswith('.py'):
                continue
            payload[f'skills/{name}'] = content
        if name.startswith('openubmc-kb-mcp/src/') or name in ('openubmc-kb-mcp/package.json', 'openubmc-kb-mcp/package-lock.json'):
            payload[name] = content
        if name.startswith('plugin/openubmc/'):
            payload[name.removeprefix('plugin/openubmc/')] = content
    payload['requirements.lock'] = source_content['requirements-ci.lock']
    payload['PYTHON.md'] = source_content['plugin/python-entrypoints.md']
    payload['workflow.json'] = source_content['workflow.json']
    for name in ('install_plugin.py', 'plugin_admin.py', 'plugin_archive.py'):
        payload['scripts/'+name] = source_content['scripts/'+name]
    payload['skills/openubmc-environment-setup/SKILL.md'] = source_content['plugin/environment-support.md']
    payload['skills/openubmc-environment-setup/references/credential-exposure-response.md'] = source_content['docs/credential-exposure-response.md']
    # The support directory retains the canonical sibling layout required by
    # existing public Skill helpers. Its descriptor is generated from the recipe.
    payload['skills/openubmc-target-runtime/SKILL.md'] = source_content['plugin/runtime-support.md']
    enforce_prompt_budget(payload)
    manifest = json.loads(payload['.codex-plugin/plugin.json'])
    manifest['version'] = workflow['version']
    payload['.codex-plugin/plugin.json'] = canonical(manifest)
    protect_python_entrypoints(payload)

    # Reuse the qualified launcher's validation and verified snapshot, with
    # location-derived roots instead of installer-specific absolute paths.
    with tempfile.TemporaryDirectory(prefix='openubmc-plugin-recipe-') as temporary:
        root = Path(temporary)
        for name, content in payload.items():
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        installer_path = root/'skills/openubmc-environment-setup/scripts/install_environment.py'
        spec = importlib.util.spec_from_file_location('plugin_installer_recipe', installer_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        plan = module.build_runtime_plan(root, root/'skills', source_commit=commit)
        plan['package_path'] = str(root/'skills/openubmc-target-runtime/openubmc_target_runtime')
        launchers = [('launch_runtime.py', 'target_runtime_mcp.py')]
        # Keep immutable builds of older refs valid: they do not contain hooks.
        if 'skills/openubmc-debug/scripts/host_continuity.py' in payload:
            launchers.append(('launch_host_hook.py', 'host_continuity.py'))
        for launcher_name, entrypoint_name in launchers:
            entrypoint = root/'skills/openubmc-debug/scripts'/entrypoint_name
            selected = dict(plan, mcp_entrypoint=str(entrypoint), mcp_entrypoint_digest=
                            module.file_content_digest(entrypoint, domain=b'openubmc-mcp-entrypoint-v1\0'))
            launcher = module.render_runtime_launcher(selected)
            replacements = {
                'PACKAGE_ROOT': "Path(__file__).resolve().parents[1] / 'skills/openubmc-target-runtime/openubmc_target_runtime'",
                'MCP_ENTRYPOINT': f"Path(__file__).resolve().parents[1] / 'skills/openubmc-debug/scripts/{entrypoint_name}'",
                'COMPOSITION_SOURCE': "Path(__file__).resolve().parents[1] / 'skills'",
            }
            for variable, expression in replacements.items():
                lines = launcher.splitlines(keepends=True)
                found = False
                for index, line in enumerate(lines):
                    if line.startswith(f'{variable} = Path('):
                        lines[index] = f'{variable} = {expression}\n'
                        found = True
                        break
                if not found:
                    raise ValueError(f'launcher recipe no longer supports relocation: {variable}')
                launcher = ''.join(lines)
            if str(root) in launcher:
                raise ValueError('launcher leaked a build path')
            payload['scripts/'+launcher_name] = launcher.encode()
    inventory = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(payload.items())}
    lock = {'schema': 'openubmc.codex-plugin.v1', 'name': 'openubmc', 'version': workflow['version'],
            'source_commit': commit, 'skills': sorted(skill_names), 'files': inventory,
            'manifest_digest': hashlib.sha256(payload['.codex-plugin/plugin.json']).hexdigest(),
            'qualification_refs': ['docs/runtime-stability-qualification.md', 'docs/codex-adoption-qualification.md']}
    lock['content_digest'] = hashlib.sha256(canonical(lock)).hexdigest()
    payload['plugin-lock.json'] = canonical(lock)
    return payload


def build(source: Path, ref: str, output: Path) -> dict:
    files = assemble(source, ref)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as buffer:
        with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0, filename='') as gzip_file:
            with tarfile.open(fileobj=gzip_file, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                for name, content in sorted(files.items()):
                    info = tarfile.TarInfo('openubmc/' + name)
                    info.size = len(content)
                    info.mode = 0o755 if name.startswith('scripts/') or name.endswith('.sh') else 0o644
                    archive.addfile(info, io.BytesIO(content))
        buffer.seek(0)
        data = buffer.read()
    output.write_bytes(data)
    lock = json.loads(files['plugin-lock.json'])
    return {'ok': True, 'source_commit': lock['source_commit'], 'version': lock['version'],
            'content_digest': lock['content_digest'], 'archive_sha256': hashlib.sha256(data).hexdigest(),
            'files': len(files), 'skills': lock['skills']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build')
    command.add_argument('--source', type=Path, default=ROOT)
    command.add_argument('--source-ref', default='HEAD')
    command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build(args.source, args.source_ref, args.output)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
