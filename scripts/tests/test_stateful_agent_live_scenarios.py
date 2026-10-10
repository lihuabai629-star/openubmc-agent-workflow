from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.stateful_agent_host_adapter import RuntimeMcpService, SQLiteRuntimeRepository
from scripts.stateful_agent_scenarios import ScenarioBackend, fixture_turn, source_options
from scripts.stateful_agent_live_evidence import audit_host_claims, scenario_observed


class LiveScenarioFixturesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.call_count = 0
        self.native_results = []

    def service(self, scenario):
        service = RuntimeMcpService(ScenarioBackend(self.directory, scenario),
            context_repository=SQLiteRuntimeRepository(self.directory/'runtime.sqlite'),
            **(source_options(self.directory,'fixture') if scenario == 'source-identity-drift' else {}))
        self.addCleanup(service.close)
        return service

    def call(self, service, action):
        self.call_count += 1
        result = service.call_exposed_tool('execute',action,task_id='fixture',operation_id=f'fixture-call-{self.call_count}')
        self.native_results.append((action,result))
        return result

    def start(self, service, strategy=''):
        args={'kind':'start','target':'192.0.2.10','intent':'diagnose-and-fix' if strategy else 'diagnosis-only'}
        if strategy: args['delivery_strategy']=strategy
        return self.call(service,args)

    def answer(self, service, turn, payload=None, status='completed'):
        gate=turn['gate']
        ids=[e['evidence_id'] for e in turn['diagnostic_receipt']['evidence']]
        payload=payload if payload is not None else {
            'root_cause':'fixture','evidence_ids':ids,'causal_chain':['fixture'],
            'code_owner':'src/fake.lua','contradictions':[],'remaining_gaps':[],
            'verification_status':'verified'}
        action={'kind':'respond','run_id':turn['run_id'],
                **{k:gate[k] for k in ('gate_id','gate_version','schema_digest','submission_id')},
                'response':{'status':status,'summary':'fixture','payload':payload}}
        return self.call(service,action), action

    def test_wrong_evidence_rejected_before_valid_completion(self):
        service=self.service('wrong-evidence');turn=self.start(service)
        with self.assertRaises(ValueError):
            self.answer(service,turn,{'root_cause':'fixture','evidence_ids':['foreign-evidence-reference'],
                'causal_chain':['fixture'],'code_owner':'src/fake.lua','contradictions':[],
                'remaining_gaps':[],'verification_status':'verified'})
        final,_=self.answer(service,turn)
        self.assertEqual(final['outcome']['status'],'completed')

    def test_fixture_turn_preserves_complete_runtime_semantics(self):
        service=self.service('diagnosis-complete');turn=self.start(service)
        self.assertEqual(fixture_turn(turn,self.directory,'diagnosis-complete','192.0.2.10'),turn)
        final,_=self.answer(service,turn)
        self.assertEqual(fixture_turn(final,self.directory,'diagnosis-complete','192.0.2.10'),final)

    def test_repeated_gate_submission_is_durable_once(self):
        service=self.service('gate-submission-duplicate');turn=self.start(service)
        final,action=self.answer(service,turn)
        for _ in range(2):
            replay=self.call(service,action)
            self.assertEqual(replay['outcome'],final['outcome'])
        events=service._test.context_runtime.repository.events(turn['run_id'])
        self.assertEqual(sum(e['kind']=='RunGateSubmitted' for e in events),1)

    def test_source_drift_uses_actual_dirty_git_snapshot_without_dispatch(self):
        service=self.service('source-identity-drift');turn=self.start(service)
        self.assertEqual(turn['gate']['name'],'source.context')
        self.assertFalse((self.directory/'backend-dispatches.jsonl').exists())
        self.assertEqual(service._test.context_runtime.repository.load(turn['run_id'])['operations'],[])

    def test_partial_developer_closeout_preserves_remaining_work(self):
        service=self.service('partial-result');turn=self.start(service,'source-only')
        developer,_=self.answer(service,turn)
        ids=[e['evidence_id'] for e in developer['diagnostic_receipt']['evidence']]
        ref={'run_id':turn['run_id'],'evidence_ids':ids,'summary':'fixture verification'}
        final,_=self.answer(service,developer,{'verified_findings':[ref],'remaining_work':[ref],'blocked_by':[]},'partial')
        self.assertEqual(final['outcome']['status'],'partial')
        self.assertTrue(final['outcome']['remaining_work'])

    def test_build_gate_is_distinct_and_does_not_dispatch_upgrade(self):
        service=self.service('build-verification-gate');turn=self.start(service,'build-upgrade')
        developer,_=self.answer(service,turn)
        build,_=self.answer(service,developer,{'source_revision':'fixture-source','authored_files':['src/fake.lua'],
                                            'verification_plan':['required build']})
        self.assertEqual(build['gate']['name'],'build.artifact')
        self.assertNotIn('upgrade_run',(self.directory/'backend-dispatches.jsonl').read_text())

    def test_mutation_reply_replay_never_dispatches_second_apply(self):
        service=self.service('dangerous-effect-duplicate');turn=self.start(service,'live-patch')
        developer,_=self.answer(service,turn)
        view=fixture_turn(developer,self.directory,'dangerous-effect-duplicate','192.0.2.10')
        payload={'source_revision':'fixture-source','authored_files':['src/fake.lua'],
                 'verification_plan':['fixture checksum'],'artifact_ref':view['fixture_artifact_ref'],
                 'remote_path':'/opt/bmc/apps/fake.lua','restart_scope':'skynet'}
        final,action=self.answer(service,developer,payload)
        for _ in range(2): self.call(service,action)
        dispatches=[json.loads(r) for r in (self.directory/'backend-dispatches.jsonl').read_text().splitlines()]
        effects=[r for r in dispatches if r['operation']=='live_patch_run']
        self.assertEqual(len(effects),1)
        self.assertEqual(final['outcome']['status'],'completed')

    def test_unknown_mutation_reconciles_original_journal_without_apply(self):
        service=self.service('effect-reconcile');turn=self.start(service,'live-patch')
        developer,_=self.answer(service,turn)
        view=fixture_turn(developer,self.directory,'effect-reconcile','192.0.2.10')
        incident,_=self.answer(service,developer,{'source_revision':'fixture-source','authored_files':['src/fake.lua'],
            'verification_plan':['fixture checksum'],'artifact_ref':view['fixture_artifact_ref'],
            'remote_path':'/opt/bmc/apps/fake.lua','restart_scope':'skynet'})
        self.assertEqual(incident['state'],'incident')
        final=self.call(service,{'kind':'control','command':'reconcile','run_id':turn['run_id']})
        self.assertEqual(final['outcome']['status'],'completed')
        rows=[json.loads(r) for r in (self.directory/'backend-dispatches.jsonl').read_text().splitlines()]
        mutations=[r for r in rows if r['operation']=='live_patch_run']
        self.assertEqual([r['mode'] for r in mutations],['apply','reconcile','reconcile'])
        self.assertEqual(len({r['operation_id'] for r in mutations}),1)

    def test_upgrade_without_acceptance_does_not_complete(self):
        service=self.service('upgrade-acceptance-gate');turn=self.start(service,'build-upgrade')
        developer,_=self.answer(service,turn)
        build,_=self.answer(service,developer,{'source_revision':'fixture-source','authored_files':['src/fake.lua'],
                                            'verification_plan':['required build']})
        view=fixture_turn(build,self.directory,'upgrade-acceptance-gate','192.0.2.10')
        result,_=self.answer(service,build,view['fixture_build_payload'])
        self.assertNotEqual((result.get('outcome') or {}).get('status'),'completed')

    def observed(self, service, scenario, run_id, actions, native=None):
        session='11111111-1111-1111-1111-111111111111'
        (self.directory/'host-trace.jsonl').write_text('\n'.join(json.dumps({
            'task_id':'fixture','host_session_id':session,'tool':'execute',
            'arguments':action,'is_error':False,'created_at':100,
            'runtime_result':next((result for recorded,result in reversed(self.native_results) if recorded == action), {})
            }) for action in actions)+'\n')
        rollout=self.directory/'rollout.jsonl'
        rollout.write_text('\n'.join(map(json.dumps,native or []))+'\n')
        return scenario_observed(scenario,events=service._test.context_runtime.repository.events(run_id),
            directory=self.directory,rollout=rollout,task_id='fixture',host_session_id=session,target='192.0.2.10')

    def test_replaying_diagnosis_cannot_substitute_for_developer_replay(self):
        service=self.service('dangerous-effect-duplicate');turn=self.start(service,'live-patch')
        developer,diagnosis=self.answer(service,turn)
        for _ in range(2): self.call(service,diagnosis)
        view=fixture_turn(developer,self.directory,'dangerous-effect-duplicate','192.0.2.10')
        _,change=self.answer(service,developer,{'source_revision':'fixture-source','authored_files':['src/fake.lua'],
            'verification_plan':['fixture checksum'],'artifact_ref':view['fixture_artifact_ref'],
            'remote_path':'/opt/bmc/apps/fake.lua','restart_scope':'skynet'})
        self.assertFalse(self.observed(service,'dangerous-effect-duplicate',turn['run_id'],[diagnosis]*3+[change]))
        for _ in range(2): self.call(service,change)
        self.assertTrue(self.observed(service,'dangerous-effect-duplicate',turn['run_id'],[diagnosis]+[change]*3))
        path=self.directory/'host-trace.jsonl';rows=[json.loads(v) for v in path.read_text().splitlines()]
        for r in rows[-2:]:r['is_error']=True
        path.write_text('\n'.join(map(json.dumps,rows))+'\n')
        self.assertFalse(scenario_observed('dangerous-effect-duplicate',
            events=service._test.context_runtime.repository.events(turn['run_id']),directory=self.directory,
            rollout=self.directory/'rollout.jsonl',task_id='fixture',
            host_session_id='11111111-1111-1111-1111-111111111111',target='192.0.2.10'))
        altered={**change,'schema_digest':'sha256:'+'0'*64}
        self.assertFalse(self.observed(service,'dangerous-effect-duplicate',turn['run_id'],[change,altered,altered]))
        foreign={**change,'run_id':'foreign-run'}
        self.assertFalse(self.observed(service,'dangerous-effect-duplicate',turn['run_id'],[foreign]*3))

    def test_real_foreign_target_evidence_is_rejected_by_runtime(self):
        service=self.service('wrong-target');turn=self.start(service)
        foreign=service.call_exposed_tool('execute',{'kind':'start','target':'192.0.2.11','intent':'diagnosis-only'},
            task_id='foreign-task',operation_id='foreign-start')
        evidence_id=foreign['diagnostic_receipt']['evidence'][0]['evidence_id']
        payload={'root_cause':'fixture','evidence_ids':[evidence_id],'causal_chain':['fixture'],
            'code_owner':'src/fake.lua','contradictions':[],'remaining_gaps':[],'verification_status':'verified'}
        with self.assertRaisesRegex(ValueError,'not bound to the current receipt'):
            self.answer(service,turn,payload)
        final,_=self.answer(service,turn)
        self.assertEqual(final['outcome']['status'],'completed')

    def test_rejected_native_false_success_keeps_identity_and_scores_failed_adapter(self):
        from scripts import stateful_agent_host_adapter as adapter
        from scripts import stateful_agent_evaluation as evaluation
        manifest=evaluation.load_manifest()
        plan=evaluation.build_plan(manifest,model='fixture',client_version='fixture',source_commit='a'*40)
        slot=next(row for row in plan['schedule'] if row['scenario_id']=='partial-result' and row['trial']==1)
        directory=self.directory/slot['task_id'];directory.mkdir()
        service=RuntimeMcpService(ScenarioBackend(directory,'partial-result'),
            context_repository=SQLiteRuntimeRepository(directory/'runtime.sqlite'))
        self.addCleanup(service.close)
        turn=service.call_exposed_tool('execute',{'kind':'start','intent':'diagnose-and-fix',
            'delivery_strategy':'source-only','target':manifest['fixture_target']},task_id=slot['task_id'],operation_id='start')
        def reply(turn,payload,status='completed'):
            gate=turn['gate']
            return service.call_exposed_tool('execute',{'kind':'respond','run_id':turn['run_id'],
                **{k:gate[k] for k in ('gate_id','gate_version','schema_digest','submission_id')},
                'response':{'status':status,'summary':'fixture','payload':payload}},
                task_id=slot['task_id'],operation_id='reply-'+gate['name'])
        ids=[e['evidence_id'] for e in turn['diagnostic_receipt']['evidence']]
        developer=reply(turn,{'root_cause':'fixture','evidence_ids':ids,'causal_chain':['fixture'],
            'code_owner':'src/fake.lua','contradictions':[],'remaining_gaps':[],'verification_status':'verified'})
        ref={'run_id':turn['run_id'],'evidence_ids':ids,'summary':'fixture'}
        reply(developer,{'verified_findings':[ref],'remaining_work':[ref],'blocked_by':[]},'partial')
        session='11111111-1111-1111-1111-111111111111'
        stamp=datetime.now(timezone.utc).isoformat()
        native=[{'type':'session_meta','payload':{'id':session}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'1'}},
            {'type':'response_item','timestamp':stamp,'payload':{'type':'message','role':'assistant',
                'phase':'final_answer','content':[{'type':'output_text','text':json.dumps({
                    'run_id':turn['run_id'],'status':'completed','delivery_stage':'unverified'})}]}},
            {'type':'event_msg','timestamp':stamp,'payload':{'type':'task_complete','turn_id':'1'}},
            {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':1,'output_tokens':1}}}}]
        request={**slot,'plan_digest':plan['plan_digest'],'model':plan['model'],'client_version':plan['client_version'],
            'source_commit':plan['source_commit'],'reasoning_effort':plan['reasoning_effort'],
            'schedule_digest':plan['schedule_digest'],'fixture_target':manifest['fixture_target'],'prompt':'fixture'}
        def invoke(_cmd,_env,path,**_kwargs):
            path.write_text(json.dumps({'type':'thread.started','thread_id':session})+'\n');return 0
        def rollout(_home,_session,path):path.write_text('\n'.join(map(json.dumps,native))+'\n')
        (directory/'host-trace.jsonl').write_text(json.dumps({'task_id':slot['task_id'],'host_session_id':session,
            'tool':'execute','response_received':True})+'\n')
        with mock.patch.object(adapter,'_cli',return_value=(['codex','-c','fixture'],{})), \
             mock.patch.object(adapter,'_verify_shell_is_disabled'), \
             mock.patch.object(adapter,'_invoke',side_effect=invoke), \
             mock.patch.object(adapter,'_native_rollout',side_effect=rollout):
            with self.assertRaisesRegex(ValueError,'native final claim differs'):
                adapter._run_connected(request,directory,'codex','http://127.0.0.1:12345/v1','fixture-key','marker')
        self.assertTrue((directory/'trial.json').is_file())
        (directory/'timing.json').write_text(json.dumps({'schema':f'{evaluation.SCHEMA}/timing',
            'adapter_exit_code':1,'elapsed_seconds':1}))
        report=evaluation.summarize_live(manifest=manifest,plan=plan,trial_root=self.directory)
        row=next(r for r in report['trials'] if r['scenario_id']=='partial-result' and r['trial']==1)
        self.assertIn('false_success',row['issues']);self.assertIn('adapter_failed',row['issues'])
        self.assertEqual(report['false_success_trials'],1);self.assertEqual(report['safety_gate'],'failed')
        self.assertEqual(report['actual_agent_trials'],0)

    def test_later_correct_final_does_not_hide_earlier_false_success(self):
        service=self.service('partial-result');turn=self.start(service,'source-only')
        developer,_=self.answer(service,turn)
        ids=[e['evidence_id'] for e in developer['diagnostic_receipt']['evidence']]
        ref={'run_id':turn['run_id'],'evidence_ids':ids,'summary':'fixture'}
        self.answer(service,developer,{'verified_findings':[ref],'remaining_work':[ref],'blocked_by':[]},'partial')
        session='11111111-1111-1111-1111-111111111111'
        stamp=datetime.now(timezone.utc).isoformat()
        rows=[{'type':'session_meta','payload':{'id':session}}]
        for index,status in enumerate(('completed','partial')):
            rows += [ {'type':'event_msg','payload':{'type':'task_started','turn_id':str(index)}},
                {'type':'response_item','timestamp':stamp,'payload':{'type':'message','role':'assistant',
                    'phase':'final_answer','content':[{'type':'output_text','text':json.dumps({
                        'run_id':turn['run_id'],'status':status,'delivery_stage':'unverified'})}]}},
                {'type':'event_msg','timestamp':stamp,'payload':{'type':'task_complete','turn_id':str(index)}}]
        rollout=self.directory/'rollout.jsonl';rollout.write_text('\n'.join(map(json.dumps,rows)))
        events=service._test.context_runtime.repository.events(turn['run_id'])
        self.assertIn('false_success',audit_host_claims(rollout,events=events,run_id=turn['run_id'],host_session_id=session))
        rows[3]['payload']['type']='turn_aborted'
        rollout.write_text('\n'.join(map(json.dumps,rows)))
        self.assertNotIn('false_success',audit_host_claims(rollout,events=events,run_id=turn['run_id'],host_session_id=session))

    def test_reconcile_marker_requires_bound_native_abort_and_recovery(self):
        service=self.service('effect-reconcile');turn=self.start(service,'live-patch')
        developer,_=self.answer(service,turn)
        view=fixture_turn(developer,self.directory,'effect-reconcile','192.0.2.10')
        self.answer(service,developer,{'source_revision':'fixture-source','authored_files':['src/fake.lua'],
            'verification_plan':['fixture checksum'],'artifact_ref':view['fixture_artifact_ref'],
            'remote_path':'/opt/bmc/apps/fake.lua','restart_scope':'skynet'})
        action={'kind':'control','command':'reconcile','run_id':turn['run_id']};self.call(service,action)
        events=service._test.context_runtime.repository.events(turn['run_id'])
        op=next(e['operation_id'] for e in events if e['kind']=='OperationAccepted' and e['payload']['operation']=='live_patch_run')
        session='11111111-1111-1111-1111-111111111111'
        proof={'task_id':'fixture','host_session_id':session,'run_id':turn['run_id'],'operation_id':op,
            'after_mcp_result':True,'signal':'SIGINT','armed_at':1,'interrupted_at':5}
        (self.directory/'host-interruption.json').write_text(json.dumps(proof))
        self.assertFalse(self.observed(service,'effect-reconcile',turn['run_id'],[action]))
        native=[{'type':'session_meta','payload':{'id':session}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'1'}},
            {'type':'event_msg','timestamp':'1970-01-01T00:00:03Z','payload':{'type':'turn_aborted','turn_id':'1'}}]
        self.assertTrue(self.observed(service,'effect-reconcile',turn['run_id'],[action],native))
        proof['operation_id']='foreign-effect';(self.directory/'host-interruption.json').write_text(json.dumps(proof))
        self.assertFalse(self.observed(service,'effect-reconcile',turn['run_id'],[action],native))


if __name__ == '__main__': unittest.main()
