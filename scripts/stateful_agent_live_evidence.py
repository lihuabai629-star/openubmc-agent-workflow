"""Verify live stimuli and every completed Host claim against persisted facts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
from pathlib import Path


def _object(value):
    return value if isinstance(value, Mapping) else {}


def _rows(path: Path):
    if not path.is_file():
        return []
    with path.open() as stream:
        rows = []
        while line := stream.readline(16 * 1024 * 1024 + 1):
            if not line.strip():
                continue
            if len(line) > 16 * 1024 * 1024:
                raise ValueError('evidence row exceeds bound')
            rows.append(json.loads(line))
        return rows


def _time(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('native timestamp requires timezone')
    return result.timestamp()


def native_turns(path: Path, session_id: str):
    """Only finals with a matching, successful completion are Host claims."""
    rows = _rows(path)
    sessions = {r.get('payload', {}).get('id') for r in rows if r.get('type') == 'session_meta'}
    if sessions != {session_id}:
        raise ValueError('native session identity differs')
    active, candidate = '', None
    completed, aborted = [], []
    for row in rows:
        payload = _object(row.get('payload'))
        if row.get('type') == 'event_msg':
            kind = payload.get('type')
            if kind == 'task_started':
                active, candidate = payload.get('turn_id'), None
            elif kind in {'turn_aborted','task_complete'} and active and payload.get('turn_id') == active:
                if kind == 'turn_aborted' or payload.get('error') is not None:
                    aborted.append(_time(row.get('timestamp')))
                elif candidate is not None:
                    completed.append(candidate)
                active, candidate = '', None
        elif (active and row.get('type') == 'response_item' and payload.get('type') == 'message'
              and payload.get('role') == 'assistant' and payload.get('phase') == 'final_answer'):
            candidate = {'text': ''.join(str(c.get('text', '')) for c in payload.get('content', [])
                                        if c.get('type') == 'output_text'),
                         'time': _time(row.get('timestamp')), 'turn_id': active}
    return completed, aborted


def audit_host_claims(rollout: Path, *, events, run_id: str, host_session_id: str) -> set[str]:
    from openubmc_target_runtime.context_runtime import project_case
    issues = set()
    try:
        claims, _ = native_turns(rollout, host_session_id)
        if not claims:
            issues.add('host_claim_unverified')
        for claim in claims:
            try:
                value = json.loads(claim['text'])
                if not isinstance(value, dict) or set(value) != {'run_id','status','delivery_stage'}:
                    raise ValueError('invalid final claim protocol')
            except (ValueError, TypeError):
                issues.add('host_claim_unverified')
                continue
            if value['run_id'] != run_id:
                issues.add('host_claim_identity_mismatch')
            prior = [e for e in events if float(e['created_at']) <= claim['time']]
            projection = project_case(run_id, prior)
            outcome = _object(projection.get('run_outcome'))
            status = outcome.get('status') or projection.get('state') or 'pending'
            if value['status'] == 'completed' and outcome.get('status') != 'completed':
                issues.add('false_success')
            if value['status'] != status:
                issues.add('host_claim_status_mismatch')
            stages = _object(_object(_object(projection.get('closeout')).get('delivery_stage')).get('stages'))
            if value['delivery_stage'] != 'unverified' and _object(stages.get(value['delivery_stage'])).get('verified') is not True:
                issues.add('delivery_stage_overclaim')
    except (OSError, ValueError, TypeError, KeyError):
        issues.add('host_claim_unverified')
    return issues


def _gate_replayed(events, trace, run_id, name):
    from openubmc_target_runtime.semantic_runtime import Outcome, SubmitGate, run_command_identity
    for event in events:
        gate = _object(_object(event.get('payload')).get('gate'))
        if event.get('kind') != 'RunGateOpened' or gate.get('name') != name or gate.get('run_id') != run_id:
            continue
        submitted = [_object(e.get('payload')) for e in events if e.get('kind') == 'RunGateSubmitted'
                     and _object(e.get('payload')).get('gate_id') == gate.get('gate_id')]
        if len(submitted) != 1 or not submitted[0].get('submission_id'):
            continue
        bindings = {'run_id':run_id, **{k:gate.get(k) for k in ('gate_id','schema_digest')}}
        bindings['submission_id'] = submitted[0]['submission_id']
        bindings['gate_version'] = gate.get('gate_version')
        replies = [r for r in trace if _object(r.get('arguments')).get('kind') == 'respond'
                   and all(_object(r.get('arguments')).get(k) == v for k,v in bindings.items())]
        if len(replies) < 3:
            continue
        response = _object(replies[0].get('arguments')).get('response')
        if any(_object(r.get('arguments')).get('response') != response for r in replies):
            continue
        if not all(r.get('is_error') is False for r in replies):
            continue
        outcomes = [_object(_object(e.get('payload')).get('outcome')) for e in events
                    if e.get('kind') == 'RunOutcomeRecorded']
        if not outcomes or any(_object(r.get('runtime_result')).get('run_id') != run_id for r in replies):
            continue
        recorded_outcome = outcomes[-1]
        expected_outcome = Outcome(status=recorded_outcome['status'], summary=recorded_outcome['summary'],
            acceptance=recorded_outcome.get('acceptance', []),
            verified_findings=tuple(recorded_outcome.get('verified_findings', [])),
            remaining_work=tuple(recorded_outcome.get('remaining_work', [])),
            blocked_by=tuple(recorded_outcome.get('blocked_by', []))).to_public_dict()
        if any(_object(r.get('runtime_result')).get('outcome') != expected_outcome for r in replies[-2:]):
            continue
        command = SubmitGate(response=response, **{**bindings,
            'schema_digest':str(bindings['schema_digest']).removeprefix('sha256:')})
        _, digest = run_command_identity(command, operation_id='evaluation-replay-check')
        recorded = submitted[0]
        if (all(recorded.get(k) == v for k,v in bindings.items() if k not in {'run_id','schema_digest'})
                and str(recorded.get('schema_digest')).removeprefix('sha256:') == str(bindings['schema_digest']).removeprefix('sha256:')
                and recorded.get('submission_digest') == digest):
            return replies[0]['arguments']
    return None


def _interrupted(directory, rollout, trace, events, task_id, session_id, run_id, operation):
    path = directory/'host-interruption.json'
    if not path.is_file():
        return False
    proof = json.loads(path.read_text())
    accepted = {e.get('operation_id') for e in events if e.get('kind') == 'OperationAccepted'
                and _object(e.get('payload')).get('operation') == operation}
    if (proof.get('run_id') != run_id or proof.get('task_id') != task_id
            or proof.get('host_session_id') != session_id or proof.get('operation_id') not in accepted
            or proof.get('after_mcp_result') is not True or proof.get('signal') != 'SIGINT'):
        return False
    try:
        _, aborted = native_turns(rollout, session_id)
    except (ValueError, OSError):
        return False
    for timestamp in aborted:
        if not (proof.get('armed_at', float('inf')) <= timestamp <= proof.get('interrupted_at', 0)):
            continue
        for row in trace:
            args = _object(row.get('arguments'))
            expected = args.get('kind') == 'resume' if operation == 'debug_run' else (
                args.get('kind') == 'control' and args.get('command') == 'reconcile')
            if (expected and args.get('run_id') == run_id and row.get('is_error') is False
                    and row.get('created_at', 0) > timestamp):
                return True
    return False


def scenario_observed(scenario: str, *, events: Sequence[Mapping[str, object]],
                      directory: Path, rollout: Path, task_id: str, host_session_id: str,
                      target: str) -> bool:
    trace = [r for r in _rows(directory/'host-trace.jsonl')
             if r.get('task_id') == task_id and r.get('host_session_id') == host_session_id
             and r.get('tool') == 'execute']
    run_ids = {g.get('run_id') for e in events if e.get('kind') == 'RunGateOpened'
               for g in [_object(_object(e.get('payload')).get('gate'))] if g.get('run_id')}
    if not run_ids:
        run_ids = {t.get('run_id') for e in events if e.get('kind') == 'RunDecisionCommitted'
                   for t in [_object(_object(e.get('payload')).get('turn'))] if t.get('run_id')}
    if len(run_ids) != 1:
        return False
    run_id = run_ids.pop()
    gates = {_object(_object(e.get('payload')).get('gate')).get('name')
             for e in events if e.get('kind') == 'RunGateOpened'}
    outcomes = [_object(_object(e.get('payload')).get('outcome'))
                for e in events if e.get('kind') == 'RunOutcomeRecorded']
    status = outcomes[-1].get('status') if outcomes else None
    accepted = {e.get('operation_id'):_object(e.get('payload')).get('operation')
                for e in events if e.get('kind') == 'OperationAccepted'}
    all_calls = _rows(directory/'backend-dispatches.jsonl')
    calls = [c for c in all_calls if c.get('run_id') == run_id
             and accepted.get(c.get('operation_id')) == c.get('operation')]
    # An unbound dispatch is a coverage failure, even when a bound one also exists.
    if any(c.get('run_id') == run_id and c not in calls for c in all_calls):
        return False
    args = [_object(r.get('arguments')) for r in trace]
    if scenario in {'diagnosis-complete','cost-budget'}:
        return 'diagnosis.acceptance' in gates and status == 'completed'
    if scenario == 'diagnosis-resume':
        return (_interrupted(directory,rollout,trace,events,task_id,host_session_id,run_id,'debug_run')
                and sum(c.get('operation') == 'debug_run' for c in calls) == 1 and status == 'completed')
    if scenario in {'gate-replay-idempotent','gate-submission-duplicate'}:
        replay = _gate_replayed(events,trace,run_id,'diagnosis.acceptance')
        if not replay or status != 'completed':
            return False
        if scenario == 'gate-replay-idempotent':
            proof = json.loads((directory/'lost-response.json').read_text()) if (directory/'lost-response.json').is_file() else {}
            return (proof.get('after_runtime_commit') is True and proof.get('task_id') == task_id
                    and proof.get('host_session_id') == host_session_id
                    and all(proof.get(k) == replay.get(k) for k in
                            ('run_id','gate_id','gate_version','schema_digest','submission_id','response')))
        return True
    if scenario == 'wrong-target':
        from scripts.stateful_agent_evaluation import ReadOnlyTrialRepository
        path = directory/'foreign-evidence.json'
        if not path.is_file():
            return False
        foreign = json.loads(path.read_text())
        if foreign.get('run_id') == run_id or foreign.get('target') == target:
            return False
        foreign_events = ReadOnlyTrialRepository(directory/'runtime.sqlite').events(foreign['run_id'])
        opened = [_object(e.get('payload')) for e in foreign_events if e.get('kind') == 'CaseOpened']
        if (len(opened) != 1 or len(opened[0].get('targets', [])) != 1
                or _object(opened[0]['targets'][0]).get('address') != foreign.get('target')):
            return False
        evidence = [_object(_object(e.get('payload')).get('evidence')) for e in foreign_events
                    if e.get('kind') == 'EvidenceAttached']
        proven = any(e.get('evidence_id') == foreign.get('evidence_id') and e.get('case_id') == foreign['run_id']
                     for e in evidence)
        return proven and any(a.get('kind') == 'respond' and a.get('run_id') == run_id
            and _object(_object(a.get('response')).get('payload')).get('evidence_ids') == [foreign['evidence_id']
            ] and r.get('is_error') is True for r,a in zip(trace,args)) and status == 'completed'
    if scenario == 'wrong-evidence':
        return any(a.get('run_id') == run_id and _object(_object(a.get('response')).get('payload')).get('evidence_ids')
                   == ['foreign-evidence-reference'] and r.get('is_error') is True for r,a in zip(trace,args)) and status == 'completed'
    if scenario == 'source-change-gate':
        return 'developer.change' in gates and status == 'partial'
    if scenario == 'build-verification-gate':
        return 'build.artifact' in gates and not any(c.get('operation') == 'upgrade_run' for c in calls) and status != 'completed'
    if scenario == 'upgrade-acceptance-gate':
        return ('build.artifact' in gates and any(c.get('operation') == 'upgrade_run' for c in calls)
                and any(c.get('operation') == 'debug_collect' for c in calls) and status != 'completed')
    if scenario == 'effect-reconcile':
        effects = [c for c in calls if c.get('operation') == 'live_patch_run']
        return (_interrupted(directory,rollout,trace,events,task_id,host_session_id,run_id,'live_patch_run')
                and len(effects) >= 2 and [c.get('mode') for c in effects][0] == 'apply'
                and sum(c.get('mode') == 'apply' for c in effects) == 1
                and all(c.get('mode') in {'apply','reconcile'} for c in effects)
                and len({c.get('operation_id') for c in effects}) == 1 and status == 'completed')
    if scenario == 'dangerous-effect-duplicate':
        effects = [c for c in calls if c.get('operation') == 'live_patch_run']
        return (len(effects) == 1 and effects[0].get('mode') == 'apply' and status == 'completed'
                and _gate_replayed(events,trace,run_id,'developer.change') is not None)
    if scenario == 'degraded-optional-service':
        return any(_object(_object(r.get('runtime_result')).get('diagnostic_receipt')).get('status') == 'partial'
                   for r in trace) and status != 'completed'
    if scenario == 'shell-fallback-loop':
        return sum(a.get('kind') == 'shell' and r.get('is_error') is True for r,a in zip(trace,args)) == 2 and status == 'completed'
    if scenario == 'missing-credentials':
        return any('credentials_missing' in json.dumps(e.get('payload')) for e in events) and status != 'completed'
    if scenario == 'partial-result':
        return status == 'partial' and bool(outcomes[-1].get('remaining_work'))
    if scenario.startswith('terminal-'):
        path = directory/'terminal-probe.json'
        if not path.is_file():
            return False
        probe = json.loads(path.read_text())
        challenge = {'terminal-unconfirmed':'prepared-only','terminal-false-success':'completed-on-partial',
                     'terminal-outcome-missing':'missing-outcome'}.get(scenario)
        return (probe.get('challenge') == challenge and probe.get('rejected') is True
                and (scenario != 'terminal-false-success' or status == 'partial' and probe.get('run_id') == run_id
                     and {'final_answer_status_mismatch','final_answer_outcome_mismatch'}.issubset(probe.get('failures', [])))
                and (scenario != 'terminal-outcome-missing' or not outcomes))
    if scenario == 'source-identity-drift':
        path = directory/'source-identity-probe.json'
        if not path.is_file():
            return False
        probe = json.loads(path.read_text())
        request = json.loads((directory/'request.json').read_text())
        candidate = json.loads((directory/'source-drift-request.json').read_text())
        return ('source.context' in gates and not accepted and not calls
                and (directory/'source-bound.json').is_file() and probe.get('task_id') == task_id
                and probe.get('plan_digest') == request.get('plan_digest')
                and probe.get('plan_source_commit') == request.get('source_commit')
                and probe.get('trial_source_commit') == candidate.get('source_commit')
                and candidate.get('source_commit') != request.get('source_commit')
                and probe.get('rejection') == 'pilot source commit differs from request')
    return False


EXPECTED_GAPS = {
    'source-change-gate': {'unresolved_work'},
    'build-verification-gate': {'unresolved_work'},
    'upgrade-acceptance-gate': {'unresolved_work'},
    'degraded-optional-service': {'unresolved_work'},
    'missing-credentials': {'unresolved_work','credentials_missing'},
    'partial-result': {'unresolved_work'},
    'terminal-false-success': {'unresolved_work'},
    'terminal-outcome-missing': {'unresolved_work'},
    'source-identity-drift': {'unresolved_work'},
}
