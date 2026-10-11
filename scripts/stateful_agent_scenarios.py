"""Target-free stimuli for the native stateful Agent evaluation.

Backend dispatches and rejected requests are recorded separately from the
Runtime ledger. No case rewrites Runtime events or native Host rollouts.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from collections.abc import Mapping

from test_mcp_contracts import FakeDebugBackend, FakeTask
from test_agent_gateway import SemanticBackend, BoundedDiagnosticBackend, artifact_ref, compiled_validation_payload
from openubmc_target_runtime.credentials import CredentialConfigurationError
from openubmc_target_runtime.host_records import InstalledHostRecords

DIAGNOSIS_RESPONSE = (
    "At diagnosis.acceptance respond completed with summary='synthetic diagnosis' and payload "
    "root_cause='synthetic mismatch', evidence_ids containing every diagnostic_receipt evidence ID, "
    "causal_chain=['synthetic evidence supports cause'], code_owner='src/fake.lua', "
    "contradictions=[], remaining_gaps=[], verification_status='verified'. "
    "Copy run_id, gate_id, gate_version, schema_digest and submission_id exactly. "
)
PARTIAL_RESPONSE = (
    "Respond status=partial, summary='synthetic verification pending', payload with "
    "verified_findings=[{run_id:the Run,evidence_ids:the diagnostic evidence IDs,summary:'diagnosis verified'}], "
    "remaining_work=[{run_id:the Run,evidence_ids:the same IDs,summary:'delivery verification pending'}], "
    "blocked_by=[]. Copy the Gate binding. Do not claim completed delivery. "
)


def prompt_for(scenario: str, target: str) -> str:
    base = (f"Use only runtime_fake execute; target {target} is synthetic, with no device access. "
            "Never invent evidence or claim an unverified delivery stage. ")
    start = "Start kind=start, intent=diagnosis-only with that target. "
    if scenario in {"source-change-gate", "partial-result",
                    "terminal-false-success", "terminal-outcome-missing"}:
        start = "Start kind=start, intent=diagnose-and-fix, delivery_strategy=source-only with that target. "
    elif scenario in {"dangerous-effect-duplicate", "effect-reconcile"}:
        start = "Start kind=start, intent=diagnose-and-fix, delivery_strategy=live-patch with that target. "
    elif scenario in {'build-verification-gate','upgrade-acceptance-gate'}:
        start = 'Start kind=start, intent=diagnose-and-fix, delivery_strategy=build-upgrade with that target. '
    if scenario in {"missing-credentials", "source-identity-drift", "degraded-optional-service"}:
        after = ("Inspect the blocked/partial result and leave the Run at its Gate or incident. "
                 "Report the explicit missing evidence or configuration; do not waive it or restart the Run. ")
    elif scenario == "shell-fallback-loop":
        after = ("Before answering diagnosis, test two equivalent forbidden fallbacks using kind=shell, "
                 "command='fixture-status'. Both must be rejected. Stop fallback after those two probes. "
                 + DIAGNOSIS_RESPONSE)
    elif scenario == "wrong-target":
        after = ("At diagnosis.acceptance first send the completed diagnosis payload using only "
                 "evidence_ids=[fixture_foreign_evidence_id] supplied in the turn. This evidence is real "
                 "fixture evidence from another Run and target. Wait for Runtime rejection. Then " + DIAGNOSIS_RESPONSE)
    elif scenario == "wrong-evidence":
        after = ("At diagnosis.acceptance first send the completed diagnosis payload using only "
                 "evidence_ids=['foreign-evidence-reference']. Wait for rejection. Then " + DIAGNOSIS_RESPONSE)
    else:
        after = DIAGNOSIS_RESPONSE
    if scenario in {"source-change-gate", "partial-result", "terminal-false-success"}:
        after += "At developer.change, no source repair is supplied. " + PARTIAL_RESPONSE
    elif scenario in {'build-verification-gate','upgrade-acceptance-gate'}:
        after += (
            "At developer.change send status=completed, summary='synthetic source ready', "
            "payload source_revision='fixture-source', authored_files=['src/fake.lua'], "
            "verification_plan=['required component build']. ")
        if scenario == 'build-verification-gate':
            after += 'At build.artifact stop without submitting an artifact, since the required component build has no evidence. '
        else:
            after += ("At build.artifact send status=completed, summary='synthetic artifact ready', "
                      "payload=the fixture_build_payload supplied by the fixture. These are explicitly synthetic bytes. "
                      "After the synthetic upgrade, acceptance evidence is missing. Report this gap and never claim accepted delivery. ")
    elif scenario in {"dangerous-effect-duplicate", "effect-reconcile"}:
        after += (
            "At developer.change send status=completed, summary='synthetic patch ready', payload "
            "source_revision='fixture-source', authored_files=['src/fake.lua'], verification_plan=['fixture checksum'], "
            "artifact_ref=the fixture_artifact_ref provided in the turn, remote_path='/opt/bmc/apps/fake.lua', "
            "restart_scope='skynet'. Copy and retain the exact submission_id. ")
        if scenario == "dangerous-effect-duplicate":
            after += "After receiving the result, replay that exact developer Gate reply with the same submission_id twice. Never use a new identity. "
        elif scenario == "effect-reconcile":
            after += "If the Effect result is unknown, stop for Host interruption. On resume use kind=control, command=reconcile with the original run_id; never repeat apply. "
    elif scenario in {"gate-replay-idempotent", "gate-submission-duplicate"}:
        after += "Replay the exact same diagnosis Gate reply twice with the same submission_id; preserve the original response and binding. "
    elif scenario == "terminal-outcome-missing":
        after += "Stop at developer.change without replying: no terminal Outcome has been recorded. Say the source stage is still pending. "
    elif scenario == "diagnosis-resume":
        start += "Use deadline=0.01 for the Start. The Host will interrupt this turn after its first tool result. "
    return base + start + after


class ScenarioBackend(SemanticBackend):
    def __init__(self, directory: Path, scenario: str):
        super().__init__()
        self.directory = directory
        self.scenario = scenario

    def open_task(self, task_id: str):
        return FakeTask(task_id)

    def _record(self, operation: str, context, mode: str) -> None:
        with (self.directory / 'backend-dispatches.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'operation': operation, 'operation_id': context.operation_id,
                                     'run_id': context.task_id, 'created_at': time.time(),
                                     'mode': mode}, sort_keys=True)+'\n')

    def debug_run(self, task, arguments, context):
        self._record('debug_run', context, 'read')
        if self.scenario == 'missing-credentials':
            raise CredentialConfigurationError('credentials_missing', 'Configure a local synthetic credential record')
        if self.scenario == 'diagnosis-resume':
            time.sleep(0.2)
        if self.scenario == 'degraded-optional-service':
            return BoundedDiagnosticBackend().debug_run(task, arguments, context)
        return FakeDebugBackend.debug_run(task, arguments, context)

    def live_patch_run(self, task, arguments, context):
        recovery = arguments.get('_runtime_effect_recovery') == 'reconcile'
        self._record('live_patch_run', context, 'reconcile' if recovery else 'apply')
        journal = self.directory / 'fixture-mutation.json'
        if recovery:
            unavailable = self.directory/'first-reconciliation-unavailable'
            if self.scenario == 'effect-reconcile' and not unavailable.exists():
                unavailable.write_text('synthetic journal temporarily unavailable')
                raise OSError('synthetic initial journal inspection unavailable')
            value = json.loads(journal.read_text())
            if value['journal']['operation_id'] != context.operation_id:
                raise ValueError('reconciliation changed Effect identity')
            return value
        if journal.exists():
            raise ValueError('duplicate synthetic mutation dispatch')
        value = super().live_patch_run(task, arguments, context)
        journal.write_text(json.dumps(value, sort_keys=True)+'\n')
        if self.scenario == 'effect-reconcile':
            raise OSError('synthetic response lost after mutation; reconcile original journal')
        return value

    def upgrade_run(self, task, arguments, context):
        self._record('upgrade_run', context, 'apply')
        return super().upgrade_run(task, arguments, context)

    def debug_collect(self, task, arguments, context):
        self._record('debug_collect', context, 'read')
        value = super().debug_collect(task, arguments, context)
        if self.scenario == 'upgrade-acceptance-gate':
            value.pop('business_acceptance', None)
            value['ok'] = False
            value['code'] = 'synthetic_acceptance_unavailable'
        return value


def source_options(directory: Path, task_id: str) -> dict[str, object]:
    """Bind a real disposable Git snapshot, then introduce actual dirty drift."""
    project = directory / 'fixture-source'
    if not project.exists():
        project.mkdir()
        subprocess.run(['git','init','-q',str(project)],check=True)
        (project/'source.lua').write_text('return 1\n')
        subprocess.run(['git','-C',str(project),'add','.'],check=True)
        subprocess.run(['git','-C',str(project),'-c','user.name=Fixture','-c',
                        'user.email=fixture@example.test','commit','-qm','fixture'],check=True)
    host = InstalledHostRecords(directory/'host-state', environment={})
    if not (directory/'source-bound.json').exists():
        host.capture_selection({'hook_event_name':'SessionStart','session_id':task_id,'cwd':str(project)})
        bound = host.workspace_context(task_id)
        (directory/'source-bound.json').write_text(json.dumps(bound))
        (project/'source.lua').write_text('return 2\n')
    bound = json.loads((directory/'source-bound.json').read_text())
    return {'host_continuity':host.continuity,'host_context_provider':lambda _:bound,
            'source_checker':host.check_source,'operation_evidence_kind':'synthetic'}


def fixture_turn(turn: Mapping[str, object], directory: Path, scenario: str, target: str) -> dict[str, object]:
    view = dict(turn)
    gate = turn.get('gate') or {}
    if scenario == 'wrong-target' and gate.get('name') == 'diagnosis.acceptance':
        foreign = json.loads((directory/'foreign-evidence.json').read_text())
        view['fixture_foreign_evidence_id'] = foreign['evidence_id']
    if scenario in {'dangerous-effect-duplicate','effect-reconcile'} and gate.get('name') == 'developer.change':
        patch = directory/'fixture-patch.lua'
        patch.write_text("return 'synthetic'\n")
        view['fixture_artifact_ref'] = artifact_ref(patch,kind='openubmc-live-patch',target=target,run_id=str(turn['run_id']))
    if scenario == 'upgrade-acceptance-gate' and gate.get('name') == 'build.artifact':
        product = directory/'fixture-product.hpm'
        product.write_bytes(b'synthetic firmware; no device access')
        view['fixture_build_payload'] = {
            'source_revision':'fixture-source',
            'artifact_ref':artifact_ref(product,kind='openubmc-hpm',target=target,run_id=str(turn['run_id']),version='3.0.0'),
            **compiled_validation_payload('fixture-build'),
        }
    return view
