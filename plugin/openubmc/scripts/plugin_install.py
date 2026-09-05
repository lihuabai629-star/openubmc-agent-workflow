"""Reversible migration from the legacy loose Runtime installation."""
from __future__ import annotations
import json, os, re, shutil, tempfile, uuid
from pathlib import Path


def _state(home: Path) -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME') or home/'.config')/'openubmc/environment-state.json'


def _owned(state: dict) -> tuple[set[str], set[str]]:
    paths={str(k) for k in state.get('links',{})}
    commands=set()
    for value in (state.get('runtime_mcp'), state.get('mcp')):
        if isinstance(value,dict):
            values=value.values() if not ('command' in value) else [value]
            for entry in values:
                if isinstance(entry,dict) and entry.get('command'):
                    commands.add(str(entry['command']))
    for value in state.get('mcp',{}).values() if isinstance(state.get('mcp'),dict) else ():
        if isinstance(value,dict) and value.get('command'): commands.add(str(value['command']))
    return paths, commands


def _strip_owned_toml(text: str, commands: set[str], targets: set[str]) -> str:
    lines=text.splitlines(keepends=True)
    blocks=[]; current=[]
    for line in lines:
        if line.startswith('[') and current:
            blocks.append(current); current=[]
        current.append(line)
    if current: blocks.append(current)
    output=[]
    for block in blocks:
        header=block[0].strip() if block else ''
        if header.startswith('[mcp_servers.') and any(
            re.search(r'^command\s*=\s*["\']'+re.escape(command)+r'["\']\s*$', ''.join(block), re.M)
            for command in commands
        ):
            continue
        if header == '[[skills.config]]':
            body=''.join(block)
            if any(re.search(r'^path\s*=\s*["\']'+re.escape(path)+r'["\']\s*$', body, re.M) for path in targets):
                continue
        output.extend(block)
    return ''.join(output)


def migrate(home: Path) -> dict:
    state_path=_state(home)
    state=json.loads(state_path.read_text()) if state_path.is_file() else {}
    links,commands=_owned(state)
    config=home/'.codex/config.toml'
    transaction=uuid.uuid4().hex
    root=Path(os.environ.get('XDG_DATA_HOME') or home/'.local/share')/'openubmc/migrations'/transaction
    root.mkdir(parents=True, mode=0o700)
    record={'schema':'openubmc.plugin-migration.v1','transaction':transaction,'home':str(home),'config':str(config),'links':[], 'config_existed':config.is_file()}
    if config.is_file():
        before=config.read_bytes(); (root/'config.toml').write_bytes(before)
        targets=set(links)
        after=_strip_owned_toml(before.decode('utf-8'),commands,targets).encode('utf-8')
        if after!=before:
            temporary=config.with_name(config.name+'.plugin-migration.tmp')
            temporary.write_bytes(after); os.replace(temporary,config)
    for link in sorted(links):
        path=Path(link)
        if path.is_symlink() and str(path.resolve()) == str(Path(state['links'][link]).resolve()):
            record['links'].append({'path':link,'target':state['links'][link]})
            path.unlink()
    (root/'transaction.json').write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    return {'ok':True,'transaction':transaction,'removed_links':len(record['links']),'config_changed':config.is_file() and (root/'config.toml').read_bytes()!=config.read_bytes()}


def restore(home: Path, transaction: str) -> dict:
    root=Path(os.environ.get('XDG_DATA_HOME') or home/'.local/share')/'openubmc/migrations'/transaction
    record=json.loads((root/'transaction.json').read_text())
    config=Path(record['config'])
    if (root/'config.toml').is_file():
        temporary=config.with_name(config.name+'.plugin-restore.tmp'); temporary.parent.mkdir(parents=True,exist_ok=True)
        temporary.write_bytes((root/'config.toml').read_bytes()); os.replace(temporary,config)
    for item in record['links']:
        path=Path(item['path']); path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() or path.is_symlink():
            raise ValueError(f'restore would overwrite changed path: {path}')
        path.symlink_to(item['target'])
    return {'ok':True,'transaction':transaction,'restored_links':len(record['links'])}
