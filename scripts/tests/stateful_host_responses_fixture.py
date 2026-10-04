"""Run only loopback scripted Responses against the native, fake-Runtime Host adapter."""
from pathlib import Path
import argparse
import http.server
import json
import os
import subprocess
import shutil
import sys
import tempfile
import threading
import time

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--codex-bin', default=shutil.which('codex'))
args=parser.parse_args()
output=args.output.resolve()
if output.exists():
    parser.error('output already exists; preserve prior evidence')
output.parent.mkdir(parents=True, exist_ok=True)
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts import stateful_agent_evaluation as evaluation

base=Path(tempfile.mkdtemp(prefix='host-resume-controlled-'))
manifest=evaluation.load_manifest()
source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
plan=evaluation.build_plan(manifest,model='gpt-6-sol',client_version='codex-cli/0.153.4',
                          source_commit=source,reasoning_effort='low')
case=next(c for c in manifest['scenarios'] if c['id']=='diagnosis-resume')
slot=next(s for s in plan['schedule'] if s['scenario_id']==case['id'] and s['trial']==1)
directory=base/slot['task_id'];directory.mkdir()
request={'schema':evaluation.SCHEMA+'/adapter-request','scenario_id':case['id'],
 'scenario_version':1,'trial':1,'task_id':slot['task_id'],'prompt':case['prompt'],
 'prompt_digest':slot['prompt_digest'],'fixture_target':manifest['fixture_target'],
 'backend':'runtime-fake','output_directory':str(directory),'source_commit':source,
 'plan_digest':plan['plan_digest'],'schedule_digest':plan['schedule_digest'],
 'client_version':plan['client_version'],'model':plan['model'],'reasoning_effort':plan['reasoning_effort'],
 'execution_mode':'controlled-scripted-responses','model_invoked':False}
(directory/'request.json').write_text(json.dumps(request,indent=2))
(base/'plan.json').write_text(json.dumps(plan,indent=2))

def function(name,arguments):
    return {'type':'function_call','id':'fc-'+name,'call_id':'call-'+name,
            'name':'execute','namespace':'mcp__runtime_fake','arguments':json.dumps(arguments)}

def last_turn(doc):
    outputs=[x for x in doc.get('input',[]) if x.get('type')=='function_call_output']
    raw=outputs[-1]['output']
    return json.loads(raw.partition('\nOutput:\n')[2] if '\nOutput:\n' in raw else raw)

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        doc=json.loads(self.rfile.read(int(self.headers['content-length'])))
        with self.server.lock:
            n=self.server.count
            self.server.count+=1
        (directory/('scripted-request-'+str(n)+'.json')).write_text(json.dumps(doc))
        try:
            messages=[(i,x) for i,x in enumerate(doc.get('input',[]))
                      if x.get('type')=='message' and x.get('role')=='user']
            user_index,user=messages[-1]
            prompt=''.join(x.get('text','') for x in user['content'])
            calls=[x for x in doc['input'][user_index+1:] if x.get('type')=='function_call']
            marker='Reply with exactly this single line and no other text: '
            if marker in prompt:
                text=prompt.split(marker,1)[1]
                item={'type':'message','id':'message-'+str(n),'role':'assistant','phase':'final_answer',
                      'status':'completed','content':[{'type':'output_text','text':text,'annotations':[]}]}
            elif prompt.startswith('Continue the interrupted diagnosis.'):
                turn=last_turn(doc)
                if not calls:
                    item=function('resume',{'kind':'resume','run_id':turn['run_id']})
                elif json.loads(calls[-1]['arguments'])['kind']=='resume':
                    gate=turn['gate']
                    item=function('respond',{'kind':'respond','run_id':turn['run_id'],
                        'gate_id':gate['gate_id'],'gate_version':gate['gate_version'],
                        'schema_digest':gate['schema_digest'],'submission_id':gate['submission_id'],
                        'response':{'status':'completed','summary':'synthetic diagnosis','payload':{
                            'root_cause':'synthetic mismatch','evidence_ids':[x['evidence_id'] for x in turn['diagnostic_receipt']['evidence']],
                            'causal_chain':['synthetic evidence supports cause'],'code_owner':'src/fake.lua',
                            'contradictions':[],'remaining_gaps':[],'verification_status':'verified'}}})
                elif json.loads(calls[-1]['arguments'])['kind']=='respond' and turn['outcome']['status']=='completed':
                    item={'type':'message','id':'message-'+str(n),'role':'assistant','phase':'final_answer',
                          'status':'completed','content':[{'type':'output_text','text':'Recovered synthetic diagnosis.','annotations':[]}]}
                else:
                    raise ValueError('unexpected recovery sequence')
            elif prompt.startswith('Start one isolated synthetic diagnosis-only Run'):
                if not calls:
                    item=function('start',{'kind':'start','intent':'diagnosis-only','target':'192.0.2.10'})
                else:
                    checkpoint=last_turn(doc)
                    if checkpoint['state']!='waiting_response' or checkpoint['outcome'] is not None:
                        raise ValueError('first result was not a nonterminal diagnosis')
                    self.server.stop.wait(60)
                    return
            else:
                raise ValueError('unexpected fixture prompt')
            rows=[{'type':'response.created','response':{'id':'resp-'+str(n),'status':'in_progress','output':[]}},
                  {'type':'response.output_item.added','output_index':0,'item':item},
                  {'type':'response.output_item.done','output_index':0,'item':item},
                  {'type':'response.completed','response':{'id':'resp-'+str(n),'status':'completed','output':[item],
                    'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}]
            raw=''.join('event: '+r['type']+'\ndata: '+json.dumps(r)+'\n\n' for r in rows).encode()
            self.send_response(200);self.send_header('content-type','text/event-stream')
            self.send_header('content-length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except Exception as exc:
            self.server.errors.append(type(exc).__name__+': '+str(exc))
            self.send_error(500)

server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
server.daemon_threads=True
server.lock=threading.Lock();server.count=0;server.errors=[];server.stop=threading.Event()
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','USER','LOGNAME','TMPDIR'}}
env.update(OPENUBMC_EVAL_BASE_URL='http://127.0.0.1:'+str(server.server_port)+'/v1',
           OPENUBMC_EVAL_CODEX_BIN=str(args.codex_bin or ''),PYTHONDONTWRITEBYTECODE='1')
started=time.monotonic()
try:
    with (directory/'adapter.log').open('w') as stream:
        completed=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/stateful_agent_host_adapter.py'),str(directory/'request.json')],
                                 env=env,cwd=ROOT,stdout=stream,stderr=stream,timeout=700)
    code=completed.returncode
finally:
    server.stop.set();server.shutdown();server.server_close();thread.join(2)
elapsed=round(time.monotonic()-started,3)
(directory/'timing.json').write_text(json.dumps({'schema':evaluation.SCHEMA+'/timing',
                                              'adapter_exit_code':code,'elapsed_seconds':elapsed}))
result={'directory':str(directory),'repository':str(ROOT),'source_commit':source,'adapter_exit_code':code,
        'execution_mode':'controlled-scripted-responses','model_invoked':False,'actual_agent_trials':0,
        'live_acceptance':'unverified','requests':server.count,'fixture_errors':server.errors,
        'elapsed_seconds':elapsed}
if code==0:
    trial=json.loads((directory/'trial.json').read_text())
    result['score']=evaluation.score_live_trial(case=case,plan=plan,manifest=manifest,trial=trial,
        runtime_db=directory/'runtime.sqlite',terminal_store=directory/'terminal.json',
        rollout=directory/'rollout.jsonl',elapsed_seconds=elapsed)
    result['aggregate']=evaluation.summarize_live(manifest=manifest,plan=plan,trial_root=base)
else:
    result['pilot_status']=json.loads((directory/'pilot-status.json').read_text())
output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='aggregate'},indent=2))
if 'aggregate' in result:print(json.dumps({k:result['aggregate'][k] for k in
    ('actual_agent_trials','controlled_trials_excluded','live_acceptance')}))

raise SystemExit(0 if code == 0 and not server.errors and not result['score']['issues'] else 1)
