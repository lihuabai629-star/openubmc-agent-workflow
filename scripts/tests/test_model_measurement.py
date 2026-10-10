"""Failure taxonomy uses observed provider and MCP facts, not tool counts alone."""
import sys
from pathlib import Path
import unittest
import tempfile
import json
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime_model_measurement import classify_failure, attempt
from runtime_model_measurement import Relay
import http.client
import http.server
import os
import threading
from unittest.mock import patch


class ModelMeasurementTests(unittest.TestCase):
    def test_provider_errors_are_transport_failures_even_after_a_gate(self):
        self.assertEqual(classify_failure(False,[{'status':500}],[],True),('transport','provider_http_500'))
        self.assertEqual(classify_failure(False,[{'status':200,'error_class':'TimeoutError'}],[],False),('transport','provider_TimeoutError'))
        self.assertEqual(classify_failure(False,[{'status':200,'stream_completed':False}],[],True),('transport','incomplete_provider_stream'))

    def test_model_invalid_input_and_runtime_invariant_are_distinct(self):
        self.assertEqual(classify_failure(False,[{'status':200}],['AgentPreflightError'],True),('model','invalid_request'))
        self.assertEqual(classify_failure(False,[{'status':200}],['GatePreflightError'],True),('model','invalid_request'))
        self.assertEqual(classify_failure(False,[{'status':200}],['RuntimeError'],True),('runtime','RuntimeError'))
        self.assertEqual(classify_failure(False,[{'status':200}],['GatePreflightError','RuntimeError'],True),('runtime','RuntimeError'))
        self.assertEqual(classify_failure(False,[{'status':200}],[],False),('plugin','mcp_not_initialized'))
        self.assertEqual(classify_failure(True,[{'status':200}],['AgentPreflightError'],True),(None,None))

    def test_failed_launch_remains_an_attempt_with_a_durable_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            args=SimpleNamespace(source=root,codex=str(root/'missing'),env_key='FIXTURE_KEY',timeout=1)
            row=attempt(args,root,SimpleNamespace(records=[],server_port=1),'fixture',1,'A',0)
            self.assertEqual(row['failure_class'],'harness')
            self.assertFalse(row['valid'])
            self.assertEqual(len(list(root.glob('*/started.json'))),1)
            self.assertEqual(json.loads((root/'fixture-01-A/result.json').read_text())['failure_class'],'harness')

    def test_producer_persists_physical_retries_before_forwarding_and_survives_reader_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'provider.json'
            persisted_before_forward=[]
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_POST(self):
                    self.rfile.read(int(self.headers.get('Content-Length','0')))
                    persisted_before_forward.append(json.loads(path.read_text()))
                    event={'type':'response.completed','response':{'id':'same-provider-response-id','model':'fixture',
                        'usage':{'input_tokens':10,'output_tokens':2,'input_tokens_details':{'cached_tokens':3},
                                 'unexpected_prompt':'SYNTHETIC-PRIVATE-PROMPT'}}}
                    body=('data: '+json.dumps(event)+'\n\n').encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            relay=Relay('http://127.0.0.1:'+str(upstream.server_port)+'/v1','SYNTHETIC_PROVIDER_KEY')
            threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (upstream,relay)]
            for thread in threads: thread.start()
            try:
                relay.begin_attempt(path,'task',evidence_kind='synthetic')
                with patch.dict(os.environ,{'SYNTHETIC_PROVIDER_KEY':'synthetic-local-only'}):
                    for _ in range(2):
                        connection=http.client.HTTPConnection('127.0.0.1',relay.server_port,timeout=5)
                        connection.request('POST','/tasks/task/v1/responses',json.dumps({'model':'fixture','input':'SYNTHETIC-PRIVATE-PROMPT'}))
                        response=connection.getresponse();self.assertEqual(response.status,200);response.read();connection.close()
                # server_close joins the owned request threads before declaring inventory complete.
                relay.shutdown();relay.server_close()
                relay.close_attempt('task')
                report=json.loads(path.read_text())
                self.assertTrue(report['inventory_complete'])
                self.assertEqual(len({row['invocation_ref'] for row in report['provider_requests']}),2)
                self.assertTrue(all(row['task_ref']=='task' and 'run_ref' not in row for row in report['provider_requests']))
                self.assertEqual(len(persisted_before_forward),2)
                self.assertFalse(any(row['inventory_complete'] for row in persisted_before_forward))
                self.assertNotIn('SYNTHETIC-PRIVATE-PROMPT',path.read_text())
                self.assertNotIn('synthetic-local-only',path.read_text())
                runtime=Path(__file__).resolve().parents[2]/'openubmc-target-runtime'
                sys.path.insert(0,str(runtime))
                from openubmc_target_runtime.measurements import ProviderReportReader,MeasurementSnapshot
                reader=ProviderReportReader(path,task_id='task',provider_ref=relay.provider_ref,evidence_kind='synthetic',producer_inventory=True)
                usage=MeasurementSnapshot(reader('task',('run',)),task_id='task',run_refs=('run',)).project()['usage']
                self.assertEqual((usage['invocation_count'],usage['input_tokens'],usage['cached_tokens']),(2,20,6))
            finally:
                for server in (relay,upstream): server.shutdown();server.server_close()
                for thread in threads: thread.join(timeout=5)

    def test_crashed_producer_leaves_incomplete_inventory_and_closed_scope_rejects_late_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'provider.json'
            relay=Relay('http://127.0.0.1:1/v1','SYNTHETIC_PROVIDER_KEY')
            thread=threading.Thread(target=relay.serve_forever,daemon=True);thread.start()
            try:
                relay.begin_attempt(path,'task',evidence_kind='synthetic')
                self.assertFalse(json.loads(path.read_text())['inventory_complete'])
                relay.close_attempt('task')
                connection=http.client.HTTPConnection('127.0.0.1',relay.server_port,timeout=5)
                connection.request('POST','/tasks/task/v1/responses','{}')
                response=connection.getresponse();self.assertEqual(response.status,409);response.read();connection.close()
                self.assertEqual(json.loads(path.read_text())['provider_requests'],[])
            finally:
                relay.shutdown();relay.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
