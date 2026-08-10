from __future__ import annotations

import sys
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import EvidenceLedger  # noqa: E402


class EvidenceLedgerContractTests(unittest.TestCase):
    def test_ledger_is_bounded_and_keeps_only_a_bounded_summary(self) -> None:
        ledger = EvidenceLedger(max_records=2, max_summary_bytes=16)

        first = ledger.append(
            target_fingerprint="a" * 64,
            collector="mdbctl",
            target_epoch=0,
            lane_epochs={"ssh": 0, "telnet": 0, "redfish": 0},
            freshness="fresh",
            status="ok",
            summary="first dynamic value",
            size_bytes=1024,
        )
        second = ledger.append(
            target_fingerprint="a" * 64,
            collector="active-alarms",
            target_epoch=0,
            lane_epochs={"ssh": 0, "telnet": 0, "redfish": 0},
            freshness="fresh",
            status="partial",
            summary="second dynamic value",
            size_bytes=2048,
        )
        third = ledger.append(
            target_fingerprint="a" * 64,
            collector="collect-logs",
            target_epoch=0,
            lane_epochs={"ssh": 0, "telnet": 0, "redfish": 0},
            freshness="fresh",
            status="ok",
            summary="third dynamic value",
            size_bytes=4096,
            artifact_reference="/tmp/evidence-3",
        )

        snapshot = ledger.to_public_dict()
        self.assertEqual(snapshot["record_count"], 2)
        self.assertEqual(
            [record["evidence_id"] for record in snapshot["records"]],
            [second.evidence_id, third.evidence_id],
        )
        self.assertNotIn(first.evidence_id, str(snapshot))
        self.assertLessEqual(
            len(snapshot["records"][0]["summary"].encode("utf-8")),
            16,
        )
        self.assertEqual(snapshot["records"][1]["size_bytes"], 4096)
        self.assertEqual(
            snapshot["records"][1]["artifact_reference"],
            "/tmp/evidence-3",
        )

    def test_new_evidence_requests_are_distinct_records_not_query_cache_hits(self) -> None:
        ledger = EvidenceLedger(max_records=4)

        first = ledger.append(
            target_fingerprint="b" * 64,
            collector="mdbctl",
            target_epoch=0,
            lane_epochs={"ssh": 0},
            freshness="fresh",
            status="ok",
            summary="value=1",
        )
        second = ledger.append(
            target_fingerprint="b" * 64,
            collector="mdbctl",
            target_epoch=0,
            lane_epochs={"ssh": 0},
            freshness="fresh",
            status="ok",
            summary="value=2",
        )

        self.assertNotEqual(first.evidence_id, second.evidence_id)
        self.assertEqual(ledger.to_public_dict()["record_count"], 2)


if __name__ == "__main__":
    unittest.main()
