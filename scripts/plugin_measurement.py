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



class MeasurementCancelled(Exception):
    pass


def cancelled(_signum, _frame):
    raise MeasurementCancelled('plugin measurement cancelled')


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


def probe(plugin,command,env,root,window,timings_supported,records):
    env=dict(env, XDG_CACHE_HOME=str(root/f'cache-{command}'))
    timing=root/f'{window}-{command}.jsonl';argv=[sys.executable,'-I',str(plugin/'scripts/pluginctl.py'),command]
    if timings_supported: argv+=['--timings',str(timing)]
    snapshot_root=Path(env['XDG_CACHE_HOME'])/'openubmc/plugin-executions'
    snapshot_exists=any(path.is_dir() for path in snapshot_root.glob('*'))
    row={'command':command,'window':window,'snapshot_cache':'present' if snapshot_exists else 'absent',
         'failure_class':None,'started_at':time.time()}
    records.append(row)
    process=None
    with (root/f'{window}-{command}.stderr').open('w') as stderr:
        started=time.monotonic()
        pending=[b''];deadline=started+30
        def send(message):
            process.stdin.write((json.dumps(message)+'\n').encode());process.stdin.flush()
        try:
            process=subprocess.Popen(argv,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,start_new_session=True)
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
        except (MeasurementCancelled, KeyboardInterrupt):
            row.update(failure_class='cancelled',error='measurement cancelled')
            raise
        except Exception as error:
            row.update(failure_class='plugin',error=type(error).__name__+': '+str(error))
        finally:
            if process is not None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait();process.stdout.close()
                if not process.stdin.closed: process.stdin.close()
            row['elapsed_seconds']=time.monotonic()-started
            row['phases']=[json.loads(line) for line in timing.read_text().splitlines()] if timing.exists() else []
    return row


def owned_processes(pid):
    result=[]
    children=Path(f'/proc/{pid}/task/{pid}/children')
    try: child_ids=[int(value) for value in children.read_text().split()]
    except FileNotFoundError: child_ids=[]
    for child in child_ids:
        result.extend(owned_processes(child))
    result.append(pid)
    return result


def prepare(plugin, env, report):
    process=None
    started=time.monotonic()
    try:
        process=subprocess.Popen([sys.executable,'-I',str(plugin/'scripts/pluginctl.py'),'prepare'],env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        _, stderr=process.communicate(timeout=540)
        report['dependency_prepare_ok']=process.returncode==0
        report['prepare_stages']=[json.loads(line) for line in stderr.splitlines() if line.startswith('{')]
        if process.returncode:
            report['prepare_error']=stderr[-1500:]
            report.update(failure_class='dependency',failure_stage='prepare')
    finally:
        if process is not None and process.poll() is None:
            # Installers can own independent sessions. Stop descendants before
            # their parent, including workers from older plugin versions.
            for pid in owned_processes(process.pid):
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
            _, stderr=process.communicate()
            report['prepare_error']=stderr[-1500:]
        report['dependency_prepare_seconds']=time.monotonic()-started


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
    report={'schema':'openubmc.plugin-measurement.v1','started_at':time.time(),
            'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'python':sys.version,'dependency_prepare_ok':False,'dependency_prepare_seconds':None,
            'cold_scope':'empty plugin dependency and snapshot caches; download cache may be shared',
            'records':[], 'failure_class':None, 'failure_stage':None, 'ok':False}
    old_handlers=(signal.getsignal(signal.SIGTERM),signal.getsignal(signal.SIGINT))
    signal.signal(signal.SIGTERM,cancelled);signal.signal(signal.SIGINT,cancelled)
    stage='preflight'
    try:
        lock=json.loads((plugin/'plugin-lock.json').read_text())
        report.update({key:lock[key] for key in ('source_commit','content_digest','version')})
        report['node']=subprocess.check_output(['node','--version'],text=True,timeout=10).strip()
        with tempfile.TemporaryDirectory(prefix='openubmc-plugin-measurement-') as temporary:
            root=Path(temporary)
            env=dict(os.environ,XDG_DATA_HOME=str(root/'data'),XDG_CACHE_HOME=str(root/'cache'),
                     OPENUBMC_MCP_FORMAL_RUN='0',OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(root/'state'),OPENUBMC_MCP_LIFECYCLE_DIR=str(root/'lifecycle'))
            stage='prepare'
            prepare(plugin,env,report)
            if report['dependency_prepare_ok']:
                timing_supported='--timings' in (plugin/'scripts/pluginctl.py').read_text()
                for window in range(args.windows):
                    stage='window_interval'
                    if window:time.sleep(args.interval)
                    for command in ('runtime','kb'):
                        stage='probe'
                        probe(plugin,command,env,root,window,timing_supported,report['records'])
            report['ok']=report['dependency_prepare_ok'] and len(report['records'])==2*args.windows and all(row['failure_class'] is None for row in report['records'])
    except (MeasurementCancelled,KeyboardInterrupt):
        report.update(ok=False,failure_class='cancelled',failure_stage=stage)
    except Exception as error:
        report.update(ok=False,failure_class='harness',failure_stage=stage,error=type(error).__name__+': '+str(error))
    finally:
        report['finished_at']=time.time()
        report['report_sha256']=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
        with args.output.open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')
        signal.signal(signal.SIGTERM,old_handlers[0]);signal.signal(signal.SIGINT,old_handlers[1])
    print(json.dumps({'ok':report['ok'],'dependency_prepare_seconds':report['dependency_prepare_seconds'],'samples':len(report['records'])}))
    return int(not report['ok'])


if __name__=='__main__': raise SystemExit(main())
