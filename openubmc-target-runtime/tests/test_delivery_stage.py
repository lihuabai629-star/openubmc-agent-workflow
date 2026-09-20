from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openubmc_target_runtime.delivery_stage import (  # noqa: E402
    DELIVERY_STAGES,
    assess_delivery_stages,
    identity_split,
)


def receipt(stage, *, facts=None, artifact=None, status="completed", evidence=None):
    return {
        "stage": stage,
        "status": status,
        "evidence_ids": evidence or [f"e-{stage}"],
        "facts": facts or {},
        "artifacts": [artifact] if artifact else [],
    }


class DeliveryStageTests(unittest.TestCase):
    def test_stage_progression_requires_explicit_evidence(self):
        rows = [
            receipt("diagnosis"),
            receipt("development", facts={"authored_files": ["src/a.lua"]}),
            receipt("build", facts={"component_versions": {"a": "1.0"}, "artifact_kind": "component-package"}),
        ]
        result = assess_delivery_stages(rows)
        self.assertEqual(result["highest"], "component-built")
        self.assertEqual(result["next"], "product-built")

    def test_build_does_not_infer_packaged_or_deployed(self):
        result = assess_delivery_stages([
            receipt("diagnosis"),
            receipt("development", facts={"authored_files": ["a"]}),
            receipt("build", facts={"artifact_kind": "openubmc-hpm"}, artifact={"kind": "openubmc-hpm", "sha256": "a" * 64}),
        ])
        self.assertEqual(result["highest"], "product-built")
        self.assertFalse(result["stages"]["packaged"]["verified"])

    def test_live_patch_identity_is_separate_from_packaged_identity(self):
        result = identity_split([
            receipt("build", facts={"artifact_sha256": "a" * 64, "product_version": "1.0"}),
            receipt("live_patch", facts={"artifact_sha256": "b" * 64, "product_version": "1.0"}),
        ])
        self.assertTrue(result["diverged"])
        self.assertNotEqual(result["packaged"]["artifact_sha256"], result["live_patch"]["artifact_sha256"])

    def test_all_stages_are_named_and_ordered(self):
        self.assertEqual(DELIVERY_STAGES[0], "diagnosed")
        self.assertEqual(DELIVERY_STAGES[-1], "rollback-verified")


if __name__ == "__main__":
    unittest.main()
