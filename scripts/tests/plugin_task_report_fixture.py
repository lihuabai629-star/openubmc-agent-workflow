"""Public Lab/report integration fixtures; these are not business qualification.

Only external Codex process responses are controlled. Subject staging, native
inventory validation, Harness lifecycle, Bundle construction, and verification
use the explicitly selected clean Evaluation Lab's public APIs unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TURN_SCHEMA = "openubmc.target-runtime.v1/agent-gateway-v1/turn"


def runtime_turn(status, *, run_id="run-fixture", recorded=True):
    """A controlled Runtime MCP response, never an actual Runtime assertion."""
    return {
        "schema": TURN_SCHEMA,
        "run_id": run_id,
        "state": status,
        "outcome_recorded": recorded,
        "outcome": {"status": status, "summary": "Controlled report test"}
        if recorded
        else None,
    }


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive(root, arm):
    """Make a small, complete plugin archive accepted by public prepare/stage."""
    files = {
        ".codex-plugin/plugin.json": _json_bytes(
            {"name": "openubmc", "version": "0.0.0-fixture", "skills": "./skills/"}
        ),
        ".mcp.json": _json_bytes(
            {"mcpServers": {"openubmc-target-runtime": {}}}
        ),
        "skills/report-fixture/SKILL.md": (
            "---\nname: report-fixture\n---\nControlled " + arm + " fixture.\n"
        ).encode(),
    }
    lock = {
        "schema": "openubmc.codex-plugin.v1",
        "name": "openubmc",
        "version": "0.0.0-fixture",
        "source_commit": ("a" if arm == "baseline" else "b") * 40,
        "manifest_digest": hashlib.sha256(files[".codex-plugin/plugin.json"]).hexdigest(),
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        "skills": ["report-fixture"],
    }
    lock["content_digest"] = hashlib.sha256(_json_bytes(lock)).hexdigest()
    files["plugin-lock.json"] = _json_bytes(lock)
    archive = root / "plugin.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for name, value in files.items():
            entry = tarfile.TarInfo("openubmc/" + name)
            entry.size = len(value)
            target.addfile(entry, io.BytesIO(value))
    return archive


def _selected_lab():
    selected = os.environ.get("OPENUBMC_EVALUATION_LAB")
    if not selected:
        raise unittest.SkipTest("set OPENUBMC_EVALUATION_LAB to a clean Lab checkout")
    lab = Path(selected).resolve()
    state = subprocess.check_output(
        ["git", "-C", str(lab), "status", "--porcelain"], text=True
    )
    if state.strip():
        raise ValueError("report fixture requires a clean Evaluation Lab checkout")
    sys.path.insert(0, str(lab))
    import evaluation.contracts

    if not Path(evaluation.contracts.__file__).resolve().is_relative_to(lab):
        raise ValueError("another Evaluation Lab is already imported")
    return lab


def build_report_fixture(root, *, samples=None, arm_samples=None, extra_events=None):
    """Return public report argv, Bundle paths, and editable independent reviews.

    ``samples`` maps selected case IDs to lists of controlled MCP Turn documents.
    Every Experiment plans all nine cases; only selected cases are observed.
    ``arm_samples`` may override that mapping separately for baseline/candidate.
    ``extra_events`` appends controlled Codex events for each selected case.
    For example ``samples={"skill-positive": [runtime_turn("failed")]}`` yields
    a completed Agent episode with a failed Runtime and all task predicates true.
    Call ``save_review(arm)`` after changing an arm's ``review_document``.
    """
    lab_root = _selected_lab()
    from evaluation.codex_harness import CodexHarnessAdapter
    from evaluation.contracts import EvidenceInput, digest_value, load_eval_set
    from evaluation.evidence import HARNESS_EVIDENCE_KIND
    from evaluation.experiment_documents import WorkflowArmBinding, workflow_experiment_document
    from evaluation.harness import HarnessEpisodeRunner
    from evaluation.lab import EvaluationLab
    from evaluation.plugin_subject import PluginSubject, prepare_isolated_plugin_runtime
    from evaluation.runner import CommandResult

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
         "commit", "--allow-empty", "-qm", "Report fixture"],
        cwd=workspace, check=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip()
    executable = root / "codex-fixture"
    executable.write_text("#!/bin/sh\nexit 99\n")
    executable.chmod(0o755)
    dataset_root = ROOT / "evaluation/plugin-tasks"
    dataset = load_eval_set(dataset_root)
    case_ids = tuple(dataset.cases)
    controls = {
        "prompt_digest": digest_value([dataset.cases[key].document["prompt"] for key in case_ids]),
        "skills_digest": digest_value("controlled plugin Skills"),
        "target_snapshot_digest": digest_value({}),
        "budgets_digest": digest_value([dataset.cases[key].document["budgets"] for key in case_ids]),
        "tool_contract_digest": digest_value("controlled Runtime responses"),
    }
    if samples is None:
        samples = {"skill-positive": [runtime_turn("failed")]}
    arms = {}
    for name in ("baseline", "candidate"):
        selected = (arm_samples or {}).get(name, samples)
        arm_root = root / name
        arm_root.mkdir()
        archive = _archive(arm_root, name)
        prepared = arm_root / "prepared"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/plugin_task_evaluation.py"), "prepare",
             "--archive", str(archive), "--sha256", _sha(archive), "--output", str(prepared)],
            check=True, capture_output=True, text=True,
        )
        subject_path = prepared / "subject.json"
        subject = PluginSubject.load(subject_path)
        native_root = arm_root / "native"
        cache = native_root / "codex-home/plugins/cache/fixture"

        def native_execute(command, *, cwd, environment):
            if tuple(command[1:4]) == ("plugin", "marketplace", "add"):
                value = {"marketplaceName": subject.plugin["marketplace"]}
            elif tuple(command[1:3]) == ("plugin", "add"):
                shutil.copytree(native_root / "marketplace/plugins/openubmc", cache)
                value = {"pluginId": subject.plugin_id, "version": subject.version,
                         "installedPath": str(cache)}
            elif tuple(command[1:3]) == ("plugin", "list"):
                value = {"installed": [{"pluginId": subject.plugin_id,
                         "version": subject.version, "installed": True, "enabled": True}]}
            elif tuple(command[1:3]) == ("mcp", "list"):
                value = [{"name": "openubmc-target-runtime", "enabled": True,
                          "transport": {"cwd": str(cache)}}]
            else:
                raise AssertionError("unexpected native process request")
            return CommandResult(tuple(command), 0, json.dumps(value), "")

        def skills_probe(**kwargs):
            return [{"name": "report-fixture", "enabled": True,
                     "pluginId": subject.plugin_id,
                     "path": str(cache / "skills/report-fixture/SKILL.md")}]

        runtime = prepare_isolated_plugin_runtime(
            subject=subject, archive=archive, destination=native_root,
            codex_executable=executable, workspace=workspace,
            executor=native_execute, skills_probe=skills_probe,
        )
        experiment_path = arm_root / "experiment.json"
        _write(experiment_path, workflow_experiment_document(
            experiment_id="report-fixture-" + name,
            hypothesis="Report completion contract integration",
            eval_set_id=dataset.eval_set_id, eval_set_digest=dataset.digest,
            case_ids=case_ids,
            candidate=WorkflowArmBinding(commit, digest_value("fixture-lock"), "fixture"),
            harness="codex", model="fixture-model", controls=controls,
            seed=7, repetitions=1, runner_image="report-fixture",
            gate_policy={"hard": {"fail_on_any": True},
                         "quality": {"minimum_success_rate": 1.0,
                                     "require_paired_comparison": False},
                         "efficiency": {"required": False, "metrics": {}}},
            environment={"plugin_subject": dict(subject.document),
                         "plugin_runtime": dict(runtime.record)},
        ))
        lab = EvaluationLab.load(dataset_root=dataset_root, experiment_path=experiment_path)
        evidence = []
        for case_id, turns in selected.items():
            events = [{"type": "thread.started", "thread_id": "fixture-thread"},
                      {"type": "turn.started"}]
            for index, turn in enumerate(turns):
                item = {"id": "tool-" + str(index), "type": "mcp_tool_call",
                        "server": "openubmc-target-runtime", "tool": "execute",
                        "arguments": {"kind": "resume", "run_id": turn.get("run_id", "")}}
                events.append({"type": "item.started", "item": dict(item, status="in_progress")})
                events.append({"type": "item.completed", "item": dict(
                    item, status="completed", result={"structured_content": turn})})
            events.extend((extra_events or {}).get(case_id, ()))
            events.extend([
                {"type": "item.completed", "item": {"id": "answer", "type": "agent_message",
                 "text": "Controlled fixture answer; no business work was performed."}},
                {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])

            def codex_execute(command, *, cwd, stdin=""):
                if command[0] == "git":
                    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
                    return CommandResult(tuple(command), result.returncode, result.stdout, result.stderr)
                if command[1] == "--version":
                    output = "codex-fixture 1.0\n"
                elif command[1] == "exec":
                    output = "\n".join(json.dumps(event) for event in events) + "\n"
                else:
                    raise AssertionError("unexpected Codex process request")
                return CommandResult(tuple(command), 0, output, "")

            adapter = CodexHarnessAdapter(workspace=workspace, executable=executable,
                                         executor=codex_execute, plugin_runtime=runtime)
            path = HarnessEpisodeRunner(lab).run(
                adapter=adapter, case_id=case_id, repetition=1, target={},
                output=arm_root / "episodes" / case_id,
            )
            evidence.append(EvidenceInput(HARNESS_EVIDENCE_KIND, path))
        bundle = arm_root / "bundle"
        lab.build(evidence=tuple(evidence), output=bundle)
        verified = EvaluationLab.verify(bundle)
        if not verified.verified:
            raise AssertionError(verified.errors)
        manifest = json.loads((bundle / "manifest.json").read_text())
        sources = {_sha(bundle / item["path"]): item["path"] for item in manifest["sources"]}
        reviews = []
        for line in (bundle / "runs/all-runs.jsonl").read_text().splitlines():
            row = json.loads(line)
            source_path = sources[row["source"]["digest"].removeprefix("sha256:")]
            source = json.loads((bundle / source_path).read_text())
            raw = Path(source_path).parent / source["raw_records"]
            refs = [{"path": raw.as_posix(), "sha256": _sha(bundle / raw)}]
            oracle = dataset.cases[row["case_id"]].document["oracle"]
            reviews.append({
                "episode_id": row["episode_id"], "source_digest": row["source"]["digest"],
                "source_path": source_path,
                "predicates": {key: True for key in oracle["required_predicates"] + oracle["forbidden_predicates"]},
                "metrics": {"cost_usd": 0, "extra_tool_calls": 0,
                            "human_interventions": 0, "recovery_attempts": 0},
                "evidence_refs": refs,
                "completion": {"status": "completed",
                               "criterion": "Controlled full-task completion assertion",
                               "evidence_refs": refs},
            })
        arm = {"root": arm_root, "bundle": bundle, "subject": subject_path,
               "review": arm_root / "review.json", "review_document": {
                   "schema": "openubmc.plugin-task-review.v1",
                   "bundle_digest": manifest["bundle_digest"], "reviewer": "independent fixture reviewer",
                   "method": "independent-task-review", "samples": reviews}}
        save_review(arm)
        arms[name] = arm
    output = root / "comparison.json"
    argv = [sys.executable, str(ROOT / "scripts/plugin_task_evaluation.py"), "report",
            "--lab", str(lab_root), "--output", str(output)]
    for name, arm in arms.items():
        for field in ("bundle", "subject", "review"):
            argv.extend(["--" + name + "-" + field, str(arm[field])])
    return {"argv": argv, "arms": arms, "output": output, "lab": lab_root}


def save_review(arm):
    """Write an edited independent review using the public Lab digest contract."""
    from evaluation.contracts import digest_document

    document = arm["review_document"]
    document["digest"] = digest_document(document)
    _write(arm["review"], document)
