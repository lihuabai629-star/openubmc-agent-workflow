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

    def test_fresh_closeout_rejects_any_contradictory_additional_timeline_proof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            contradictions = {
                "runtime": ("completed_at", "2026-08-29T10:00:00Z"),
                "upgrade": ("completed_at", "2026-08-29T11:45:00Z"),
                "freshness": ("observed_at", "2000-01-01T00:00:00Z"),
                "hardware": ("observed_at", "2000-01-01T00:00:00Z"),
            }
            for dimension, (field, value) in contradictions.items():
                original_ref = manifest[dimension]["evidence"][0]
                original = json.loads(
                    Path(original_ref["path"]).read_text(encoding="utf-8")
                )
                original[field] = value
                contradictory = root / f"{dimension}-contradictory.json"
                contradictory.write_text(
                    json.dumps(original, sort_keys=True), encoding="utf-8"
                )
                manifest[dimension]["evidence"].append(
                    {"path": str(contradictory), "sha256": sha256(contradictory)}
                )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertEqual(report["dimensions"]["runtime"]["verified_evidence_count"], 2)
        self.assertTrue(
            any("timeline" in item or "predates" in item for item in report["violations"])
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

    def test_historical_evidence_rejects_manifest_authored_claims(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(
                root, mode="historical-reconstruction"
            )
            unrelated = root / "unrelated.json"
            unrelated.write_text('{"name":"official-ut"}', encoding="utf-8")
            manifest["diagnosis"]["evidence"] = [
                {
                    "path": str(unrelated),
                    "sha256": sha256(unrelated),
                    "claims": [
                        {
                            "kind": "json_equals",
                            "path": "name",
                            "value": "official-ut",
                        }
                    ],
                }
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any(
                "manifest-authored claims are unsupported" in item
                for item in report["violations"]
            )
        )

    def test_historical_evidence_rejects_unknown_evidence_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(
                root, mode="historical-reconstruction"
            )
            diagnosis = root / "diagnosis.txt"
            diagnosis.write_text(
                "根因：错误关联键\n修复：使用全局映射\n", encoding="utf-8"
            )
            manifest["diagnosis"]["evidence"] = [
                {
                    "path": str(diagnosis),
                    "sha256": sha256(diagnosis),
                    "evidence_type": "invented-diagnosis-format",
                }
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("unknown historical evidence_type" in item for item in report["violations"])
        )

    def test_historical_evidence_type_must_match_its_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(
                root, mode="historical-reconstruction"
            )
            record = root / "record.txt"
            record.write_text("10/10 passed\n", encoding="utf-8")
            manifest["diagnosis"]["evidence"] = [
                {
                    "path": str(record),
                    "sha256": sha256(record),
                    "evidence_type": "workflow-official-ut-record",
                }
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("does not match diagnosis dimension" in item for item in report["violations"])
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

            diagnosis = root / "diagnosis.md"
            diagnosis.write_text(
                "失败位置：storage 使用了错误的关联键。\n"
                "修复方案：改为消费全局盘位映射。\n",
                encoding="utf-8",
            )
            official_ut = root / "official-ut.log"
            official_ut.write_text("10/10 passed\n", encoding="utf-8")
            component_build = root / "component-build.log"
            component_build.write_text(
                "storage/1.0.0@openubmc/stable: Created package revision "
                "0123456789abcdef0123456789abcdef\n"
                "storage/1.0.0@openubmc/stable: Full package reference: "
                "storage/1.0.0@openubmc/stable#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb#"
                "0123456789abcdef0123456789abcdef\n"
                "构建成功\n",
                encoding="utf-8",
            )
            product_build = root / "product-build.log"
            product_build.write_text(
                "hpm 构建成功 !!\n"
                "给 hpm 包 rootfs_openUBMC.hpm 签名\n"
                "任务 personal 执行成功\n",
                encoding="utf-8",
            )
            upgrade = root / "upgrade.md"
            upgrade.write_text(
                "上传与激活 | 完成\n安装版本确认 | `12.08.21.10`\n",
                encoding="utf-8",
            )
            freshness = root / "timeline.log"
            freshness.write_text(
                "elapsed=50s manager_ready\n"
                "elapsed=158s drives=1 direct=1 direct_attributed=1 raid=0 "
                "raid_zero=0 health_ok=1 presence_ok=1 serial_ok=1\n"
                "accepted_elapsed=158s\n",
                encoding="utf-8",
            )
            hardware = root / "drive-summary.json"
            hardware.write_text(
                json.dumps(
                    {
                        "accepted_elapsed_seconds": 158,
                        "summary": {
                            "drives": 1,
                            "direct": 1,
                            "direct_attributed": 1,
                            "raid": 0,
                            "raid_zero": 0,
                            "health_ok": 1,
                            "presence_ok": 1,
                            "serial_ok": 1,
                        },
                        "drives": [
                            {
                                "id": 23,
                                "controller": 255,
                                "resource": 1,
                                "health": 0,
                                "presence": 1,
                                "serial_present": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def historical_ref(path: Path, evidence_type: str) -> dict[str, str]:
                return {
                    "path": str(path),
                    "sha256": sha256(path),
                    "evidence_type": evidence_type,
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
                "diagnosis": {
                    "status": "passed",
                    "evidence": [
                        historical_ref(diagnosis, "workflow-diagnosis-record")
                    ],
                },
                "source": {
                    "status": "completed",
                    "repositories": [
                        {"name": "storage", "path": str(source), "commit": source_commit}
                    ],
                },
                "validation": {
                    "official_ut": {
                        "status": "passed",
                        "evidence": [
                            historical_ref(
                                official_ut, "workflow-official-ut-record"
                            )
                        ],
                    },
                    "build": {
                        "status": "compiled",
                        "evidence": [
                            historical_ref(component_build, "component-build-log"),
                            historical_ref(product_build, "product-build-log"),
                        ],
                    },
                },
                "artifact": {
                    "status": "verified",
                    "path": str(artifact),
                    "sha256": sha256(artifact),
                    "size": artifact.stat().st_size,
                    "version": "12.08.21.10",
                },
                "upgrade": {
                    "status": "completed",
                    "evidence": [
                        historical_ref(upgrade, "workflow-upgrade-record")
                    ],
                },
                "freshness": {
                    "status": "fresh",
                    "evidence": [
                        historical_ref(freshness, "reboot-acceptance-timeline")
                    ],
                },
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive23", "protocol": "NVMe"}],
                    "evidence": [historical_ref(hardware, "drive-summary-json")],
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
