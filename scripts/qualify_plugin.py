#!/usr/bin/env python3
"""Qualify an immutable archive through a clean native Codex installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from package_plugin import build
from plugin_archive import canonical, verify_directory

ROOT = Path(__file__).resolve().parents[1]


def command(argv: list[str], env: dict[str, str]) -> dict | list:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=360)
    if result.returncode:
        raise ValueError('qualification command failed: '+str(argv[:3])+': '+result.stderr[-3000:])
    return json.loads(result.stdout)


def qualify(source: Path, ref: str, archive: Path) -> dict:
    report = build(source, ref, archive)
    codex_version = subprocess.check_output(['codex', '--version'], text=True).strip()
    if codex_version != 'codex-cli 0.153.4':
        raise ValueError('plugin qualification requires codex-cli 0.153.4')
    with tempfile.TemporaryDirectory(prefix='openubmc-plugin-qualification-') as temporary:
        root = Path(temporary)
        home, codex = root/'home', root/'custom-codex'
        codex.mkdir()
        config = codex/'config.toml'
        config.write_text('model = "plugin-qualification"\n')
        secret = home/'.config/openubmc/credentials.env'
        secret.parent.mkdir(parents=True)
        secret.write_text('# external credentials preserved\n')
        env = dict(os.environ, CODEX_HOME=str(codex), XDG_CONFIG_HOME=str(home/'.config'),
                   XDG_DATA_HOME=str(home/'.local/share'), XDG_CACHE_HOME=str(home/'.cache'),
                   OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(root/'runtime-state'),
                   OPENUBMC_MCP_LIFECYCLE_DIR=str(root/'lifecycle'), OPENUBMC_MCP_FORMAL_RUN='0')
        for key in ('OPENUBMC_CREDENTIALS_FILE', 'NODE_OPTIONS', 'NODE_PATH', 'NPM_CONFIG_NODE_OPTIONS'):
            env.pop(key, None)
        repeated = root/'repeated.tar.gz'
        build(source, ref, repeated)
        if repeated.read_bytes() != archive.read_bytes():
            raise ValueError('archive is not deterministic')
        install_cli = [sys.executable, '-I', str(source/'scripts/install_plugin.py'), str(archive), '--sha256', report['archive_sha256'], '--home', str(home), '--codex-home', str(codex)]
        installed = command(install_cli, env)
        plugin = Path(installed['codex_install']['installedPath'])
        lock = verify_directory(plugin)
        if lock['source_commit'] != report['source_commit'] or lock['content_digest'] != report['content_digest']:
            raise ValueError('installed identity does not match archive')
        doctor = command([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'doctor'], env)
        if not doctor['startup_ready']:
            raise ValueError('installed MCP startup failed')
        servers = command(['codex', 'mcp', 'list', '--json'], env)
        for name in ('openubmc-target-runtime', 'openubmc-kb'):
            server = next(row for row in servers if row['name'] == name)
            if Path(server['transport']['cwd']).resolve() != plugin:
                raise ValueError('MCP launcher does not resolve to the installed plugin')
        admin = [sys.executable, '-I', str(source/'scripts/plugin_admin.py')]
        command([*admin, 'uninstall', '--home', str(home), '--codex-home', str(codex)], env)
        listing = command(['codex', 'plugin', 'list', '--json'], env)
        if any(row['pluginId'].startswith('openubmc@') for row in listing['installed']):
            raise ValueError('Codex kept the plugin installed after uninstall')
        if secret.read_text() != '# external credentials preserved\n' or 'plugin-qualification' not in config.read_text():
            raise ValueError('lifecycle changed external configuration')
        # Reinstall must converge even though the owned distribution source
        # and archives intentionally survive native cache removal.
        command(install_cli, env)
        report.update(schema='openubmc.codex-plugin.qualification.v1', codex=codex_version,
                      deterministic_archive=True, native_install=True, native_uninstall=True,
                      reinstall=True, external_state_preserved=True, mcp_health=doctor['mcp_health'])
        report['ok'] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--source-ref', default='HEAD')
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = qualify(args.source.resolve(), args.source_ref, args.archive.resolve())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report = {'ok':False, 'error':str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(report))
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
