#!/usr/bin/env python3
"""Run retained A/A model attempts against a target-free Runtime MCP server.

The loopback relay measures provider/network waiting separately from local MCP
execution. It never records authorization headers or changes a model response.
"""
import argparse
from collections import Counter
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from runtime_measurement import encoded, source_identity

PROMPT = '''Exercise the local openubmc-target-runtime fixture using execute only. Its Domain Adapter is deterministic and has no target connection. Start with exactly this execute argument object: {"kind":"start","intent":"diagnose-and-fix","delivery_strategy":"source-only","target":"198.51.100.10","purpose":"validate fixture state refresh"}. The field is intent, not workflow. Respond to diagnosis.acceptance with the evidence IDs in the returned diagnostic_receipt, root_cause "fixture state was outdated", causal_chain ["fixture state was outdated", "component consumed that state"], code_owner "fixture/component.lua", contradictions [], remaining_gaps [], verification_status "verified". Then respond to developer.change with source_revision "fixture-revision", authored_files ["fixture/component.lua"], verification_plan ["test fixture state refresh"]. Use each returned Gate binding exactly, and response status completed and summary "fixture phase complete". Do not observe, use shell, edit files, or contact a target. Stop only after Runtime reports state completed and outcome_recorded true, and report the terminal Run ID. Tool discovery through exec/ALL_TOOLS is allowed.'''


class Relay(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self, upstream, env_key):
        self.upstream=urlsplit(upstream)
        self.env_key=env_key
        self.records=[]
        super().__init__(('127.0.0.1',0),Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args): pass
    def do_POST(self):
        started=time.perf_counter()
        record={'started_at':time.time(), 'first_byte_seconds':None, 'duration_seconds':None, 'status':None,
                'requested_model':None,'response_model':None, 'usage':None}
        self.server.records.append(record)
        upstream=self.server.upstream
        connection=(http.client.HTTPSConnection if upstream.scheme=='https' else http.client.HTTPConnection)(upstream.hostname,upstream.port,timeout=180)
        try:
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            request=json.loads(body)
            record['requested_model']=request.get('model')
            path=upstream.path.rstrip('/')+'/responses'
            connection.request('POST',path,body,{'Content-Type':'application/json','Authorization':'Bearer '+os.environ[self.server.env_key], 'Accept':'text/event-stream'})
            response=connection.getresponse();record['status']=response.status
            self.send_response(response.status)
            self.send_header('Content-Type',response.getheader('Content-Type','text/event-stream'))
            self.send_header('Connection','close');self.end_headers();self.close_connection=True
            pending=b''
            while True:
                chunk=response.read1(65536)
                if not chunk: break
                if record['first_byte_seconds'] is None: record['first_byte_seconds']=time.perf_counter()-started
                self.wfile.write(chunk);self.wfile.flush()
                pending+=chunk
                lines=pending.split(b'\n');pending=lines.pop()
                for line in lines:
                    if not line.startswith(b'data:'): continue
                    try: event=json.loads(line[5:].strip())
                    except (ValueError,UnicodeDecodeError): continue
                    data=event.get('response',{})
                    if data.get('model'): record['response_model']=data['model']
                    if data.get('usage'):
                        record['usage']={key:value for key,value in data['usage'].items() if key!='attribution'}
        except Exception as error:
            record['error_class']=type(error).__name__
            self.close_connection=True
        finally:
            record['duration_seconds']=time.perf_counter()-started
            connection.close()


def attempt(args, output, relay, model, pair, arm, ordinal):
    root=output/f'{model}-{pair:02d}-{arm}';root.mkdir()
    home=root/'home';home.mkdir();codex_home=home/'.codex';codex_home.mkdir()
    trace=root/'mcp.jsonl';events=root/'events.jsonl'
    env=dict(os.environ, HOME=str(home),CODEX_HOME=str(codex_home),PYTHONDONTWRITEBYTECODE='1')
    config=['features.plugins=false','features.shell_tool=false','model_reasoning_effort="low"',
            'model_provider="measurement"','model_providers.measurement.name="Measurement relay"',
            f'model_providers.measurement.base_url="http://127.0.0.1:{relay.server_port}/v1"',
            f'model_providers.measurement.env_key="{args.env_key}"','model_providers.measurement.wire_api="responses"',
            'model_providers.measurement.supports_websockets=false','model_providers.measurement.request_max_retries=0',
            'model_providers.measurement.stream_max_retries=0',
            f'mcp_servers.openubmc-target-runtime.command={json.dumps(sys.executable)}',
            'mcp_servers.openubmc-target-runtime.args='+json.dumps([str(Path(__file__).with_name('runtime_measurement_server.py')),'--source',str(args.source),'--trace',str(trace)]),
            'mcp_servers.openubmc-target-runtime.required=true']
    argv=[args.codex,'exec','--ephemeral','--json','--skip-git-repo-check','--sandbox','danger-full-access','-C',str(root),'-m',model]
    for item in config: argv+=['-c',item]
    argv+=[PROMPT]
    begin=len(relay.records);started=time.perf_counter();failure=None
    with events.open('w') as stdout, (root/'stderr.log').open('w') as stderr:
        process=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
        try:
            code=process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            failure='transport';code=None
        finally:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
    elapsed=time.perf_counter()-started
    calls=[json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    tools=[row for row in calls if row['request']['method']=='tools/call']
    turns=[row['response'].get('result',{}).get('structuredContent',{}) for row in tools]
    provider=relay.records[begin:]
    successful=[turn for turn in turns if turn.get('state')!='failed']
    gates=[(turn.get('gate') or {}).get('name') for turn in successful]
    completed=bool(successful and successful[-1].get('state')=='completed' and successful[-1].get('outcome_recorded') is True)
    valid=code==0 and completed and 'diagnosis.acceptance' in gates and 'developer.change' in gates
    request_errors=[turn.get('error',{}).get('code') for turn in turns if turn.get('state')=='failed']
    if not valid and failure is None:
        failure=('model' if request_errors or not tools else 'runtime' if not completed else 'transport')
    clean_flow=valid and len(turns)==3 and not request_errors
    row={'model':model,'pair':pair,'arm':arm,'ordinal':ordinal,'exit_code':code,'valid':bool(valid),'failure_class':failure,
         'duration_seconds':elapsed,'runtime_seconds':sum(row['runtime_wall_seconds'] for row in tools),
         'tool_output_bytes':sum(len(encoded(row['response'].get('result',{}))) for row in tools),
         'provider_requests':provider,'provider_seconds':sum(row.get('duration_seconds') or 0 for row in provider),
         'completed':completed,'clean_flow':bool(clean_flow),'model_request_errors':request_errors,'mcp_calls':len(tools),'events_sha256':hashlib.sha256(events.read_bytes()).hexdigest(),
         'trace_sha256':hashlib.sha256(trace.read_bytes()).hexdigest() if trace.exists() else None}
    (root/'result.json').write_text(json.dumps(row,indent=2)+'\n')
    return row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--models',nargs='+',default=['gpt-5.6-sol','gpt-5.6-luna'])
    parser.add_argument('--pairs',type=int,default=10)
    parser.add_argument('--codex',default='codex')
    parser.add_argument('--upstream',required=True)
    parser.add_argument('--env-key',default='CLI_PROXY_API_KEY')
    parser.add_argument('--timeout',type=float,default=240)
    args=parser.parse_args();args.source=args.source.resolve();args.output=args.output.resolve()
    if not os.environ.get(args.env_key): parser.error('configured provider credential is unavailable')
    if args.pairs<1: parser.error('pairs must be positive')
    args.output.mkdir(parents=True,exist_ok=False)
    schedule=[];rng=random.Random(20260906)
    for model in args.models:
        for pair in range(1,args.pairs+1):
            arms=['A','B'];rng.shuffle(arms)
            schedule.extend((model,pair,arm) for arm in arms)
    identity={'source':source_identity(args.source),'prompt_sha256':hashlib.sha256(PROMPT.encode()).hexdigest(),
              'codex':subprocess.check_output([args.codex,'--version'],text=True).strip(),
              'reasoning_effort':'low','schedule':schedule,'cache':'isolated client homes; provider cache uncontrolled; usage retained',
              'wait_scope':'provider round-trip includes network, queue, model and streaming; excludes Runtime calls',
              'reruns':0,'status':'running'}
    (args.output/'condition.json').write_text(json.dumps(identity,indent=2)+'\n')
    relay=Relay(args.upstream,args.env_key);thread=threading.Thread(target=relay.serve_forever,daemon=True);thread.start()
    rows=[]
    try:
        for ordinal,(model,pair,arm) in enumerate(schedule):
            row=attempt(args,args.output,relay,model,pair,arm,ordinal);rows.append(row)
            (args.output/'attempts.json').write_text(json.dumps(rows,indent=2)+'\n')
            print(json.dumps({key:row[key] for key in ['model','pair','arm','valid','failure_class','duration_seconds','mcp_calls']}),flush=True)
    finally:
        relay.shutdown();relay.server_close();thread.join(timeout=5)
        identity.update(status='complete' if len(rows)==len(schedule) else 'interrupted',attempted=len(rows),valid=sum(row['valid'] for row in rows),
                        failure_counts=dict(Counter(row['failure_class'] for row in rows if row['failure_class'])))
        (args.output/'condition.json').write_text(json.dumps(identity,indent=2)+'\n')
    return int(identity['valid']!=len(schedule))


if __name__=='__main__': raise SystemExit(main())
