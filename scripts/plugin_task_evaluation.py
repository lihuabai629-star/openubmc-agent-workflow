#!/usr/bin/env python3
"""Export immutable plugin candidates and audit paired Evaluation Lab task evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

if __package__:
    from .plugin_archive import read_archive
else:
    from plugin_archive import read_archive

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation/plugin-tasks"


def digest(document):
    unsigned = {
        key: value
        for key, value in document.items()
        if key not in {"digest", "evidence_digest", "bundle_digest"}
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                unsigned, sort_keys=True, ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def prepare(archive, sha256, output):
    """Create a distinct Lab subject from verified bytes, without installing it."""
    lock, files = read_archive(archive, sha256)
    if output.exists():
        raise ValueError("candidate destination already exists")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    subject = {
        "schema": "openubmc-agent-workflow.evaluation-subject.v1",
        "kind": "codex-plugin-candidate",
        "published": False,
        "plugin": {
            "name": lock["name"],
            "version": lock["version"],
            "marketplace": "openubmc-evaluation",
        },
        "distribution": {
            "repository": "lihuabai629-star/openubmc-agent-workflow",
            "commit": lock["source_commit"],
            "archive": {
                "name": archive.name,
                "url": archive.resolve().as_uri(),
                "sha256": sha256,
                "size_bytes": archive.stat().st_size,
            },
        },
        "payload": {
            "source_commit": lock["source_commit"],
            "content_digest": lock["content_digest"],
            "files": len(lock["files"]),
            "manifest_sha256": hashes[".codex-plugin/plugin.json"],
            "plugin_lock_sha256": hashes["plugin-lock.json"],
            "mcp_config_sha256": hashes[".mcp.json"],
            "mcp_servers": sorted(json.loads(files[".mcp.json"])["mcpServers"]),
            "skill_files": {
                name: sha
                for name, sha in hashes.items()
                if name.startswith("skills/") and name.endswith("/SKILL.md")
            },
            "workflow_skills": lock["skills"],
            "runtime_files_digest": digest(
                {
                    name: sha
                    for name, sha in hashes.items()
                    if name.startswith(
                        "skills/openubmc-target-runtime/openubmc_target_runtime/"
                    )
                }
            ),
        },
    }
    subject["digest"] = digest(subject)
    output.mkdir(parents=True)
    write(output / "subject.json", subject)
    shutil.copytree(DATASET, output / "dataset")
    qualification = {
        "schema": "openubmc.plugin-task-qualification.v1",
        "status": "unverified",
        "subject_digest": subject["digest"],
        "archive_verified": True,
        "native_plugin_loaded": False,
        "evidence": {
            "paired_agent_tasks": "unverified",
            "bmc": "unverified",
            "conan": "unverified",
            "kb": "unverified",
            "native_windows": "unverified",
            "cross_wsl": "unverified",
        },
    }
    write(output / "qualification.json", qualification)
    return qualification


TASK_METRICS = (
    "wall_seconds",
    "cost_usd",
    "extra_tool_calls",
    "human_interventions",
    "recovery_attempts",
)


def valid_metric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def valid_digest(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    )


def metric_summary(rows, *, expected=None):
    rows = list(rows)
    count = len(rows) if expected is None else expected
    result = {}
    for name in TASK_METRICS:
        values = [row.get("metrics", {}).get(name) for row in rows]
        valid = [value for value in values if valid_metric(value)]
        complete = count > 0 and len(valid) == count
        result[name] = {
            "measured": len(valid),
            "expected": count,
            "complete": complete,
            "mean": sum(valid) / len(valid) if complete else None,
        }
    return result


def summarize_tasks(cases, arms, *, repetitions):
    """Summarize validated task observations; absent evidence never implies success."""
    if (
        type(repetitions) is not int
        or repetitions < 1
        or set(arms) != {"baseline", "candidate"}
    ):
        raise ValueError("select two arms and positive repetitions")
    baseline_identity = arms["baseline"].get("identity", {})
    candidate_identity = arms["candidate"].get("identity", {})
    if baseline_identity.get("subject_digest") and baseline_identity.get(
        "subject_digest"
    ) == candidate_identity.get("subject_digest"):
        raise ValueError("paired tasks require distinct plugin subjects")
    if baseline_identity.get("archive_sha256") and baseline_identity.get(
        "archive_sha256"
    ) == candidate_identity.get("archive_sha256"):
        raise ValueError("paired tasks require distinct plugin archives")
    expected = {
        (case["case_id"], repeat)
        for case in cases
        for repeat in range(1, repetitions + 1)
    }
    if not expected or len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("select distinct task cases")
    case_index = {case["case_id"]: case for case in cases}
    actual_inputs = {}
    for arm, data in arms.items():
        for row in data["episodes"]:
            key = (row["case_id"], row["repetition"])
            value = row.get("pairing_identity")
            if key in actual_inputs:
                previous = actual_inputs[key]
                left, right = dict(previous or {}), dict(value or {})
                if not valid_digest(left.get("target_input_digest")) or not valid_digest(
                    right.get("target_input_digest")
                ):
                    left.pop("target_input_digest", None)
                    right.pop("target_input_digest", None)
                if not left.get("model_configuration") or not right.get(
                    "model_configuration"
                ):
                    left.pop("model_configuration", None)
                    right.pop("model_configuration", None)
                if left != right:
                    raise ValueError(
                        "paired samples received different actual task inputs"
                    )
            actual_inputs[key] = value
    reports, successes, gaps = {}, {}, []
    failed = False
    for arm, data in arms.items():
        rows = {}
        for episode in data["episodes"]:
            key = (episode["case_id"], episode["repetition"])
            if key not in expected or key in rows:
                raise ValueError("unexpected or duplicate task sample")
            rows[key] = episode
        score_index = {}
        for score in data["scores"]:
            key = (score["episode_id"], score["scorer_id"])
            if key in score_index:
                raise ValueError("duplicate task predicate")
            score_index[key] = score
        confirmed, arm_gaps, task_results = {}, [], []
        for key in sorted(expected):
            row = rows.get(key)
            if row is None:
                arm_gaps.append(
                    {"case": key[0], "repetition": key[1], "missing": ["sample"]}
                )
                continue
            oracle = case_index[key[0]]["oracle"]
            names = oracle["required_predicates"] + oracle["forbidden_predicates"]
            predicates = [
                score_index.get((row["episode_id"], "plugin-task." + name))
                for name in names
            ]
            absent = [name for name, score in zip(names, predicates) if score is None]
            if not valid_digest(
                (row.get("pairing_identity") or {}).get("target_input_digest")
            ):
                absent.append("target_input_digest")
            if row.get("identity_verified") is False:
                absent.append("loaded_agent_identity")
            completion = row.get("task_completion", {}).get("status", "unverified")
            runtime = row.get("runtime_completion", {"status": "unverified"})
            if completion == "unverified":
                absent.append("task_completion")
            if runtime["status"] == "unverified":
                absent.append("runtime_outcome")
            metrics = row.get("metrics", {})
            missing_metrics = [
                name for name in TASK_METRICS if not valid_metric(metrics.get(name))
            ]
            if absent or missing_metrics:
                arm_gaps.append(
                    {
                        "case": key[0],
                        "repetition": key[1],
                        "missing": absent + missing_metrics,
                    }
                )
            outcome = (
                row.get("status") == "completed"
                and row.get("strict_success") is True
                and row.get("hard_failure") is False
                and row.get("gate_eligible", True) is True
                and completion == "completed"
                and runtime["status"] in {"completed", "not-applicable"}
            )
            if (
                outcome
                and not absent
                and all(score.get("passed") is True for score in predicates)
            ):
                confirmed[key] = row
            elif not absent:
                failed = True
            task_results.append(
                {
                    "case": key[0],
                    "repetition": key[1],
                    "episode_id": row["episode_id"],
                    "status": "completed" if key in confirmed else (
                        "failed" if completion == "failed" or runtime["status"] == "failed"
                        else "unverified" if absent else "failed"
                    ),
                    "review_completion": completion,
                    "runtime_completion": runtime,
                }
            )
        metrics_report = metric_summary(confirmed.values())
        layers = {}
        for key, row in rows.items():
            if key not in confirmed:
                layer = row.get("failure_layer") or (
                    "runtime" if row.get("runtime_completion", {}).get("status") == "failed"
                    else "unclassified"
                )
                layers[layer] = layers.get(layer, 0) + 1
        all_metrics = metric_summary(rows.values(), expected=len(expected))
        reports[arm] = {
            "expected_tasks": len(expected),
            "observed_tasks": len(rows),
            "task_successes": len(confirmed),
            "task_success_rate": len(confirmed) / len(expected),
            "successful_tasks": metrics_report,
            "all_tasks": all_metrics,
            "failure_layers": layers,
            "gaps": arm_gaps,
            "task_results": task_results,
        }
        successes[arm] = confirmed
        gaps.extend(arm_gaps)
    paired = {}
    common = set(successes["baseline"]) & set(successes["candidate"])
    for name in TASK_METRICS:
        differences = []
        for key in common:
            values = [
                successes[arm][key].get("metrics", {}).get(name)
                for arm in ("baseline", "candidate")
            ]
            if all(valid_metric(value) for value in values):
                differences.append(values[1] - values[0])
        complete = bool(common) and len(differences) == len(common)
        paired[name] = {
            "pairs": len(differences),
            "expected_success_pairs": len(common),
            "candidate_minus_baseline": sum(differences) / len(differences)
            if complete
            else None,
        }
    return {
        "schema": "openubmc.plugin-task-comparison.v1",
        "status": "unverified" if gaps else "failed" if failed else "passed",
        "arms": reports,
        "paired_success": paired,
    }


def reviewed_task_scores(root, review, episodes, cases, *, bundle_digest):
    """Accept explicit independent task judgments bound to unchanged evidence."""
    if review.get("schema") != "openubmc.plugin-task-review.v1" or review.get(
        "digest"
    ) != digest(review):
        raise ValueError("task review identity is invalid")
    if (
        review.get("bundle_digest") != bundle_digest
        or not review.get("reviewer")
        or review.get("method") != "independent-task-review"
    ):
        raise ValueError(
            "task review must identify its bundle and independent reviewer"
        )
    index = {row["episode_id"]: row for row in episodes}
    oracles = {case["case_id"]: case["oracle"] for case in cases}
    scores, metrics, seen = [], {}, set()
    for sample in review["samples"]:
        identifier = sample["episode_id"]
        if (
            identifier in seen
            or identifier not in index
            or sample.get("source_digest") != index[identifier]["source"]["digest"]
        ):
            raise ValueError(
                "task review sample is duplicated or bound to another source"
            )
        seen.add(identifier)
        source = root / sample["source_path"]
        if (
            not source.resolve().is_relative_to(root.resolve())
            or (
                index[identifier].get("verified_source_path") is not None
                and sample["source_path"] != index[identifier]["verified_source_path"]
            )
            or source.is_symlink()
            or not source.is_file()
            or "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            != sample["source_digest"]
        ):
            raise ValueError("task source evidence identity mismatch")
        if not sample.get("evidence_refs"):
            raise ValueError("task review evidence is missing")
        for reference in sample["evidence_refs"]:
            path = root / reference["path"]
            if (
                not path.resolve().is_relative_to(source.parent.resolve())
                or path.is_symlink()
                or not path.is_file()
            ):
                raise ValueError("task review evidence path is invalid")
            if hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256"):
                raise ValueError("task review evidence digest mismatch")
        oracle = oracles[index[identifier]["case_id"]]
        completion = sample.get("completion")
        if completion is not None:
            if (
                not isinstance(completion, dict)
                or completion.get("status") not in {"completed", "failed", "unverified"}
                or not isinstance(completion.get("criterion"), str)
                or not completion["criterion"].strip()
                or not completion.get("evidence_refs")
            ):
                raise ValueError("task completion requires a status, criterion and evidence")
            # Only the original Agent records indexed by the verified source may
            # support completion; a new reviewer-authored file is not evidence.
            for reference in completion["evidence_refs"]:
                if reference not in index[identifier].get("task_evidence_refs", []):
                    raise ValueError("task completion evidence is not the verified episode record")
            index[identifier]["task_completion"] = completion
        allowed = set(oracle["required_predicates"] + oracle["forbidden_predicates"])
        for name, passed in sample["predicates"].items():
            if name not in allowed or type(passed) is not bool:
                raise ValueError("task predicate is invalid")
            scores.append(
                {
                    "episode_id": identifier,
                    "scorer_id": "plugin-task." + name,
                    "passed": passed,
                }
            )
        observations = sample.get("metrics", {})
        if any(
            name not in TASK_METRICS or not valid_metric(value)
            for name, value in observations.items()
        ):
            raise ValueError("task review metric is invalid")
        layer = sample.get("failure_layer")
        if layer is not None and layer not in {
            "environment",
            "model",
            "tools",
            "runtime",
            "unclassified",
        }:
            raise ValueError("task failure layer is invalid")
        index[identifier]["failure_layer"] = layer
        metrics[identifier] = observations
    return scores, metrics


def runtime_task_completion(records):
    """Read Runtime-returned Outcomes; independent reviews cannot replace them."""
    runs, terminal, pending = {}, {}, set()
    started, transport_errors = {}, {}
    unresolved = False
    for position, record in enumerate(records):
        if record.get("event_type") != "codex.event":
            continue
        event = record.get("payload", {})
        item = event.get("item", {})
        if (
            item.get("type") != "mcp_tool_call"
            or item.get("server") != "openubmc-target-runtime"
            or item.get("tool") != "execute"
        ):
            continue
        if event.get("type") == "item.started":
            pending.add(item.get("id"))
            started[item.get("id")] = (position, item.get("arguments"))
            continue
        if event.get("type") != "item.completed":
            continue
        pending.discard(item.get("id"))
        arguments = item.get("arguments") or {}
        if started.get(item.get("id"), (None, arguments))[1] != arguments:
            unresolved = True
            continue
        requested_run = arguments.get("run_id") if isinstance(arguments, dict) else None
        if requested_run is not None and not isinstance(requested_run, str):
            unresolved = True
            requested_run = None
        if requested_run:
            runs.setdefault(requested_run, {"run_id": requested_run, "status": "unverified"})
        result = item.get("result") or {}
        turn = result.get("structured_content") if isinstance(result, dict) else None
        if (
            item.get("status") != "completed"
            or not isinstance(turn, dict)
            or turn.get("schema") != "openubmc.target-runtime.v1/agent-gateway-v1/turn"
        ):
            if (
                item.get("status") == "failed"
                and item.get("error")
                and item.get("result") is None
                and requested_run
                and arguments.get("kind") in {"resume", "respond", "control"}
            ):
                # A failed transport has no Outcome. Only a new call on this
                # same Run can establish what happened after this failure.
                transport_errors[requested_run] = position
                runs[requested_run] = {"run_id": requested_run, "status": "unverified"}
            else:
                unresolved = True
            continue
        run_id = turn.get("run_id")
        if not isinstance(run_id, str):
            unresolved = True
            continue
        if not run_id:
            # A rejected request that created no Run has no terminal Outcome.
            # It does not erase a later valid Run's successful completion.
            if (
                requested_run
                or turn.get("state") != "failed"
                or turn.get("outcome") is not None
                or turn.get("outcome_recorded") is True
            ):
                unresolved = True
            continue
        if requested_run and requested_run != run_id:
            unresolved = True
        outcome = turn.get("outcome")
        if outcome is None:
            outcome = {}
        if not isinstance(outcome, dict):
            runs[run_id] = {"run_id": run_id, "status": "unverified"}
            unresolved = True
            continue
        status = "unverified"
        if turn.get("outcome_recorded") is True:
            outcome_status = outcome.get("status")
            if outcome_status == "completed" and turn.get("state") == "completed":
                status = "completed"
            elif outcome_status in {"failed", "cancelled"}:
                status = "failed"
            if run_id in terminal and terminal[run_id] != outcome:
                unresolved = True
            terminal[run_id] = outcome
        if status in {"completed", "failed"} and run_id in transport_errors:
            call_started, start_arguments = started.get(item.get("id"), (-1, None))
            if (
                call_started > transport_errors[run_id]
                and start_arguments == arguments
                and requested_run == run_id
                and arguments.get("kind") in {"resume", "respond", "control"}
            ):
                del transport_errors[run_id]
        runs[run_id] = {
            "run_id": run_id,
            "status": status,
            "state": turn.get("state"),
            "outcome_recorded": turn.get("outcome_recorded") is True,
            "outcome_status": outcome.get("status"),
        }
    statuses = {run["status"] for run in runs.values()}
    status = (
        "unverified" if unresolved or pending or transport_errors or "unverified" in statuses else
        "failed" if "failed" in statuses else
        "completed" if runs else "not-applicable"
    )
    return {"status": status, "runs": list(runs.values())}


def validate_task_identity(
    source, records, *, subject_digest, runtime_digest, client, model
):
    """Compare actual harness preparation evidence with the pinned experiment."""
    if source.get("adapter_kind") != "codex":
        raise ValueError("task evidence must come from the Codex Agent harness")
    prepared = [row for row in records if row.get("event_type") == "harness.prepared"]
    if not prepared:
        return False
    if len(prepared) != 1:
        raise ValueError("task evidence has ambiguous harness identity")
    payload = prepared[0]["payload"]
    identity = payload.get("identity", {})
    plugin = identity.get("plugin_runtime", {})
    if (
        payload.get("adapter_kind") != "codex"
        or plugin.get("subject_digest") != subject_digest
        or plugin.get("runtime_digest") != runtime_digest
        or identity.get("executable", {}).get("digest") != "sha256:" + client
        or identity.get("model_configuration", {}).get("model") != model
    ):
        raise ValueError("actual Agent identity differs from the pinned task identity")
    return dict(identity["model_configuration"])


def load_lab_arm(bundle, subject_path, review_path):
    # Import from the explicitly selected, clean Evaluation Lab, never replace its
    # historical subject or bypass its source-evidence recomputation.
    from evaluation.contracts import (
        load_eval_set,
        load_experiment,
        load_json,
        load_jsonl,
    )
    from evaluation.lab import EvaluationLab
    from evaluation.plugin_subject import PluginSubject

    checked = EvaluationLab.verify(bundle)
    if not checked.verified:
        raise ValueError("Evaluation Lab rejected the evidence bundle")
    dataset = load_eval_set(bundle / "dataset")
    selected = load_eval_set(DATASET)
    if dataset.digest != selected.digest:
        raise ValueError("bundle task dataset differs from the selected task suite")
    experiment = load_experiment(bundle / "experiment.json", eval_set=dataset)
    arm_id = experiment.candidate_arm_id
    if set(experiment.arm_case_ids[arm_id]) != set(dataset.cases):
        raise ValueError("bundle omits planned task cases")
    environment = dict(experiment.arm_environments[arm_id])
    subject = PluginSubject.load(subject_path)
    bound_subject = environment.pop("plugin_subject", {})
    runtime = environment.pop("plugin_runtime", {})
    if bound_subject != dict(subject.document) or bound_subject.get("digest") != digest(
        bound_subject
    ):
        raise ValueError(
            "bundle plugin subject differs from the selected immutable subject"
        )
    if (
        runtime.get("digest") != digest(runtime)
        or runtime.get("subject_digest") != subject.digest
        or runtime.get("native_plugin_loaded") is not True
    ):
        raise ValueError("bundle has no verified native plugin Runtime identity")
    client = runtime.get("codex_executable_sha256")
    if not isinstance(client, str) or len(client) != 64:
        raise ValueError("bundle client identity is missing")
    manifest = load_json(bundle / "manifest.json", description="bundle")
    episodes = [
        row
        for row in load_jsonl(bundle / "runs/all-runs.jsonl", description="episodes")
        if row["arm_id"] == arm_id
    ]
    source_paths = {}
    for reference in manifest["sources"]:
        path = bundle / reference["path"]
        source_paths["sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()] = path
    model_configurations = set()
    for row in episodes:
        path = source_paths.get(row["source"]["digest"])
        if path is None:
            raise ValueError("task source is not indexed by the verified bundle")
        source = load_json(path, description="task source")
        row["verified_source_path"] = path.relative_to(bundle).as_posix()
        row["pairing_identity"] = {
            name: source.get(name)
            for name in (
                "target_input_digest",
                "case_prompt_digest",
                "case_budgets_digest",
            )
        }
        records_path = path.parent / source["raw_records"]
        if not records_path.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError("task raw records escape their verified source")
        records = load_jsonl(records_path, description="task raw records")
        row["task_evidence_refs"] = [{
            "path": records_path.relative_to(bundle).as_posix(),
            "sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        }]
        row["runtime_completion"] = runtime_task_completion(records)
        model_configuration = validate_task_identity(
            source,
            records,
            subject_digest=subject.digest,
            runtime_digest=runtime["digest"],
            client=client,
            model=experiment.arms[arm_id]["model"],
        )
        row["identity_verified"] = bool(model_configuration)
        if model_configuration:
            row["pairing_identity"]["model_configuration"] = model_configuration
            model_configurations.add(digest(model_configuration))
        # Wall time is measured by the harness even when usage omits it.
        elapsed = source.get("budget", {}).get("elapsed_seconds")
        if valid_metric(elapsed):
            row["metrics"]["wall_seconds"] = elapsed
    if len(model_configurations) > 1:
        raise ValueError("actual model configuration changed within one task arm")
    model_configuration_digest = next(iter(model_configurations), None)
    cases = [case.document for case in dataset.cases.values()]
    scores = []
    if review_path:
        review = load_json(review_path, description="independent task review")
        scores, measured = reviewed_task_scores(
            bundle, review, episodes, cases, bundle_digest=manifest["bundle_digest"]
        )
        for row in episodes:
            # A review can supply absent observations, but cannot rewrite
            # measurements already recorded by the actual Agent harness.
            for name, value in measured.get(row["episode_id"], {}).items():
                if name in row["metrics"] and row["metrics"][name] != value:
                    raise ValueError("review contradicts a measured task metric")
                row["metrics"][name] = value
    arm = experiment.arms[arm_id]
    controls = {
        key: arm[key]
        for key in (
            "model",
            "prompt_digest",
            "target_snapshot_digest",
            "budgets_digest",
            "tool_contract_digest",
        )
    }
    controls["environment_digest"] = digest(environment)
    controls["client_sha256"] = client
    identity = {
        "subject_digest": subject.digest,
        "source_commit": subject.payload["source_commit"],
        "archive_sha256": subject.archive_sha256,
        "content_digest": subject.payload["content_digest"],
        "runtime_digest": runtime["digest"],
        "client_sha256": client,
        "model": arm["model"],
        "model_configuration_digest": model_configuration_digest,
        "environment_digest": digest(environment),
        "bundle_digest": manifest["bundle_digest"],
        "review_digest": review["digest"] if review_path else None,
    }
    return {
        "episodes": episodes,
        "scores": scores,
        "controls": controls,
        "identity": identity,
        "cases": cases,
        "repetitions": experiment.repetitions,
    }


def compare_bundles(args):
    lab = args.lab.resolve()
    if subprocess.check_output(
        ["git", "-C", str(lab), "status", "--porcelain"], text=True
    ).strip():
        raise ValueError(
            "select a clean Evaluation Lab checkout to pin evaluator identity"
        )
    lab_commit = subprocess.check_output(
        ["git", "-C", str(lab), "rev-parse", "HEAD"], text=True
    ).strip()
    sys.path.insert(0, str(lab))
    arms = {
        name: load_lab_arm(
            getattr(args, name + "_bundle"),
            getattr(args, name + "_subject"),
            getattr(args, name + "_review"),
        )
        for name in ("baseline", "candidate")
    }
    if (
        arms["baseline"]["controls"] != arms["candidate"]["controls"]
        or arms["baseline"]["repetitions"] != arms["candidate"]["repetitions"]
    ):
        raise ValueError(
            "paired tasks require matching model, client, environment, target, prompts and budgets"
        )
    report = summarize_tasks(
        arms["candidate"]["cases"], arms, repetitions=arms["candidate"]["repetitions"]
    )
    report["evaluation_lab_commit"] = lab_commit
    report["identities"] = {name: arm["identity"] for name, arm in arms.items()}
    report["scope"] = (
        "selected paired Agent tasks; no platform or hardware qualification beyond these samples"
    )
    write(args.output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("prepare")
    export.add_argument("--archive", type=Path, required=True)
    export.add_argument("--sha256", required=True)
    export.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("report")
    compare.add_argument("--lab", type=Path, required=True)
    for arm in ("baseline", "candidate"):
        compare.add_argument("--" + arm + "-bundle", type=Path, required=True)
        compare.add_argument("--" + arm + "-subject", type=Path, required=True)
        compare.add_argument("--" + arm + "-review", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = (
            prepare(args.archive, args.sha256, args.output)
            if args.command == "prepare"
            else compare_bundles(args)
        )
    except (
        ValueError,
        OSError,
        KeyError,
        ImportError,
        subprocess.SubprocessError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if args.command == "prepare" or report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
