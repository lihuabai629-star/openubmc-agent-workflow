from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import product_closeout_ingestion as ingestion
from scripts.product_closeout_qualification import qualify
from scripts.tests.test_product_closeout_qualification import complete_manifest


def _write_artifact_metadata(artifact: Path, manifest: dict[str, object]) -> None:
    artifact_document = manifest["artifact"]
    Path(str(artifact) + ".metadata.json").write_text(
        json.dumps(
            {
                "schema": "openubmc-agent-workflow/artifact-metadata-v1",
                "artifact": {
                    "sha256": artifact_document["sha256"],
                    "size": artifact_document["size"],
                    "kind": "openubmc-hpm",
                },
                "product_version": artifact_document["version"],
                "provenance": "openubmc-build",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _ingestion_input(manifest: dict[str, object]) -> dict[str, object]:
    validation = manifest["validation"]

    def evidence(dimension: dict[str, object]) -> list[dict[str, str]]:
        return [
            {
                "proof_path": item["path"],
                **(
                    {"support_path": item["supporting_evidence"]["path"]}
                    if "supporting_evidence" in item
                    else {}
                ),
            }
            for item in dimension["evidence"]
        ]

    return {
        "schema": "openubmc-agent-workflow.product-closeout-ingestion.v1",
        "case": {
            "name": "fresh closeout assembled from trusted evidence",
            "required_protocols": ["NVMe"],
        },
        "runtime": {
            "run_id": manifest["runtime"]["run_id"],
            "evidence": evidence(manifest["runtime"]),
        },
        "source_repositories": [
            {
                "name": repository["name"],
                "path": repository["path"],
            }
            for repository in manifest["source"]["repositories"]
        ],
        "artifact_path": manifest["artifact"]["path"],
        "evidence": {
            "diagnosis": evidence(manifest["diagnosis"]),
            "official_ut": evidence(validation["official_ut"]),
            "build": evidence(validation["build"]),
            "upgrade": evidence(manifest["upgrade"]),
            "freshness": evidence(manifest["freshness"]),
            "hardware": evidence(manifest["hardware"]),
        },
        "freshness_max_age_seconds": manifest["freshness"]["max_age_seconds"],
    }


class ProductCloseoutIngestionTests(unittest.TestCase):
    def test_assembles_a_promotable_manifest_from_runtime_and_fixed_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original, _, _, artifact = complete_manifest(root)
            _write_artifact_metadata(artifact, original)
            runtime_repository = Path(original["runtime"]["repository"]["path"])

            assembled = ingestion.assemble_manifest(
                _ingestion_input(original),
                runtime_repository=runtime_repository,
            )
            report = qualify(assembled, runtime_repository=runtime_repository)

        self.assertEqual(assembled["mode"], "fresh-runtime")
        self.assertEqual(assembled["case"]["target"], "target-1")
        self.assertEqual(assembled["runtime"]["terminal_outcome"], "completed")
        self.assertEqual(
            assembled["source"]["repositories"][0]["commit"],
            original["source"]["repositories"][0]["commit"],
        )
        self.assertEqual(assembled["artifact"], original["artifact"])
        self.assertTrue(report["qualified"], report["violations"])
        self.assertTrue(report["promotable"], report["gaps"])

    def test_cli_writes_deterministic_manifest_and_qualification_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original, _, _, artifact = complete_manifest(root)
            _write_artifact_metadata(artifact, original)
            descriptor = root / "ingestion.json"
            descriptor.write_text(
                json.dumps(_ingestion_input(original), sort_keys=True),
                encoding="utf-8",
            )
            output_manifest = root / "assembled-manifest.json"
            output_report = root / "assembled-report.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(ingestion.__file__)),
                    str(descriptor),
                    "--runtime-repository",
                    original["runtime"]["repository"]["path"],
                    "--output-manifest",
                    str(output_manifest),
                    "--output-report",
                    str(output_report),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout),
                json.loads(output_manifest.read_text(encoding="utf-8")),
            )
            report = json.loads(output_report.read_text(encoding="utf-8"))

        self.assertTrue(report["qualified"])
        self.assertTrue(report["promotable"])

    def test_rejects_supporting_evidence_that_is_not_bound_by_its_proof(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original, _, _, artifact = complete_manifest(root)
            _write_artifact_metadata(artifact, original)
            descriptor = _ingestion_input(original)
            replacement = root / "replacement-diagnosis.md"
            replacement.write_text("root cause: wrong\nfix: wrong\n", encoding="utf-8")
            descriptor["evidence"]["diagnosis"][0]["support_path"] = str(
                replacement
            )

            with self.assertRaisesRegex(
                ValueError,
                "supporting evidence digest does not match the proof binding",
            ):
                ingestion.assemble_manifest(
                    descriptor,
                    runtime_repository=Path(
                        original["runtime"]["repository"]["path"]
                    ),
                )

    def test_rejects_ambiguous_runtime_target_without_operator_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original, _, _, artifact = complete_manifest(root)
            _write_artifact_metadata(artifact, original)
            from scripts.tests.test_product_closeout_qualification import (
                rebuild_runtime_ledger,
            )

            rebuild_runtime_ledger(original, additional_targets=("target-2",))

            with self.assertRaisesRegex(
                ValueError,
                "multiple Runtime targets require case.target",
            ):
                ingestion.assemble_manifest(
                    _ingestion_input(original),
                    runtime_repository=Path(
                        original["runtime"]["repository"]["path"]
                    ),
                )


if __name__ == "__main__":
    unittest.main()
