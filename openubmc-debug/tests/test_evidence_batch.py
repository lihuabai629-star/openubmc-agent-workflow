"""Offline batch processing of synthetic, already captured Debug results."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from _evidence_batch import compare_snapshots, normalize_timestamp, summarize_captured_snapshot
from _workflow_correlation import build_correlation
from _workflow_runtime import _compact_correlation


TIME = "2026-09-08 09:00:00"
EPOCH = int(datetime(2026, 9, 8, 1, tzinfo=timezone.utc).timestamp())
LINE = TIME + " EventDemo instance=Slot-A asserted " + "reading=42 threshold=40 " * 18


def captured(*, lines=None, paths=None, offset=480, numbers=True, complete=True):
    paths = paths or ["/var/log/alarm.log"]
    lines = lines if lines is not None else [LINE] * 48
    alarms = {
        "tool": "active_alarms", "ip": "target-a", "ok": True,
        "payload": {"result": {"record_count": 2, "records": [{
            "EventName": "EventDemo", "EventCode": "EventDemo",
            "ComponentLocation": "Slot-A", "State": "Asserted", "Timestamp": str(EPOCH),
        }] * 2}},
    }
    entries = []
    for path in paths:
        entry = {"path": path, "lines": list(lines), "truncated": not complete,
                 "content_complete": complete}
        if numbers:
            entry["line_numbers"] = list(range(1, len(lines) + 1))
        entries.append(entry)
    logs = {"tool": "collect_logs", "ip": "target-a", "ok": True,
            "payload": {"result": {"utc_offset_minutes": offset, "entries": entries}}}
    return alarms, logs


def batch(alarms, logs, *, version="v1", epoch=7):
    return summarize_captured_snapshot(target="target-a", source_version=version,
                                       target_epoch=epoch, alarms=alarms, logs=logs)


class EvidenceBatchTests(unittest.TestCase):
    def test_exact_duplicates_keep_every_raw_pointer_and_reduce_displayed_bytes(self):
        alarms, logs = captured()
        result = batch(alarms, logs)
        self.assertEqual(result["counts"], {"raw": 50, "unique": 2, "duplicates_collapsed": 48})
        alarm, line = result["groups"]
        self.assertEqual(alarm["count"], 2)
        self.assertEqual([item["pointer"] for item in alarm["refs"]],
                         [f"/alarms/payload/result/records/{index}" for index in range(2)])
        self.assertEqual(line["count"], 48)
        self.assertEqual([item["pointer"] for item in line["refs"]],
                         [f"/logs/payload/result/entries/0/lines/{index}" for index in range(48)])
        self.assertEqual(line["time"]["utc"], "2026-09-08T01:00:00+00:00")
        self.assertEqual(result["uncertainties"], [])
        correlation = build_correlation(alarms, logs, source_root="", max_matches=0,
                                        alarm_limit=2, timeout=1, workflow_keyword="",
                                        enabled=False, source_version="v1", target_epoch=7)
        compact = _compact_correlation(correlation)
        displayed = compact["evidence_pool"]["alarm_log_lines"]
        self.assertEqual(len(displayed), 1)
        self.assertEqual(displayed[0]["ids"], list(range(48)))
        self.assertEqual(displayed[0]["count"], 48)
        self.assertEqual([item["line_number"] for item in displayed[0]["pointers"]], list(range(1, 49)))
        old_shape = [{key: value for key, value in record.items() if key != "pointer"}
                     for record in correlation["evidence_pool"]["alarm_log_lines"]]
        self.assertLess(len(json.dumps(displayed).encode()), len(json.dumps(old_shape).encode()))
        self.assertEqual(correlation["records"][0]["temporal_relation"]["status"], "within_window")
        self.assertFalse(correlation["records"][0]["temporal_relation"]["causal_proof"])

    def test_source_and_path_boundaries_prevent_cross_scope_collapse(self):
        alarms, logs = captured(lines=[LINE] * 2, paths=["/var/log/a", "/var/log/b"])
        result = batch(alarms, logs)
        self.assertEqual(result["counts"]["unique"], 3)
        self.assertEqual(sorted(group["count"] for group in result["groups"] if group["kind"] == "log"), [2, 2])
        common = LINE + "X" * 600
        alarms, logs = captured(lines=[common + "tail-a", common + "tail-b"])
        correlation = build_correlation(alarms, logs, source_root="", max_matches=0,
                                        alarm_limit=2, timeout=1, workflow_keyword="", enabled=False)
        displayed = _compact_correlation(correlation)["evidence_pool"]["alarm_log_lines"]
        self.assertEqual(len(displayed), 2, "equal truncated previews must not collapse distinct lines")

    def test_unknown_time_failed_parse_missing_sequence_and_partial_collection_remain_visible(self):
        alarms, logs = captured(lines=[LINE, "2026-99-99 99:99:99 broken"], offset=None,
                                numbers=False, complete=False)
        result = batch(alarms, logs, version=None, epoch=None)
        self.assertIn("time_timezone_unknown", result["uncertainties"])
        self.assertIn("time_parse_failed", result["uncertainties"])
        self.assertIn("missing_sequence", result["uncertainties"])
        self.assertIn("partial_collection", result["uncertainties"])
        self.assertIn("source_version_unknown", result["uncertainties"])
        self.assertIn("target_epoch_unknown", result["uncertainties"])
        self.assertFalse(result["collection_complete"])
        self.assertEqual(normalize_timestamp("2026-09-08T09:00:00+08:00")["utc"],
                         "2026-09-08T01:00:00+00:00")
        alarms, logs = captured(lines=[LINE], offset=480)
        conflicted = summarize_captured_snapshot(
            target="target-a", source_version="v1", target_epoch=7,
            alarms=alarms, logs=logs, utc_offset_minutes=0,
        )
        self.assertIn("timezone_offset_conflict", conflicted["uncertainties"])
        self.assertIn("time_timezone_unknown", conflicted["uncertainties"])

    def test_explicit_fractional_timestamp_preserves_precision_and_offset(self):
        observed = normalize_timestamp("2026-09-08T09:00:00.123+08:00 EventDemo", 0)
        self.assertEqual(observed["status"], "known")
        self.assertEqual(observed["utc"], "2026-09-08T01:00:00.123000+00:00")
        self.assertEqual(observed["raw"], "2026-09-08T09:00:00.123+08:00")

    def test_cross_target_captured_results_cannot_form_comparable_snapshot(self):
        alarms, logs = captured(lines=[LINE])
        correct = batch(alarms, logs)
        logs["ip"] = "target-b"
        mixed = batch(alarms, logs)
        self.assertIn("target_source_conflict", mixed["uncertainties"])
        self.assertFalse(mixed["collection_complete"])
        compared = compare_snapshots(correct, mixed)
        self.assertFalse(compared["comparable"])
        self.assertIsNone(compared["changed"])
        self.assertIn("target_source_conflict", compared["reasons"])

    def test_cross_target_logs_cannot_support_alarm_correlation(self):
        alarms, logs = captured(lines=[LINE])
        logs["ip"] = "target-b"
        correlation = build_correlation(
            alarms, logs, source_root="", max_matches=0, alarm_limit=2,
            timeout=1, workflow_keyword="EventDemo", enabled=False,
        )
        self.assertEqual(correlation["correlation_blocked_by"], ["target_source_conflict"])
        self.assertEqual(correlation["records"], [])
        self.assertEqual(correlation["workflow_keyword_refs"], [])
        self.assertEqual(len(correlation["evidence_pool"]["alarm_log_lines"]), 1)
        self.assertEqual(correlation["evidence_pool"]["alarm_log_lines"][0]["target"], "target-b")

    def test_separate_workflow_log_target_must_match_alarm_target(self):
        alarms, logs = captured(lines=[LINE])
        _, workflow_logs = captured(lines=[LINE])
        workflow_logs["ip"] = "target-b"
        correlation = build_correlation(
            alarms, logs, source_root="", max_matches=0, alarm_limit=2,
            timeout=1, workflow_keyword="EventDemo", enabled=False,
            workflow_logs_result=workflow_logs,
        )
        self.assertEqual(correlation["correlation_blocked_by"], ["target_source_conflict"])
        self.assertEqual(correlation["records"], [])
        self.assertEqual(correlation["workflow_keyword_refs"], [])
        self.assertEqual(len(correlation["evidence_pool"]["workflow_log_lines"]), 1)

    def test_comparison_rejects_incompatible_snapshots_and_preserves_change_refs(self):
        alarms, logs = captured(lines=[LINE])
        before = batch(alarms, logs)
        after_alarms, after_logs = captured(lines=[LINE, TIME + " EventOther"])
        after = batch(after_alarms, after_logs)
        compared = compare_snapshots(before, after)
        self.assertTrue(compared["comparable"])
        self.assertTrue(compared["changed"])
        self.assertEqual(compared["added"][0]["refs"][0]["pointer"],
                         "/logs/payload/result/entries/0/lines/1")
        for changed, reason in (
            ({**after, "target": "target-b"}, "target_mismatch_or_unknown"),
            ({**after, "source_version": "v2"}, "source_version_conflict"),
            ({**after, "target_epoch": 8}, "stale_epoch"),
            ({**after, "collection_complete": False}, "partial_collection"),
            ({**after, "target_epoch": None}, "target_epoch_unknown"),
        ):
            with self.subTest(reason=reason):
                outcome = compare_snapshots(before, changed)
                self.assertFalse(outcome["comparable"])
                self.assertIn(reason, outcome["reasons"])
                self.assertIsNone(outcome["changed"])

    def test_alarm_log_time_links_stay_inside_the_window_and_do_not_prove_cause(self):
        alarms, logs = captured(lines=[LINE.replace("09:00:00", "09:10:00")])
        result = build_correlation(alarms, logs, source_root="", max_matches=0,
                                   alarm_limit=2, timeout=1, workflow_keyword="",
                                   enabled=False, time_window=300)
        relation = result["records"][0]["temporal_relation"]
        self.assertEqual(relation["status"], "no_match_in_window")
        self.assertEqual(relation["matched_log_refs"], [])
        self.assertFalse(relation["causal_proof"])
        logs["payload"]["result"]["utc_offset_minutes"] = None
        unknown = build_correlation(alarms, logs, source_root="", max_matches=0,
                                    alarm_limit=2, timeout=1, workflow_keyword="", enabled=False)
        self.assertEqual(unknown["records"][0]["temporal_relation"]["status"], "timezone_unknown")

    def test_processor_failure_keeps_original_correlation_evidence(self):
        alarms, logs = captured(lines=[LINE] * 2049)
        result = build_correlation(alarms, logs, source_root="", max_matches=0,
                                   alarm_limit=2, timeout=1, workflow_keyword="",
                                   enabled=False)
        self.assertEqual(result["batch"]["status"], "processor_failed")
        self.assertTrue(result["batch"]["raw_evidence_preserved"])
        self.assertEqual(len(result["evidence_pool"]["alarm_log_lines"]), 2049)


if __name__ == "__main__":
    unittest.main()
