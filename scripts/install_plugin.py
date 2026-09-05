#!/usr/bin/env python3
"""Install an immutable OpenUBMC Codex plugin with a recoverable activation journal."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin_archive import canonical, materialize, read_archive, verify_directory


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def command(argv: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=240)
    if result.returncode:
        raise ValueError('command failed: '+str(argv[:3])+': '+result.stderr[-2000:])
    return result.stdout


def compensate(journal: Path, record: dict) -> None:
    source, market, config = (Path(record[key]) for key in ('source', 'market', 'config'))
    if record.get('source_replaced') and source.exists():
        lock = verify_directory(source)
        if lock['content_digest'] != record['content_digest']:
            raise ValueError('cannot compensate a changed plugin source')
        source.rename(journal/'failed-source')
    if (journal/'previous-source').exists():
        (journal/'previous-source').rename(source)
    for key, path in (('market', market), ('config', config)):
        if record.get(key+'_written'):
            current = path.read_bytes() if path.is_file() else b''
            if hashlib.sha256(current).hexdigest() != record.get(key+'_after'):
                raise ValueError('cannot compensate subsequent '+key+' changes; inspect '+str(journal))
            backup = journal/(key+'-before')
            write(path, backup.read_bytes())
    for item in record.get('legacy_links', []):
        path = Path(item['path'])
        if path.exists() or path.is_symlink():
            if not path.is_symlink() or os.readlink(path) != item['target']:
                raise ValueError('cannot compensate a changed legacy Skill path')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(item['target'])
    record['status'] = 'rolled_back'
    write(journal/'transaction.json', canonical(record))


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--codex-home', type=Path)
    args = parser.parse_args()
    lock, files = read_archive(args.archive, args.sha256.lower())
    home = args.home.resolve()
    codex = (args.codex_home or home/'.codex').resolve()
    env = dict(os.environ, CODEX_HOME=str(codex), XDG_CONFIG_HOME=str(home/'.config'),
               XDG_DATA_HOME=str(home/'.local/share'), XDG_CACHE_HOME=str(home/'.cache'))
    store = home/'.local/share/openubmc/plugin-store'
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store/'activation.lock').open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        for path in (store/'transactions').glob('*/transaction.json'):
            pending = json.loads(path.read_bytes())
            if pending.get('status') not in {'committed', 'rolled_back'}:
                compensate(path.parent, pending)
        release = store/'releases'/(lock['version']+'-'+lock['content_digest'][:16])
        if release.exists():
            if verify_directory(release) != lock:
                raise ValueError('existing immutable release has drifted')
        else:
            materialize(release, files)
        # Preparation and real MCP startup precede all activation changes.
        cli = [sys.executable, '-I', str(release/'scripts/pluginctl.py')]
        for operation in ('prepare', 'doctor'):
            command([*cli, operation], env)
        source = home/'plugins/openubmc'
        market = home/'.agents/plugins/marketplace.json'
        config = codex/'config.toml'
        if source.is_symlink() or market.is_symlink() or config.is_symlink():
            raise ValueError('activation paths must not be symbolic links')
        old = verify_directory(source) if source.exists() else None
        if old:
            owner = store/'install-audits'/(old['content_digest'][:16]+'.json')
            if not owner.is_file() or json.loads(owner.read_bytes()).get('content_digest') != old['content_digest']:
                raise ValueError('existing plugin source has no matching ownership audit')
        if old and old['version'] == lock['version'] and old['content_digest'] != lock['content_digest']:
            raise ValueError('same-version plugin replacement requires a new qualified version')
        market_before = market.read_bytes() if market.is_file() else b''
        marketplace = json.loads(market_before) if market_before else {'name':'personal','interface':{'displayName':'Personal'},'plugins':[]}
        if not re.fullmatch(r'[A-Za-z0-9_-]+', marketplace.get('name', '')):
            raise ValueError('invalid personal marketplace name')
        for entry in marketplace.get('plugins', []):
            if entry.get('name') == 'openubmc' and entry.get('source') != {'source':'local','path':'./plugins/openubmc'}:
                raise ValueError('existing OpenUBMC marketplace entry has another owner')
        entries = list(marketplace.get('plugins', []))
        if not any(item.get('name') == 'openubmc' for item in entries):
            entries.append({'name':'openubmc','source':{'source':'local','path':'./plugins/openubmc'},'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'})
        marketplace['plugins'] = entries
        journal = store/'transactions'/uuid.uuid4().hex
        journal.mkdir(parents=True, mode=0o700)
        write(journal/'market-before', market_before)
        write(journal/'config-before', config.read_bytes() if config.is_file() else b'')
        record = {'schema':'openubmc.plugin-activation.v1','status':'prepared','source':str(source),'market':str(market),'config':str(config),
                  'source_commit':lock['source_commit'],'version':lock['version'],'content_digest':lock['content_digest'],
                  'archive_sha256':args.sha256.lower(),'release_path':str(release)}
        write(journal/'transaction.json', canonical(record))
        try:
            # Journal legacy links before migration so a failed activation can
            # restore them without trusting a later mutable source directory.
            import types
            migration_module = types.ModuleType('verified_plugin_migration')
            exec(compile(files['scripts/plugin_install.py'], '<verified-migration>', 'exec'), migration_module.__dict__)
            paths = [item['path'] for item in json.loads(files['workflow.json'])['skills']]
            plan, _, _ = migration_module.plan(home, paths, codex)
            record['legacy_links'] = plan['links']
            write(journal/'transaction.json', canonical(record))
            record['migration'] = migration_module.migrate(home, paths, codex)
            record['config_written'] = True
            record['config_after'] = hashlib.sha256(config.read_bytes() if config.is_file() else b'').hexdigest()
            write(journal/'transaction.json', canonical(record))
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                source.rename(journal/'previous-source')
            materialize(source, files)
            record['source_replaced'] = True
            write(journal/'transaction.json', canonical(record))
            write(market, canonical(marketplace))
            record['market_written'] = True
            record['market_after'] = hashlib.sha256(market.read_bytes()).hexdigest()
            write(journal/'transaction.json', canonical(record))
            codex.mkdir(parents=True, exist_ok=True)
            if home != Path.home().resolve():
                command(['codex','plugin','marketplace','add',str(home)], env)
            output = command(['codex','plugin','add','openubmc@'+marketplace['name'],'--json'], env)
            record['config_after'] = hashlib.sha256(config.read_bytes()).hexdigest()
            write(journal/'transaction.json', canonical(record))
            installed = json.loads(output)
            if verify_directory(Path(installed['installedPath'])) != lock:
                raise ValueError('Codex installed content differs from the selected release')
            record['codex_install'] = installed
            record['status'] = 'committed'
            write(journal/'transaction.json', canonical(record))
            write(store/'archives'/(args.sha256.lower()+'.tar.gz'), args.archive.read_bytes())
            write(store/'install-audits'/(lock['content_digest'][:16]+'.json'), canonical(record))
        except BaseException:
            # Capture output of our last Codex config write for compare-before-
            # restore; the journal remains available if compensation fails.
            if record.get('config_written') and config.is_file():
                record['config_after'] = hashlib.sha256(config.read_bytes()).hexdigest()
            compensate(journal, record)
            raise
        print(json.dumps({**record, 'ok':True}, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':str(error)}), file=sys.stderr)
        raise SystemExit(2)
