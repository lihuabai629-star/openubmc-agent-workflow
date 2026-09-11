"""Decision support through the public, read-only diagnostic helper CLI."""
from __future__ import annotations

import copy
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "openubmc-debug/scripts/diagnostic_advice.py"
TIME = "2026-09-08T01:00:00+00:00"
DEVICE = {"Name": "Drive0", "Protocol": "NVMe"}
TARGET = "192.0.2.20"


def captured(target=TARGET, *, hardware=None, mdb=False, northbound=None):
    stages = {key: value for key, value in {
        "hardware_discovery": hardware, "mdb": mdb, "northbound": northbound,
    }.items() if value is not None}
    return {
        "schema_version": "openubmc-debug.v1", "ip": target, "ok": True,
        "observed_at": TIME, "target_epoch": 7, "request": {"mdb_only": True},
        "result": {
            "freshness": {"status": "fresh", "complete": True},
            "drive": {**DEVICE, **stages},
        },
    }


def advice_request():
    return {
        "schema": "openubmc-debug.diagnostic-advice-request.v1",
        "target": TARGET, "device": DEVICE, "snapshot_at": TIME,
        "target_epochs": {TARGET: 7}, "max_age_seconds": 30,
        "sources": {"capture": captured()},
        "facts": [{
            "stage": "mdb", "source_id": "capture",
            "device_pointer": "/result/drive", "value_pointer": "/result/drive/mdb",
        }],
        "queries": {
            "hardware_discovery": {
                "target": TARGET,
                "selectors": [{"id": "scanner-drive0", "kind": "mdb", "queries": ["lsprop ScannerDrive0"]}],
            },
        },
    }


def comparison_request(*, mdb=False, hardware=True):
    sys.path.insert(0, str(ROOT / "openubmc-target-runtime"))
    from openubmc_target_runtime import build_comparison_receipt, comparison_target_identities
    path = ROOT / "openubmc-debug/scripts/_comparison.py"
    spec = importlib.util.spec_from_file_location("advice_comparison", path)
    comparison = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = comparison
    spec.loader.exec_module(comparison)
    args = {"targets": [{"ip": "192.0.2.10", "role": "reference"}, {"ip": TARGET, "role": "candidate"}], "mdb_only": True}
    sources = {
        "reference": captured("192.0.2.10", hardware=True, mdb=True, northbound=True),
        "candidate": captured(hardware=hardware, mdb=mdb, northbound=mdb),
    }
    observations = [comparison.TargetObservation.success(
        role=role, target_id=identity, started_at=TIME, completed_at=TIME, result=sources[role],
    ) for role, identity in comparison_target_identities(args["targets"])]
    raw = comparison.build_dual_comparison(observations=observations)
    receipt = build_comparison_receipt(raw, args, ["captured-comparison"], source_results_complete=True)
    request = advice_request()
    request.update(sources=sources, include_fault_chain=True, reference_target="192.0.2.10",
                   comparison_receipt=receipt.to_public_dict(), queries={})
    request["target_epochs"]["192.0.2.10"] = 7
    request["facts"] = [{
        "source_id": source_id, "stage": stage, "device_pointer": "/result/drive",
        "value_pointer": "/result/drive/" + stage,
    } for source_id in sources for stage in ("hardware_discovery", "mdb", "northbound")
        if stage in sources[source_id]["result"]["drive"]]
    return request


class DiagnosticAdviceTests(unittest.TestCase):
    def run_helper(self, request):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "request.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            for field, suffix in {
                "HOME": "home", "CODEX_HOME": "codex", "XDG_CONFIG_HOME": "config",
                "XDG_DATA_HOME": "data", "XDG_CACHE_HOME": "cache",
            }.items():
                directory = root / suffix
                directory.mkdir()
                environment[field] = str(directory)
            result = subprocess.run(
                [sys.executable, str(CLI), "--input", str(path)],
                capture_output=True, text=True, env=environment, timeout=15,
            )
            return result, json.loads(result.stdout) if result.stdout.strip() else None

    def test_missing_drive_recommends_the_observation_that_separates_remaining_causes(self):
        result, advice = self.run_helper(advice_request())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(advice["status"], "advisory")
        hypotheses = {item["id"]: item for item in advice["hypotheses"]}
        self.assertEqual(hypotheses["hardware_not_discovered"]["status"], "unknown")
        self.assertEqual(hypotheses["mdb_not_created"]["status"], "unknown")
        self.assertEqual(hypotheses["northbound_not_published"]["status"], "contradicted")
        self.assertEqual([item["stage"] for item in advice["next_observations"]], ["hardware_discovery"])
        suggested = advice["next_observations"][0]
        self.assertEqual(suggested["query"], advice_request()["queries"]["hardware_discovery"])
        self.assertEqual(suggested["expected_outcomes"], {
            "present": ["mdb_not_created"], "absent": ["hardware_not_discovered"],
        })
        self.assertTrue(hypotheses["mdb_not_created"]["supporting_refs"])
        self.assertNotIn("diagnosis_record", advice)
        self.assertNotIn("fault_chain", advice)

    def test_partial_device_evidence_cannot_fulfil_a_hypothesis(self):
        request = advice_request()
        request["sources"]["capture"] = captured(hardware=True, mdb=False, northbound=False)
        request["facts"] = [dict(request["facts"][0], stage=stage, value_pointer="/result/drive/" + stage)
                            for stage in ("hardware_discovery", "mdb", "northbound")]
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        states = {item["id"]: item["status"] for item in advice["hypotheses"]}
        self.assertEqual(states, {
            "hardware_not_discovered": "contradicted", "mdb_not_created": "fulfilled",
            "northbound_not_published": "contradicted",
        })
        self.assertEqual(advice["next_observations"], [])
        request["sources"]["capture"]["result"]["drive"]["content_complete"] = False
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["status"] for item in advice["hypotheses"]}, {"unknown"})
        self.assertEqual({item["status"] for item in advice["facts"]}, {"unknown"})

    def test_invalid_references_and_unbounded_requests_produce_no_advice(self):
        invalid = []
        missing = advice_request()
        missing["facts"][0]["value_pointer"] = "/not-captured"
        invalid.append(missing)
        excessive = advice_request()
        excessive["facts"] *= 25
        invalid.append(excessive)
        ambiguous = advice_request()
        ambiguous["device"] = {}
        invalid.append(ambiguous)
        wrong_target = advice_request()
        wrong_target["queries"]["hardware_discovery"]["target"] = "192.0.2.99"
        invalid.append(wrong_target)
        malformed = advice_request()
        malformed["sources"]["capture"]["result"]["freshness"] = "fresh"
        invalid.append(malformed)
        for index, request in enumerate(invalid):
            with self.subTest(index=index):
                result, advice = self.run_helper(request)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(advice)
                self.assertNotIn("Traceback", result.stderr)

    def test_chain_reports_mdb_divergence_only_after_comparable_hardware_discovery(self):
        request = comparison_request()
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        chain = advice["fault_chain"]
        self.assertEqual(chain["status"], "different")
        self.assertEqual(chain["first_observed_divergence"], "mdb")
        self.assertEqual([stage["stage"] for stage in chain["stages"]], ["hardware_discovery", "mdb", "northbound"])
        self.assertEqual([stage["status"] for stage in chain["stages"]], ["same", "different", "different"])
        self.assertEqual(chain["differences"], request["comparison_receipt"]["differences"])
        self.assertEqual(chain["comparison_receipt_id"], request["comparison_receipt"]["receipt_id"])
        result, advice = self.run_helper(comparison_request(mdb=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(advice["fault_chain"]["status"], "same")
        self.assertIsNone(advice["fault_chain"]["first_observed_divergence"])

    def test_fact_binding_cannot_borrow_another_devices_value(self):
        request = advice_request()
        request["sources"]["capture"]["result"]["other_drive"] = {"Name": "Drive1", "Protocol": "NVMe", "mdb": True}
        request["facts"][0]["value_pointer"] = "/result/other_drive/mdb"
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(advice)

    def test_array_references_require_canonical_json_pointer_indices(self):
        request = advice_request()
        result = request["sources"]["capture"]["result"]
        result["drives"] = [result.pop("drive"), dict(DEVICE, mdb=False)]
        for token in ("0", "1", "-1", "+1", "01", " 1", "-"):
            with self.subTest(token=token):
                request["facts"][0].update(
                    device_pointer=f"/result/drives/{token}",
                    value_pointer=f"/result/drives/{token}/mdb",
                )
                process, advice = self.run_helper(request)
                self.assertEqual(process.returncode, 0 if token in {"0", "1"} else 2, process.stderr)
                if token not in {"0", "1"}:
                    self.assertIsNone(advice)

    def test_historical_fresh_labels_expired_windows_and_unknown_epochs_do_not_suppress_observation(self):
        old = advice_request()
        old.pop("snapshot_at")
        old["sources"]["capture"]["observed_at"] = "2000-01-01T00:00:00+00:00"
        expired_age = advice_request()
        expired_age["snapshot_at"] = "2026-09-08T01:00:31+00:00"
        expired_window = advice_request()
        expired_window["sources"]["capture"]["result"]["freshness"]["valid_until"] = TIME
        changed = advice_request()
        changed["target_epochs"][TARGET] = 8
        unknown_source = advice_request()
        unknown_source["sources"]["capture"].pop("target_epoch")
        unknown_current = advice_request()
        unknown_current.pop("target_epochs")
        for index, request in enumerate((old, expired_age, expired_window, changed, unknown_source, unknown_current)):
            with self.subTest(index=index):
                result, advice = self.run_helper(request)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(advice["facts"][0]["status"], "unknown")
                self.assertIsNone(advice["facts"][0]["present"])
                self.assertEqual({item["status"] for item in advice["hypotheses"]}, {"unknown"})
                self.assertEqual(advice["next_observations"][0]["stage"], "hardware_discovery")

    def test_runtime_observation_reference_is_retained_only_when_it_matches_the_captured_document(self):
        request = advice_request()
        document = {
            "schema": "openubmc.target-runtime.v1/observation-source-v1",
            "scope": {"target": TARGET}, "target": TARGET, "scope_digest": "b" * 64,
            "observed_at": TIME, "target_epoch": 7, "target_fingerprint": "drive-target",
            "reusable": True, "fresh_until": 1788830100.0, "raw": request["sources"]["capture"],
        }
        body = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        reference = {
            "schema": "openubmc.target-runtime.v1/semantic-runtime-v1/observation-ref",
            "handle": "blob://" + digest, "digest": "sha256:" + digest,
            "size": len(body), "kind": "observation", "provenance": "runtime-observation",
            "retention_hint": "run-lifetime", "target": TARGET,
            "scope_digest": "sha256:" + "b" * 64, "observed_at": TIME,
            "target_fingerprint": "drive-target", "target_epoch": 7,
        }
        request["sources"]["capture"] = document
        request["observation_refs"] = {"capture": reference}
        request["facts"][0].update(device_pointer="/raw/result/drive", value_pointer="/raw/result/drive/mdb")
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(advice["facts"][0]["status"], "observed")
        self.assertEqual(advice["facts"][0]["evidence_ref"]["observation_ref"], reference)
        request["observation_refs"]["capture"]["digest"] = "sha256:" + "0" * 64
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(advice)

    def test_workflow_epoch_is_bound_to_the_captured_target(self):
        request = advice_request()
        source = request["sources"]["capture"]
        source.pop("target_epoch")
        source["result"]["runtime"] = {"status": {"targets": [{
            "target": {"host": TARGET}, "epochs": {"target_epoch": 7},
        }]}}
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(advice["facts"][0]["status"], "observed")
        self.assertEqual(advice["facts"][0]["evidence_ref"]["target_epoch"], 7)
        source["result"]["runtime"]["status"]["targets"][0]["target"]["host"] = "192.0.2.99"
        result, advice = self.run_helper(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(advice["facts"][0]["status"], "unknown")

    def test_missing_hardware_and_inconclusive_sources_keep_chain_location_unknown(self):
        missing = comparison_request(hardware=None)
        stale = comparison_request()
        stale["sources"]["candidate"]["result"]["freshness"]["status"] = "stale"
        mixed = comparison_request()
        mixed["sources"]["candidate"]["result"]["drive"]["Protocol"] = "SATA"
        incomplete = comparison_request()
        incomplete["comparison_receipt"]["status"] = "partial"
        for index, request in enumerate((missing, stale, mixed, incomplete)):
            with self.subTest(index=index):
                result, advice = self.run_helper(request)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(advice["fault_chain"]["status"], "inconclusive")
                self.assertIsNone(advice["fault_chain"]["first_observed_divergence"])
                if index == 0:
                    self.assertEqual(advice["fault_chain"]["stages"][0]["stage"], "hardware_discovery")
                    self.assertEqual(advice["fault_chain"]["stages"][0]["status"], "unknown")

    def test_malformed_comparison_and_time_requests_fail_without_a_traceback(self):
        cases = []
        for field, value in (("freshness", []), ("sources", [None]), ("differences", "wrong")):
            request = comparison_request()
            request["comparison_receipt"][field] = value
            cases.append(request)
        for field, value in (("max_age_seconds", True), ("max_age_seconds", 901), ("snapshot_at", "2026-09-08"),
                             ("target_epochs", {TARGET: True})):
            request = advice_request()
            request[field] = value
            cases.append(request)
        for index, request in enumerate(cases):
            with self.subTest(index=index):
                result, advice = self.run_helper(request)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(advice)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
