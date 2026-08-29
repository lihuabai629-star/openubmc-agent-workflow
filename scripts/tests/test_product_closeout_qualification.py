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
    proof = root / "proof.json"
    proof.write_text("{}", encoding="utf-8")
    proof_ref = {"path": str(proof), "sha256": sha256(proof)}
    artifact = root / "firmware.hpm"
    artifact.write_bytes(b"firmware")
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
            "evidence": [proof_ref],
        },
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
    return manifest, source, proof, artifact


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
                evidence[name] = {"path": str(path), "sha256": sha256(path)}
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
