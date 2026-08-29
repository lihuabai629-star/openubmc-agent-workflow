from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "product_closeout_qualification.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def structured_proof(root: Path, name: str, payload: dict[str, object]) -> tuple[dict[str, str], Path]:
    path = root / f"{name}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {"path": str(path), "sha256": sha256(path)}, path


def complete_manifest(
    root: Path,
    *,
    mode: str = "fresh-runtime",
) -> tuple[dict[str, object], Path, Path, Path]:
    source = root / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "qualification@example.com"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Qualification"],
        cwd=source,
        check=True,
    )
    (source / "fix.lua").write_text("return true\n", encoding="utf-8")
    subprocess.run(["git", "add", "fix.lua"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fix"], cwd=source, check=True)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    artifact = root / "firmware.hpm"
    artifact.write_bytes(b"firmware")
    artifact_identity = {
        "sha256": sha256(artifact),
        "size": artifact.stat().st_size,
        "version": "1.0.0",
    }
    proof_base = {
        "schema": "openubmc-agent-workflow.product-closeout-proof.v1",
        "target": "target-1",
        "run_id": "run-product-closeout-1",
    }
    runtime_ref, runtime_proof = structured_proof(
        root,
        "runtime-proof",
        {
            **proof_base,
            "dimension": "runtime",
            "status": "completed",
            "terminal_outcome": "completed",
            "completed_at": "2026-08-29T12:00:00Z",
        },
    )
    diagnosis_ref, diagnosis_proof = structured_proof(
        root,
        "diagnosis-proof",
        {
            **proof_base,
            "dimension": "diagnosis",
            "status": "passed",
            "evidence_ids": ["observation-1"],
        },
    )
    official_ut_ref, _ = structured_proof(
        root,
        "official-ut-proof",
        {
            **proof_base,
            "dimension": "official_ut",
            "status": "passed",
            "source_commits": [source_commit],
            "tests_run": 1,
            "tests_failed": 0,
        },
    )
    build_ref, _ = structured_proof(
        root,
        "build-proof",
        {
            **proof_base,
            "dimension": "build",
            "status": "compiled",
            "source_commits": [source_commit],
            "compiled_units": 1,
        },
    )
    upgrade_ref, _ = structured_proof(
        root,
        "upgrade-proof",
        {
            **proof_base,
            "dimension": "upgrade",
            "status": "completed",
            "artifact": artifact_identity,
            "installed_version": "1.0.0",
            "completed_at": "2026-08-29T11:00:00Z",
        },
    )
    freshness_ref, _ = structured_proof(
        root,
        "freshness-proof",
        {
            **proof_base,
            "dimension": "freshness",
            "status": "fresh",
            "artifact_sha256": artifact_identity["sha256"],
            "observed_at": "2026-08-29T12:00:00Z",
        },
    )
    hardware_ref, _ = structured_proof(
        root,
        "hardware-proof",
        {
            **proof_base,
            "dimension": "hardware",
            "status": "covered",
            "required_protocols": ["NVMe"],
            "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
            "observed_at": "2026-08-29T12:00:00Z",
        },
    )
    manifest: dict[str, object] = {
        "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
        "mode": mode,
        "case": {
            "name": "fresh closeout",
            "target": "target-1",
            "required_protocols": ["NVMe"],
        },
        "runtime": {
            "run_id": "run-product-closeout-1",
            "terminal_outcome": "completed",
            "evidence": [runtime_ref],
        },
        "diagnosis": {"status": "passed", "evidence": [diagnosis_ref]},
        "source": {
            "status": "completed",
            "repositories": [
                {"name": "source", "path": str(source), "commit": source_commit}
            ],
        },
        "validation": {
            "official_ut": {"status": "passed", "evidence": [official_ut_ref]},
            "build": {"status": "compiled", "evidence": [build_ref]},
        },
        "artifact": {
            "status": "verified",
            "path": str(artifact),
            "sha256": artifact_identity["sha256"],
            "size": artifact.stat().st_size,
            "version": "1.0.0",
        },
        "upgrade": {"status": "completed", "evidence": [upgrade_ref]},
        "freshness": {
            "status": "fresh",
            "max_age_seconds": 3600,
            "evidence": [freshness_ref],
        },
        "hardware": {
            "status": "covered",
            "required_protocols": ["NVMe"],
            "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
            "evidence": [hardware_ref],
        },
    }
    del runtime_proof
    return manifest, source, diagnosis_proof, artifact


def run_qualification(root: Path, manifest: dict[str, object]) -> subprocess.CompletedProcess[str]:
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest_path)],
        text=True,
        capture_output=True,
        check=False,
    )


class ProductCloseoutQualificationTests(unittest.TestCase):
    def test_complete_fresh_runtime_closeout_is_promotable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["qualified"])
        self.assertTrue(report["promotable"])
        self.assertEqual(report["claim_level"], "fresh-runtime-product-closed")
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["violations"], [])

    def test_evidence_digest_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, proof, _ = complete_manifest(root)
            proof.write_text('{"tampered":true}', encoding="utf-8")
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("evidence digest mismatch" in item for item in report["violations"])
        )

    def test_fresh_closeout_rejects_hash_valid_self_attested_empty_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            empty = root / "empty-proof.json"
            empty.write_text("{}", encoding="utf-8")
            manifest["diagnosis"]["evidence"] = [
                {"path": str(empty), "sha256": sha256(empty)}
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("diagnosis" in item and "structured proof" in item for item in report["violations"])
        )

    def test_fresh_closeout_rejects_upgrade_proof_for_another_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            upgrade_ref = manifest["upgrade"]["evidence"][0]
            upgrade_path = Path(upgrade_ref["path"])
            proof = json.loads(upgrade_path.read_text(encoding="utf-8"))
            proof["artifact"]["sha256"] = "0" * 64
            upgrade_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
            upgrade_ref["sha256"] = sha256(upgrade_path)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("upgrade" in item and "artifact identity" in item for item in report["violations"])
        )

    def test_fresh_closeout_rejects_stale_or_misordered_target_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            for dimension in ("freshness", "hardware"):
                evidence_ref = manifest[dimension]["evidence"][0]
                evidence_path = Path(evidence_ref["path"])
                proof = json.loads(evidence_path.read_text(encoding="utf-8"))
                proof["observed_at"] = "2000-01-01T00:00:00Z"
                evidence_path.write_text(
                    json.dumps(proof, sort_keys=True), encoding="utf-8"
                )
                evidence_ref["sha256"] = sha256(evidence_path)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("freshness evidence predates upgrade" in item for item in report["violations"])
        )

    def test_source_commit_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["source"]["repositories"][0]["commit"] = "f" * 40
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("commit mismatch" in item for item in report["violations"])
        )

    def test_artifact_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["artifact"]["sha256"] = "0" * 64
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("artifact: digest mismatch" in item for item in report["violations"])
        )

    def test_historical_product_evidence_is_qualified_but_not_fresh_runtime_promotable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "storage"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "qualification@example.com"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Qualification"],
                cwd=source,
                check=True,
            )
            (source / "fix.lua").write_text("return true\n", encoding="utf-8")
            subprocess.run(["git", "add", "fix.lua"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fix"], cwd=source, check=True)
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

            evidence = {}
            for name in (
                "diagnosis",
                "official-ut",
                "build",
                "upgrade",
                "freshness",
                "hardware",
            ):
                path = root / f"{name}.json"
                path.write_text(json.dumps({"name": name}), encoding="utf-8")
                evidence[name] = {
                    "path": str(path),
                    "sha256": sha256(path),
                    "claims": [
                        {"kind": "json_equals", "path": "name", "value": name}
                    ],
                }
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"verified-historical-firmware")

            manifest = {
                "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
                "mode": "historical-reconstruction",
                "case": {
                    "name": "630 NVMe attribution",
                    "target": "historical-630-target",
                    "required_protocols": ["NVMe"],
                },
                "runtime": {
                    "run_id": "",
                    "terminal_outcome": "unavailable",
                },
                "diagnosis": {"status": "passed", "evidence": [evidence["diagnosis"]]},
                "source": {
                    "status": "completed",
                    "repositories": [
                        {"name": "storage", "path": str(source), "commit": source_commit}
                    ],
                },
                "validation": {
                    "official_ut": {
                        "status": "passed",
                        "evidence": [evidence["official-ut"]],
                    },
                    "build": {
                        "status": "compiled",
                        "evidence": [evidence["build"]],
                    },
                },
                "artifact": {
                    "status": "verified",
                    "path": str(artifact),
                    "sha256": sha256(artifact),
                    "size": artifact.stat().st_size,
                    "version": "12.08.21.10",
                },
                "upgrade": {"status": "completed", "evidence": [evidence["upgrade"]]},
                "freshness": {"status": "fresh", "evidence": [evidence["freshness"]]},
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive23", "protocol": "NVMe"}],
                    "evidence": [evidence["hardware"]],
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(manifest_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["qualified"])
        self.assertFalse(report["promotable"])
        self.assertEqual(report["claim_level"], "historical-product-validated")
        self.assertEqual(report["violations"], [])
        self.assertIn("fresh_runtime_identity_unavailable", report["gaps"])
        self.assertTrue(report["evidence_digest"].startswith("sha256:"))

    def test_fresh_closeout_without_terminal_runtime_outcome_is_unqualified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "qualification@example.com"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Qualification"],
                cwd=source,
                check=True,
            )
            (source / "fix.lua").write_text("return true\n", encoding="utf-8")
            subprocess.run(["git", "add", "fix.lua"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fix"], cwd=source, check=True)
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            proof = root / "proof.json"
            proof.write_text("{}", encoding="utf-8")
            proof_ref = {"path": str(proof), "sha256": sha256(proof)}
            artifact = root / "firmware.hpm"
            artifact.write_bytes(b"firmware")
            manifest = {
                "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
                "mode": "fresh-runtime",
                "case": {
                    "name": "fresh closeout",
                    "target": "target-1",
                    "required_protocols": ["NVMe"],
                },
                "runtime": {"run_id": "", "terminal_outcome": "unavailable"},
                "diagnosis": {"status": "passed", "evidence": [proof_ref]},
                "source": {
                    "status": "completed",
                    "repositories": [
                        {"name": "source", "path": str(source), "commit": source_commit}
                    ],
                },
                "validation": {
                    "official_ut": {"status": "passed", "evidence": [proof_ref]},
                    "build": {"status": "compiled", "evidence": [proof_ref]},
                },
                "artifact": {
                    "status": "verified",
                    "path": str(artifact),
                    "sha256": sha256(artifact),
                    "size": artifact.stat().st_size,
                    "version": "1.0.0",
                },
                "upgrade": {"status": "completed", "evidence": [proof_ref]},
                "freshness": {"status": "fresh", "evidence": [proof_ref]},
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
                    "evidence": [proof_ref],
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(manifest_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertFalse(report["promotable"])
        self.assertIn("runtime_run_id_missing", report["gaps"])
        self.assertIn("runtime_terminal_outcome=unavailable", report["gaps"])

    def test_sata_evidence_cannot_satisfy_nvme_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proof = root / "hardware.json"
            proof.write_text("{}", encoding="utf-8")
            manifest = {
                "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
                "mode": "historical-reconstruction",
                "case": {
                    "name": "protocol scope",
                    "target": "target-1",
                    "required_protocols": ["NVMe"],
                },
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive1", "protocol": "SATA"}],
                    "evidence": [{"path": str(proof), "sha256": sha256(proof)}],
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(manifest_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)
        self.assertFalse(report["dimensions"]["hardware"]["accepted"])
        self.assertIn("hardware_protocols_missing=NVMe", report["gaps"])


if __name__ == "__main__":
    unittest.main()
