#!/usr/bin/env python3
"""Audit, rollback, or remove an OpenUBMC Codex plugin without touching state."""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path

def env(home, codex_home):
 e=dict(os.environ,CODEX_HOME=str(codex_home or home/'.codex')); (codex_home or home/'.codex').mkdir(parents=True,exist_ok=True); return e

def store(home): return home/'.local/share/openubmc/plugin-store'
def main():
 p=argparse.ArgumentParser(); p.add_argument('command',choices=['audit','rollback','uninstall']); p.add_argument('--home',type=Path,default=Path.home()); p.add_argument('--codex-home',type=Path); p.add_argument('--release')
 a=p.parse_args(); root=store(a.home); e=env(a.home,a.codex_home)
 if a.command=='audit':
  audits=root/'install-audits'; reports=[]
  for path in sorted(audits.glob('*.json')) if audits.is_dir() else []: reports.append(json.loads(path.read_text()))
  print(json.dumps({'schema':'openubmc.codex-plugin.audit.v1','releases':reports},sort_keys=True)); return 0
 if a.command=='rollback':
  if not a.release: raise ValueError('--release is required')
  release=root/'releases'/a.release
  if not (release/'plugin-lock.json').is_file(): raise ValueError('unknown immutable release')
  destination=a.home/'plugins/openubmc'; stage=destination.with_name('.openubmc-rollback');
  if stage.exists(): shutil.rmtree(stage)
  shutil.copytree(release,stage); previous=destination.with_name('.openubmc-previous')
  if previous.exists(): shutil.rmtree(previous)
  if destination.exists(): destination.rename(previous)
  stage.rename(destination)
  subprocess.run(['codex','plugin','add','openubmc@'+json.loads((a.home/'.agents/plugins/marketplace.json').read_text())['name'],'--json'],env=e,check=True)
  print(json.dumps({'ok':True,'release':a.release})); return 0
 subprocess.run(['codex','plugin','remove','openubmc@'+json.loads((a.home/'.agents/plugins/marketplace.json').read_text())['name']],env=e,check=True)
 destination=a.home/'plugins/openubmc'
 if destination.is_dir(): shutil.rmtree(destination)
 print(json.dumps({'ok':True,'preserved_state':True})); return 0
if __name__=='__main__':
 try: raise SystemExit(main())
 except (OSError,ValueError,subprocess.CalledProcessError,json.JSONDecodeError) as error: print(json.dumps({'ok':False,'error':str(error)}),file=sys.stderr); raise SystemExit(2)
