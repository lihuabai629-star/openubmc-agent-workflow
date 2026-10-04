from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts import stateful_agent_evaluation as evaluation

sys.path.insert(0, str(evaluation.RUNTIME_ROOT / 'tests'))
from test_mcp_contracts import FakeDebugBackend
from openubmc_target_runtime import RuntimeMcpService, SQLiteRuntimeRepository
from openubmc_target_runtime.terminal_delivery import TerminalAnswerStore


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def trial_fixture(root: Path, scenario='diagnosis-complete', mode='agent'):
    """Actual fake Runtime execution plus explicitly synthetic native event fixtures."""
    manifest = evaluation.load_manifest()
    plan = evaluation.build_plan(manifest, model='fixture', client_version='fixture-1',
                                 source_commit='a' * 40)
    case = next(c for c in manifest['scenarios'] if c['id'] == scenario)
    slot = next(s for s in plan['schedule'] if s['scenario_id'] == scenario and s['trial'] == 1)
    directory = root / slot['task_id']
    directory.mkdir()
    session = '12345678-1234-1234-1234-123456789abc'
    events = [{'type': 'session_meta', 'payload': {'id': session}}]
    trace = []
    active_turn = 'turn-initial'

    def event(kind, payload):
        events.append({'type': kind, 'timestamp': datetime.now(timezone.utc).isoformat(),
                       'payload': payload})

    class CountedBackend(FakeDebugBackend):
        @staticmethod
        def debug_run(task, arguments, context):
            with (directory / 'backend-invocations.jsonl').open('a') as stream:
                stream.write(json.dumps({'task_id': slot['task_id'], 'operation': 'debug_run'}) + '\n')
            return FakeDebugBackend.debug_run(task, arguments, context)

    repo = SQLiteRuntimeRepository(directory / 'runtime.sqlite')
    service = RuntimeMcpService(CountedBackend(), context_repository=repo)

    def call(arguments, name):
        event('response_item', {'type': 'function_call', 'call_id': name,
              'name': 'execute', 'namespace': 'mcp__runtime_fake',
              'arguments': json.dumps(arguments),
              'internal_chat_message_metadata_passthrough': {'turn_id': active_turn}})
        turn = service.call_exposed_tool('execute', arguments, task_id=slot['task_id'], operation_id=name)
        event('response_item', {'type': 'function_call_output', 'call_id': name,
              'output': 'Wall time: 0.01 seconds\nOutput:\n' + json.dumps(turn),
              'internal_chat_message_metadata_passthrough': {'turn_id': active_turn}})
        trace.append({'task_id': slot['task_id'], 'host_session_id': session,
                      'tool': 'execute', 'response_received': True, 'host_call_id': name,
                      'request_kind': arguments['kind'], 'response_run_id': turn['run_id']})
        return turn

    event('event_msg', {'type': 'task_started', 'turn_id': active_turn})
    try:
        turn = call({'kind': 'start', 'intent': 'diagnosis-only', 'target': '192.0.2.10'}, 'start')
        run_id = turn['run_id']
        if scenario == 'diagnosis-resume':
            event('event_msg', {'type': 'turn_aborted', 'turn_id': active_turn, 'reason': 'interrupted'})
            active_turn = 'turn-resumed'
            event('event_msg', {'type': 'task_started', 'turn_id': active_turn})
            turn = call({'kind': 'resume', 'run_id': run_id}, 'resume')
        gate = turn['gate']
        turn = call({'kind': 'respond', 'run_id': run_id,
                     'gate_id': gate['gate_id'], 'gate_version': gate['gate_version'],
                     'schema_digest': gate['schema_digest'],
                     'response': {'status': 'completed', 'summary': 'synthetic diagnosis',
                         'payload': {'root_cause': 'synthetic mismatch',
                                     'evidence_ids': [e['evidence_id'] for e in turn['diagnostic_receipt']['evidence']],
                                     'causal_chain': ['synthetic evidence supports cause'],
                                     'code_owner': 'src/fake.lua', 'contradictions': [],
                                     'remaining_gaps': [], 'verification_status': 'verified'}}}, 'respond')
        outcome = repo.load(run_id)['run_outcome']
        event('event_msg', {'type': 'task_complete', 'turn_id': active_turn})
    finally:
        service.close()
    store = TerminalAnswerStore(directory / 'terminal.json')
    prepared = store.prepare(task_id=slot['task_id'], run_id=run_id, outcome=outcome,
                             delivery_stage='diagnosed', text='Synthetic completion')
    observed = (datetime.fromisoformat(prepared.prepared_at) + timedelta(seconds=1)).isoformat()
    events.extend([
        {'type': 'event_msg', 'timestamp': observed,
         'payload': {'type': 'task_started', 'turn_id': 'turn-final'}},
        {'type': 'response_item', 'timestamp': observed, 'payload': {
            'type': 'message', 'id': 'final-1', 'role': 'assistant', 'phase': 'final_answer',
            'content': [{'type': 'output_text', 'text': 'Synthetic completion'}]}},
        {'type': 'event_msg', 'timestamp': observed, 'payload': {'type': 'token_count',
         'info': {'total_token_usage': {'input_tokens': 120, 'output_tokens': 30}}}},
        {'type': 'event_msg', 'timestamp': observed,
         'payload': {'type': 'task_complete', 'turn_id': 'turn-final'}},
    ])
    write_rows(directory / 'rollout.jsonl', events)
    write_rows(directory / 'host-trace.jsonl', trace)
    from openubmc_target_runtime.terminal_delivery import audit_rollout_final
    event_id, text, observed_at = audit_rollout_final(directory / 'rollout.jsonl', task_id=session,
        prepared_at=prepared.prepared_at, expected_text=prepared.text)
    store.acknowledge(task_id=slot['task_id'], run_id=run_id, outcome=outcome,
        delivery_stage='diagnosed', text=text, host_event_id=event_id, observed_at=observed_at,
        delivery_source='codex-rollout-v1')
    trial = {'scenario_id': scenario, 'scenario_version': 1, 'trial': 1,
             'task_id': slot['task_id'], 'run_id': run_id, 'host_session_id': session,
             'backend': 'runtime-fake', 'plan_digest': plan['plan_digest'], 'execution_mode': mode,
             'identity': {key: plan[key] for key in
                 ('source_commit', 'model', 'client_version', 'reasoning_effort', 'schedule_digest')}}
    trial['identity']['prompt_digest'] = slot['prompt_digest']
    trial['model_invoked'] = mode == 'agent'
    write_json(directory / 'request.json', {'execution_mode': mode, 'model_invoked': mode == 'agent'})
    write_json(directory / 'trial.json', trial)
    write_json(directory / 'timing.json', {'schema': evaluation.SCHEMA + '/timing',
               'adapter_exit_code': 0, 'elapsed_seconds': 1})
    return manifest, plan, case, trial, directory, events


class StatefulHostResumeTests(unittest.TestCase):
    def score(self, fixture):
        manifest, plan, case, trial, directory, _ = fixture
        return evaluation.score_live_trial(
            case=case, plan=plan, manifest=manifest, trial=trial,
            runtime_db=directory / 'runtime.sqlite', terminal_store=directory / 'terminal.json',
            rollout=directory / 'rollout.jsonl', elapsed_seconds=1)

    def test_resume_requires_cancel_then_same_run_execute_before_confirmed_final(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = trial_fixture(Path(raw), scenario='diagnosis-resume',
                                    mode='controlled-scripted-responses')
            result = self.score(fixture)
            self.assertEqual(result['issues'], [])
            self.assertTrue(result['recovery']['host_cancellation_verified'])
            self.assertEqual(result['recovery']['backend_invocations'], 1)
            self.assertTrue(result['host_final_confirmed'])
            self.assertFalse(result['model_invoked'])
            self.assertEqual(result['actual_agent_trials'], 0)
            self.assertEqual(result['live_acceptance'], 'unverified')

    def test_unproven_recovery_never_passes(self):
        for fault in ('missing-cancel', 'wrong-session', 'wrong-run', 'no-resume',
                      'duplicate-backend', 'missing-backend', 'wrong-effect',
                      'terminal-before-cancel', 'early-final', 'wrong-cancel-turn',
                      'wrong-call-turn', 'wrong-output-turn', 'missing-output-turn',
                      'mcp-wrong-call', 'mcp-wrong-session', 'mcp-wrong-run'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                fixture = trial_fixture(Path(raw), scenario='diagnosis-resume',
                                        mode='controlled-scripted-responses')
                directory, events = fixture[4:]
                abort = next(e for e in events if e['payload'].get('type') == 'turn_aborted')
                resume = next(e for e in events if e['payload'].get('call_id') == 'resume'
                              and e['payload'].get('type') == 'function_call')
                output = next(e for e in events if e['payload'].get('call_id') == 'resume'
                              and e['payload'].get('type') == 'function_call_output')
                if fault.startswith('mcp-'):
                    path = directory / 'host-trace.jsonl'
                    trace = [json.loads(line) for line in path.read_text().splitlines()]
                    key = {'mcp-wrong-call': 'host_call_id', 'mcp-wrong-session': 'host_session_id',
                           'mcp-wrong-run': 'response_run_id'}[fault]
                    trace[1][key] = 'wrong-binding'
                    write_rows(path, trace)
                elif fault == 'missing-cancel':
                    abort['payload']['type'] = 'task_complete'
                elif fault == 'wrong-cancel-turn':
                    abort['payload']['turn_id'] = 'other-turn'
                elif fault == 'wrong-session':
                    events[0]['payload']['id'] = 'aaaaaaaa-1234-1234-1234-123456789abc'
                elif fault == 'wrong-call-turn':
                    resume['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'other-turn'
                elif fault == 'wrong-output-turn':
                    output['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'other-turn'
                elif fault == 'missing-output-turn':
                    del output['payload']['internal_chat_message_metadata_passthrough']
                elif fault == 'wrong-run':
                    resume['payload']['arguments'] = json.dumps({'kind': 'resume', 'run_id': 'other-run'})
                elif fault == 'no-resume':
                    events.remove(resume)
                    events.remove(output)
                elif fault == 'duplicate-backend':
                    path = directory / 'backend-invocations.jsonl'
                    path.write_text(path.read_text() * 2)
                elif fault == 'missing-backend':
                    (directory / 'backend-invocations.jsonl').unlink()
                elif fault == 'wrong-effect':
                    turn = json.loads(output['payload']['output'].partition('\nOutput:\n')[2])
                    turn['diagnostic_receipt']['evidence'][0]['operation_id'] = 'other-effect'
                    output['payload']['output'] = json.dumps(turn)
                elif fault == 'terminal-before-cancel':
                    start_output = next(e for e in events if e['payload'].get('call_id') == 'start'
                                        and e['payload'].get('type') == 'function_call_output')
                    turn = json.loads(start_output['payload']['output'].partition('\nOutput:\n')[2])
                    turn['state'] = 'completed'
                    turn['outcome'] = {'status': 'completed'}
                    start_output['payload']['output'] = json.dumps(turn)
                elif fault == 'early-final':
                    final = copy.deepcopy(next(e for e in events if e['payload'].get('phase') == 'final_answer'))
                    final['timestamp'] = abort['timestamp']
                    events.insert(events.index(abort), final)
                write_rows(directory / 'rollout.jsonl', events)
                result = self.score(fixture)
                self.assertIn('host_recovery_unconfirmed', result['issues'])
                self.assertIn('scenario_not_exercised', result['issues'])
                self.assertEqual(result['actual_agent_trials'], 0)

    def test_controlled_request_cannot_be_promoted_by_changing_trial_mode(self):
        for mode in (None, 'agent', 'unknown'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                fixture = trial_fixture(root, mode='controlled-scripted-responses')
                trial = fixture[3]
                if mode is None:
                    del trial['execution_mode']
                else:
                    trial['execution_mode'] = mode
                trial['model_invoked'] = True
                with self.assertRaisesRegex(ValueError, 'execution mode'):
                    self.score(fixture)
                write_json(fixture[4] / 'trial.json', trial)
                report = evaluation.summarize_live(manifest=fixture[0], plan=fixture[1], trial_root=root)
                self.assertEqual(report['actual_agent_trials'], 0)
                self.assertEqual(report['live_acceptance'], 'unverified')

    def test_resume_cannot_become_legacy_by_losing_all_mode_markers(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = trial_fixture(root, scenario='diagnosis-resume', mode='controlled-scripted-responses')
            trial = fixture[3]
            trial.pop('execution_mode')
            trial.pop('model_invoked')
            (fixture[4] / 'request.json').unlink()
            write_json(fixture[4] / 'trial.json', trial)
            report = evaluation.summarize_live(manifest=fixture[0], plan=fixture[1], trial_root=root)
            self.assertEqual(report['actual_agent_trials'], 0)
            self.assertEqual(report['live_acceptance'], 'unverified')

    def test_resume_requires_the_respond_output_to_bind_runtime_outcome(self):
        for fault in ('missing', 'wrong-run', 'wrong-outcome', 'error-turn', 'wrong-turn'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                fixture = trial_fixture(Path(raw), scenario='diagnosis-resume', mode='controlled-scripted-responses')
                directory, events = fixture[4:]
                output = next(e for e in events if e['payload'].get('call_id') == 'respond'
                              and e['payload'].get('type') == 'function_call_output')
                if fault == 'missing':
                    events.remove(output)
                elif fault == 'wrong-turn':
                    output['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'wrong-turn'
                else:
                    turn = json.loads(output['payload']['output'].partition('\nOutput:\n')[2])
                    if fault == 'wrong-run':
                        turn['run_id'] = 'wrong-run'
                    elif fault == 'wrong-outcome':
                        turn['outcome']['summary'] = 'forged completion'
                    else:
                        turn['state'] = 'failed'
                        turn['outcome'] = None
                    output['payload']['output'] = json.dumps(turn)
                write_rows(directory / 'rollout.jsonl', events)
                self.assertIn('host_recovery_unconfirmed', self.score(fixture)['issues'])

    def test_resume_requires_explicit_mode_and_model_markers_in_both_files(self):
        for side, field in (('request', 'missing-file'), ('request', 'execution_mode'),
                            ('request', 'model_invoked'), ('trial', 'execution_mode'),
                            ('trial', 'model_invoked')):
            with self.subTest(side=side, field=field), tempfile.TemporaryDirectory() as raw:
                fixture = trial_fixture(Path(raw), scenario='diagnosis-resume', mode='controlled-scripted-responses')
                if side == 'request':
                    path = fixture[4] / 'request.json'
                    if field == 'missing-file':
                        path.unlink()
                    else:
                        request = json.loads(path.read_text())
                        del request[field]
                        write_json(path, request)
                else:
                    del fixture[3][field]
                with self.assertRaisesRegex(ValueError, 'execution mode'):
                    self.score(fixture)

    def test_controlled_host_evidence_never_counts_as_an_actual_agent_trial(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, plan, *_ = trial_fixture(root, mode='controlled-scripted-responses')
            report = evaluation.summarize_live(manifest=manifest, plan=plan, trial_root=root)
            self.assertEqual(report['actual_agent_trials'], 0)
            self.assertEqual(report['live_acceptance'], 'unverified')
            self.assertEqual(report['controlled_trials_excluded'], 1)


if __name__ == '__main__':
    unittest.main()
