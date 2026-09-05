#!/usr/bin/env python3
"""Install a verified OpenUBMC Codex plugin archive and migrate legacy state."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path


def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def extract_safe(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive) as bundle:
        members=bundle.getmembers()
        for item in members:
            path=Path(item.name)
            if not path.is_relative_to(Path('openubmc')) or path.is_absolute() or '..' in path.parts or item.issym() or item.islnk():
                raise ValueError('archive contains an unsafe member: '+item.name)
        bundle.extractall(destination, filter='data')
    return destination/'openubmc'


def run() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--codex-home', type=Path)
    args=parser.parse_args()
    actual=sha(args.archive)
    if actual != args.sha256.lower():
        raise ValueError(f'archive digest mismatch: expected {args.sha256.lower()}, got {actual}')
    with tempfile.TemporaryDirectory(prefix='openubmc-plugin-install-') as temporary:
        extracted=extract_safe(args.archive, Path(temporary))
        verify=subprocess.run([sys.executable,'-I',str(extracted/'scripts/pluginctl.py'),'verify'],capture_output=True,text=True,check=False)
        if verify.returncode: raise ValueError('plugin verification failed: '+(verify.stderr.strip() or verify.stdout.strip()))
        lock=json.loads((extracted/'plugin-lock.json').read_bytes())
        store=(args.home/'.local/share/openubmc/plugin-store').resolve()
        release=store/'releases'/(lock['version']+'-'+lock['content_digest'][:16])
        release.parent.mkdir(parents=True,exist_ok=True)
        if release.exists():
            existing=json.loads((release/'plugin-lock.json').read_bytes())
            if existing != lock: raise ValueError('release identity collision in plugin store')
        else:
            staging=release.with_name('.'+release.name+'.staging')
            if staging.exists(): shutil.rmtree(staging)
            shutil.copytree(extracted,staging)
            os.replace(staging,release)
        market=args.home/'.agents/plugins/marketplace.json'
        market.parent.mkdir(parents=True,exist_ok=True)
        marketplace=json.loads(market.read_text()) if market.is_file() else {'name':'personal','interface':{'displayName':'Personal'},'plugins':[]}
        if not isinstance(marketplace.get('name'),str) or not marketplace['name']:
            raise ValueError('personal marketplace has no valid name')
        entries=[entry for entry in marketplace.get('plugins',[]) if entry.get('name')!='openubmc']
        entries.append({'name':'openubmc','source':{'source':'local','path':'./plugins/openubmc'},'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Developer tools'})
        marketplace['plugins']=entries
        source=args.home/'plugins/openubmc'
        source.parent.mkdir(parents=True,exist_ok=True)
        stage=source.with_name('.openubmc-installing')
        if stage.exists(): shutil.rmtree(stage)
        shutil.copytree(release,stage)
        previous=source.with_name('.openubmc-previous')
        if previous.exists(): shutil.rmtree(previous)
        if source.exists(): source.rename(previous)
        stage.rename(source)
        market.write_text(json.dumps(marketplace,indent=2)+'\n')
        env=dict(os.environ,CODEX_HOME=str(args.codex_home or (args.home/'.codex')))
        (args.codex_home or (args.home/'.codex')).mkdir(parents=True,exist_ok=True)
        prepared=subprocess.run([sys.executable,'-I',str(release/'scripts/pluginctl.py'),'prepare'],env=env,capture_output=True,text=True,check=False)
        if prepared.returncode: raise ValueError('dependency preparation failed: '+prepared.stderr[-2000:])
        migration=subprocess.run([sys.executable,'-I',str(release/'scripts/pluginctl.py'),'migrate','--home',str(args.home)],env=env,capture_output=True,text=True,check=False)
        if migration.returncode: raise ValueError('legacy migration failed: '+(migration.stderr.strip() or migration.stdout.strip()))
        if args.home.resolve()!=Path.home().resolve():
            subprocess.run(['codex','plugin','marketplace','add',str(args.home)],env=env,check=True,capture_output=True,text=True)
        added=subprocess.run(['codex','plugin','add','openubmc@'+marketplace['name'],'--json'],env=env,check=False,capture_output=True,text=True)
        if added.returncode: raise ValueError('Codex plugin activation failed: '+(added.stderr.strip() or added.stdout.strip()))
        report={'schema':'openubmc.codex-plugin.install.v1','archive_sha256':actual,'source_commit':lock['source_commit'],'version':lock['version'],'content_digest':lock['content_digest'],'release_path':str(release),'migration':json.loads(migration.stdout) if migration.stdout.strip() else {}}
        audit=store/'install-audits'; audit.mkdir(exist_ok=True); (audit/(lock['content_digest'][0:16]+'.json')).write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
        print(json.dumps(report,sort_keys=True))
    return 0

if __name__=='__main__':
    try: raise SystemExit(run())
    except (OSError,ValueError,tarfile.TarError,subprocess.CalledProcessError) as error:
        print(json.dumps({'ok':False,'error':str(error)}),file=sys.stderr); raise SystemExit(2)
