#!/usr/bin/env python3
"""Measure a relocated plugin through its real CLI and newline MCP transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time


def receive(process, expected, deadline, pending):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout,selectors.EVENT_READ)
        while True:
            while b'\n' in pending[0]:
                line,pending[0]=pending[0].split(b'\n',1)
                message=json.loads(line)
                if message.get('id')==expected: return message
            remaining=deadline-time.monotonic()
            if remaining<=0 or not selector.select(remaining): raise TimeoutError('MCP response deadline')
            data=os.read(process.stdout.fileno(),65536)
            if not data: raise ValueError('MCP exited before its response')
            pending[0]+=data


def probe(plugin,command,env,root,window,timings_supported):
    timing=root/f'{window}-{command}.jsonl';argv=[sys.executable,'-I',str(plugin/'scripts/pluginctl.py'),command]
    if timings_supported: argv+=['--timings',str(timing)]
    snapshot_root=Path(env['XDG_CACHE_HOME'])/'openubmc/plugin-executions'
    snapshot_exists=any(path.is_dir() for path in snapshot_root.glob('*'))
    row={'command':command,'window':window,'snapshot_cache':'present' if snapshot_exists else 'absent',
         'failure_class':None,'started_at':time.time()}
    with (root/f'{window}-{command}.stderr').open('w') as stderr:
        started=time.monotonic()
        process=subprocess.Popen(argv,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,start_new_session=True)
        pending=[b''];deadline=started+30
        def send(message):
            process.stdin.write((json.dumps(message)+'\n').encode());process.stdin.flush()
        try:
            send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'measurement','version':'1'}}})
            initialized=receive(process,1,deadline,pending)
            row['process_to_initialize_seconds']=time.monotonic()-started
            if 'result' not in initialized: raise ValueError('initialize rejected')
            send({'jsonrpc':'2.0','method':'notifications/initialized'})
            send({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
            response=receive(process,2,deadline,pending)
            expected={'observe','execute'} if command=='runtime' else {'openubmc_kb_query','openubmc_kb_status','openubmc_kb_list'}
            if {item['name'] for item in response['result']['tools']}!=expected: raise ValueError('tool discovery mismatch')
            row['tools_list_bytes']=len(json.dumps(response,ensure_ascii=False).encode())
            # Invalid input exercises the first tool response without contacting a service.
            name='execute' if command=='runtime' else 'openubmc_kb_query'
            before=time.monotonic()
            send({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':name,'arguments':{}}})
            result=receive(process,3,deadline,pending)
            row['first_tool_roundtrip_seconds']=time.monotonic()-before
            row['process_to_first_tool_seconds']=time.monotonic()-started
            if 'error' not in result and result.get('result',{}).get('isError') is not True:
                raise ValueError('missing input was not rejected')
            process.stdin.close();process.wait(timeout=10)
            row['closed_cleanly']=process.returncode==0
            if not row['closed_cleanly']: raise ValueError('unclean MCP exit')
        except Exception as error:
            row.update(failure_class='plugin',error=type(error).__name__+': '+str(error))
        finally:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait();process.stdout.close()
            if not process.stdin.closed: process.stdin.close()
    row['phases']=[json.loads(line) for line in timing.read_text().splitlines()] if timing.exists() else []
    return row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--windows',type=int,default=3)
    parser.add_argument('--interval',type=float,default=5)
    args=parser.parse_args();plugin=args.plugin.resolve()
    if args.windows<1 or not 0<=args.interval<=60: parser.error('invalid windows or interval')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists(): parser.error('output already exists')
    lock=json.loads((plugin/'plugin-lock.json').read_text())
    report={'schema':'openubmc.plugin-measurement.v1','source_commit':lock['source_commit'],'content_digest':lock['content_digest'],
            'version':lock['version'],'cold_scope':'empty plugin dependency and snapshot caches; download cache may be shared',
            'records':[]}
    with tempfile.TemporaryDirectory(prefix='openubmc-plugin-measurement-') as temporary:
        root=Path(temporary)
        env=dict(os.environ,XDG_DATA_HOME=str(root/'data'),XDG_CACHE_HOME=str(root/'cache'),
                 OPENUBMC_MCP_FORMAL_RUN='0',OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(root/'state'),OPENUBMC_MCP_LIFECYCLE_DIR=str(root/'lifecycle'))
        started=time.monotonic()
        prepared=subprocess.run([sys.executable,'-I',str(plugin/'scripts/pluginctl.py'),'prepare'],env=env,capture_output=True,text=True,timeout=540)
        report['dependency_prepare_seconds']=time.monotonic()-started
        report['dependency_prepare_ok']=prepared.returncode==0
        report['prepare_stages']=[json.loads(line) for line in prepared.stderr.splitlines() if line.startswith('{')]
        if prepared.returncode:
            report['prepare_error']=prepared.stderr[-1500:]
        else:
            timing_supported='--timings' in (plugin/'scripts/pluginctl.py').read_text()
            for window in range(args.windows):
                if window: time.sleep(args.interval)
                for command in ('runtime','kb'):
                    report['records'].append(probe(plugin,command,env,root,window,timing_supported))
        report['ok']=report['dependency_prepare_ok'] and len(report['records'])==2*args.windows and all(row['failure_class'] is None for row in report['records'])
    report['report_sha256']=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
    with args.output.open('x') as stream: json.dump(report,stream,indent=2);stream.write('\n')
    print(json.dumps({'ok':report['ok'],'dependency_prepare_seconds':report['dependency_prepare_seconds'],'samples':len(report['records'])}))
    return int(not report['ok'])


if __name__=='__main__': raise SystemExit(main())
