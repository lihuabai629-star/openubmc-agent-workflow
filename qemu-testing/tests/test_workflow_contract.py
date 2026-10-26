import importlib.util
import hashlib
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qemu_workflow_contract",
    ROOT / "scripts" / "qemu_workflow_contract.py",
)
assert SPEC is not None and SPEC.loader is not None
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW - timedelta(seconds=120)


def identity(path: str) -> dict[str, object]:
    return {"path": path, "sha256": "a" * 64, "size": 1}


def file_identity(path: pathlib.Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def plan() -> dict[str, object]:
    return {
        "schema": CONTRACT.PLAN_SCHEMA,
        "scenario": "daily-development",
        "actions": ["discover", "launch", "observe", "smoke"],
        "repository": {"root": "/workspace/manifest", "revision": "source-1"},
        "launcher": {
            **identity("/workspace/manifest/x86_system.py"),
            "authorization_enforcement": {
                "mode": "reviewed_wrapper",
                "seam": "authorize before subprocess",
                "evidence_ids": ["seam-negative-test"],
            },
        },
        "runtime": {
            "root": "/workspace/output/qemu-1",
            "mode": "fresh",
            "preserve_data": True,
        },
        "artifact": {
            "index": identity("/workspace/output/build.json"),
            "source_revision": "source-1",
            "product_version": "1.0.0",
            "image_inputs": [{"role": "hpm", **identity("/workspace/output/product.hpm")}],
        },
        "ports": [
            {"protocol": "tcp", "bind": "127.0.0.1", "host_port": 22222, "guest_port": 22}
        ],
        "intent": {
            "expose_non_loopback_ports": False,
            "weaken_verification": False,
        },
        "authorization": {
            "local_process_control": True,
            "expose_non_loopback_ports": False,
            "weaken_verification": False,
        },
        "verification": {
            "profile": "targeted",
            "required_smoke": ["ssh", "redfish"],
            "tls_mode": "exact_fingerprint",
            "stability_seconds": 120,
            "not_before": START.isoformat(),
            "max_evidence_age_seconds": 900,
            "check_modes": {"ssh": "host_key_pinned", "redfish": "exact_fingerprint"},
        },
        "baseline": {
            "expected_state": "healthy",
            "fault_injection": "after-baseline",
        },
    }


def result(selected_plan: dict[str, object]) -> dict[str, object]:
    layers = {
        name: {
            "status": "passed",
            "summary": f"{name} passed",
            "evidence_ids": [f"{name}-evidence"],
            "observed_at": NOW.isoformat(),
        }
        for name in CONTRACT.required_layers(set(selected_plan["actions"]))
    }
    if "stability" in layers:
        layers["stability"].update(
            observed_seconds=120,
            boot_id_start="boot-1",
            boot_id_end="boot-1",
            service_identities_unchanged=True,
        )
    smoke = [
        {
            "name": name,
            "endpoint": "127.0.0.1",
            "observed_at": NOW.isoformat(),
            "verification_mode": selected_plan["verification"]["check_modes"][name],
            "status": "passed",
            "evidence_ids": [f"{name}-evidence"],
        }
        for name in selected_plan["verification"]["required_smoke"]
    ]
    return {
        "schema": CONTRACT.RESULT_SCHEMA,
        "started_at": START.isoformat(),
        "finished_at": NOW.isoformat(),
        "plan_digest": CONTRACT.canonical_digest(selected_plan),
        "status": "completed",
        "instance": {
            "runtime_root": "/workspace/output/qemu-1",
            "identity": "run-1",
            "artifact_index_sha256": "a" * 64,
            "pid": 123,
            "process_start": "start-1",
            "executable": "/workspace/qemu-system-x86_64",
            "boot_id": "boot-1",
            "product_version": "1.0.0",
        },
        "baseline": {"status": "healthy", "evidence_ids": ["healthy-baseline"]},
        "layers": layers,
        "smoke": smoke,
        "failure_classification": "none",
        "evidence_ids": ["qemu-result"],
    }


class QemuWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(CONTRACT, "datetime", wraps=datetime).start()
        self.addCleanup(patch.stopall)
        clock.now.return_value = NOW

    def test_complete_result_binds_every_requested_layer_and_smoke(self):
        selected_plan = plan()
        summary = CONTRACT.validate_result(selected_plan, result(selected_plan))
        self.assertEqual(summary["status"], "completed")
        self.assertIsNone(summary["first_nonpassing_layer"])

    def test_launch_requires_process_authorization(self):
        selected_plan = plan()
        selected_plan["authorization"]["local_process_control"] = False
        with self.assertRaisesRegex(CONTRACT.ContractError, "local process control"):
            CONTRACT.validate_plan(selected_plan)

    def test_targeted_task_command_needs_no_wrapper_certificate_or_healthy_baseline(self):
        selected_plan = plan()
        selected_plan["launcher"]["authorization_enforcement"] = {
            "mode": "task_command", "seam": "scoped argv and runtime identity review",
            "evidence_ids": ["launcher-source-review"],
        }
        selected_plan["verification"].update(
            required_smoke=["process"], tls_mode="not_applicable",
            check_modes={"process": "local_process_identity"}, stability_seconds=1,
        )
        selected_plan["baseline"] = {"expected_state": "not_requested", "fault_injection": "disabled"}
        receipt = result(selected_plan)
        receipt.pop("baseline")
        self.assertEqual(CONTRACT.validate_result(selected_plan, receipt)["status"], "completed")
        selected_plan["authorization"]["local_process_control"] = False
        with self.assertRaisesRegex(CONTRACT.ContractError, "local process control"):
            CONTRACT.validate_plan(selected_plan)

    def test_not_applicable_cannot_bypass_requested_tls_or_fault_baseline(self):
        selected_plan = plan()
        selected_plan["verification"]["tls_mode"] = "not_applicable"
        with self.assertRaisesRegex(CONTRACT.ContractError, "tls_mode"):
            CONTRACT.validate_plan(selected_plan)
        selected_plan = plan()
        selected_plan["baseline"]["expected_state"] = "not_requested"
        with self.assertRaisesRegex(CONTRACT.ContractError, "healthy baseline"):
            CONTRACT.validate_plan(selected_plan)

    def test_non_loopback_exposure_requires_intent_and_authorization(self):
        selected_plan = plan()
        selected_plan["ports"][0]["bind"] = "0.0.0.0"
        with self.assertRaisesRegex(CONTRACT.ContractError, "non-loopback"):
            CONTRACT.validate_plan(selected_plan)

    def test_completed_result_cannot_skip_required_smoke(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        receipt["smoke"] = receipt["smoke"][:-1]
        with self.assertRaisesRegex(CONTRACT.ContractError, "omits required checks"):
            CONTRACT.validate_result(selected_plan, receipt)

    def test_insecure_smoke_verification_is_rejected(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        receipt["smoke"][0]["verification_mode"] = "insecure"
        with self.assertRaisesRegex(CONTRACT.ContractError, "not trusted"):
            CONTRACT.validate_result(selected_plan, receipt)

    def test_weakening_requires_bounded_scope_and_recovery(self):
        selected_plan = plan()
        selected_plan["intent"]["weaken_verification"] = True
        selected_plan["authorization"]["weaken_verification"] = True
        selected_plan["verification"].update(
            tls_mode="weakened_authorized", weakened_checks=["redfish"],
            check_modes={"ssh": "host_key_pinned", "redfish": "weakened_authorized"})
        with self.assertRaisesRegex(CONTRACT.ContractError, "weakened_scope"):
            CONTRACT.validate_plan(selected_plan)
        selected_plan["verification"].update(
            weakened_scope="isolated loopback web endpoint",
            expires_at=(NOW + timedelta(minutes=5)).isoformat(),
            recovery_steps=["restore exact certificate fingerprint verification"],
        )
        summary = CONTRACT.validate_plan(selected_plan)
        self.assertEqual(summary["scenario"], "daily-development")

    def test_existing_runtime_requires_stateful_identity_and_reason(self):
        selected_plan = plan()
        selected_plan["runtime"]["mode"] = "existing"
        with self.assertRaisesRegex(CONTRACT.ContractError, "stateful_reason"):
            CONTRACT.validate_plan(selected_plan)
        selected_plan["runtime"].update(
            stateful_reason="verify persistence across restart",
            initial_artifact=identity("/workspace/output/initial.hpm"),
        )
        summary = CONTRACT.validate_plan(selected_plan)
        self.assertEqual(summary["scenario"], "daily-development")
        self.assertEqual(selected_plan["runtime"]["mode"], "existing")

    def test_secret_bearing_fields_are_rejected(self):
        selected_plan = plan()
        selected_plan["password"] = "must-not-enter-plan"
        with self.assertRaisesRegex(CONTRACT.ContractError, "forbidden"):
            CONTRACT.validate_plan(selected_plan)

    def test_current_file_identity_is_rehashed_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            launcher = root / "launcher.py"
            build_index = root / "build.json"
            hpm = root / "product.hpm"
            launcher.write_text("launcher", encoding="utf-8")
            build_index.write_text("{}", encoding="utf-8")
            hpm.write_bytes(b"hpm")
            selected_plan = plan()
            selected_plan["repository"] = {
                "root": str(root),
                "revision": "source-1",
            }
            selected_plan["launcher"].update(file_identity(launcher))
            selected_plan["runtime"]["root"] = str(root / "runtime")
            selected_plan["artifact"]["index"] = file_identity(build_index)
            selected_plan["artifact"]["image_inputs"] = [
                {"role": "hpm", **file_identity(hpm)}
            ]
            CONTRACT.validate_plan(selected_plan, verify_files=True)
            hpm.write_bytes(b"changed")
            with self.assertRaisesRegex(CONTRACT.ContractError, "filesystem identity"):
                CONTRACT.validate_plan(selected_plan, verify_files=True)

    def test_launch_failure_can_be_reported_without_a_fabricated_process(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        receipt.update(status="failed", failure_classification="launcher")
        receipt["instance"] = {
            "runtime_root": selected_plan["runtime"]["root"],
            "artifact_index_sha256": selected_plan["artifact"]["index"]["sha256"],
        }
        for name, layer in receipt["layers"].items():
            layer["status"] = "failed" if name == "launcher" else "blocked"
        for row in receipt["smoke"]:
            row.update(status="blocked", verification_mode="not_run")
        summary = CONTRACT.validate_result(selected_plan, receipt)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["first_nonpassing_layer"], "launcher")

    def test_stability_requires_the_actual_observation_window(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        receipt["layers"]["stability"]["observed_seconds"] = 119
        with self.assertRaisesRegex(CONTRACT.ContractError, "shorter"):
            CONTRACT.validate_result(selected_plan, receipt)

    def test_guest_reboot_or_service_restart_rejects_stability_pass(self):
        selected_plan = plan()
        for changes in (
            {"boot_id_end": "boot-2"},
            {"service_identities_unchanged": False},
        ):
            with self.subTest(changes=changes):
                receipt = result(selected_plan)
                receipt["layers"]["stability"].update(changes)
                with self.assertRaisesRegex(CONTRACT.ContractError, "changed"):
                    CONTRACT.validate_result(selected_plan, receipt)

    def test_full_x86_acceptance_cannot_hide_omitted_services(self):
        selected_plan = plan()
        selected_plan["verification"]["profile"] = "full-x86"
        with self.assertRaisesRegex(CONTRACT.ContractError, "omits checks"):
            CONTRACT.validate_plan(selected_plan)
        selected_plan["verification"]["required_smoke"] = sorted(CONTRACT.FULL_X86_SMOKE)
        selected_plan["verification"]["check_modes"] = {
            name: "exact_fingerprint" if name in {"web", "redfish"} else
            "protocol_native" if name == "ipmi" else "host_key_pinned"
            for name in CONTRACT.FULL_X86_SMOKE}
        selected_plan["verification"]["stability_seconds"] = 119
        with self.assertRaisesRegex(CONTRACT.ContractError, "120 seconds"):
            CONTRACT.validate_plan(selected_plan)
        selected_plan["verification"]["stability_seconds"] = 120
        CONTRACT.validate_result(selected_plan, result(selected_plan))

    def test_daily_development_cannot_pass_with_an_unhealthy_baseline(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        receipt["baseline"]["status"] = "active-faults"
        with self.assertRaisesRegex(CONTRACT.ContractError, "healthy baseline"):
            CONTRACT.validate_result(selected_plan, receipt)

    def test_unhealthy_guest_can_be_reported_as_a_failure(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        receipt.update(status="failed", failure_classification="unhealthy-baseline")
        receipt["baseline"]["status"] = "active-faults"
        receipt["layers"]["services"]["status"] = "failed"
        summary = CONTRACT.validate_result(selected_plan, receipt)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["first_nonpassing_layer"], "services")

    def test_result_from_another_plan_is_rejected(self):
        selected_plan = plan()
        receipt = result(selected_plan)
        selected_plan["artifact"]["index"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(CONTRACT.ContractError, "plan_digest"):
            CONTRACT.validate_result(selected_plan, receipt)

    def test_invalid_stale_future_and_preplan_evidence_is_rejected(self):
        for timestamp in ("not-a-date", "2000-01-01T00:00:00Z", "2026-02-30T00:00:00Z",
                          (NOW + timedelta(seconds=1)).isoformat(),
                          (START - timedelta(seconds=1)).isoformat()):
            for collection, key in (("smoke", 0), ("layers", "process")):
                selected = plan()
                receipt = result(selected)
                receipt[collection][key]["observed_at"] = timestamp
                with self.subTest(timestamp=timestamp, collection=collection):
                    with self.assertRaises(CONTRACT.ContractError):
                        CONTRACT.validate_result(selected, receipt)
        selected = plan()
        with self.assertRaisesRegex(CONTRACT.ContractError, "stale"):
            CONTRACT.validate_result(selected, result(selected), now=NOW + timedelta(hours=1))
        historical = CONTRACT.validate_result(selected, result(selected), now=NOW + timedelta(days=1), historical=True)
        self.assertFalse(historical["freshness_verified"])

    def test_expired_weak_authorization_is_only_valid_as_historical_evidence(self):
        selected = plan()
        selected["intent"]["weaken_verification"] = True
        selected["authorization"]["weaken_verification"] = True
        selected["verification"].update(
            tls_mode="weakened_authorized", weakened_checks=["redfish"],
            check_modes={"ssh": "host_key_pinned", "redfish": "weakened_authorized"},
            weakened_scope="loopback redfish", recovery_steps=["restore TLS"],
            expires_at=(NOW + timedelta(seconds=1)).isoformat())
        with self.assertRaisesRegex(CONTRACT.ContractError, "expired"):
            CONTRACT.validate_plan(selected, now=NOW + timedelta(minutes=1))
        self.assertFalse(CONTRACT.validate_result(selected, result(selected), historical=True,
                                                  now=NOW + timedelta(hours=1))["freshness_verified"])
        selected["verification"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(CONTRACT.ContractError, "expired"):
            CONTRACT.validate_result(selected, result(selected), historical=True)

    def test_result_and_plan_modes_are_bound_to_the_protocol(self):
        for mode in ("local_process_identity", "host_key_pinned", "ca_bundle"):
            selected = plan()
            receipt = result(selected)
            receipt["smoke"][1]["verification_mode"] = mode
            with self.assertRaisesRegex(CONTRACT.ContractError, "not trusted"):
                CONTRACT.validate_result(selected, receipt)
        selected["verification"]["check_modes"]["redfish"] = "local_process_identity"
        with self.assertRaisesRegex(CONTRACT.ContractError, "protocol"):
            CONTRACT.validate_plan(selected)

    def test_plain_http_and_impossible_stability_window_are_rejected(self):
        selected = plan()
        receipt = result(selected)
        receipt["smoke"][1]["endpoint"] = "http://127.0.0.1/redfish/v1"
        with self.assertRaisesRegex(CONTRACT.ContractError, "plain HTTP"):
            CONTRACT.validate_result(selected, receipt)
        receipt = result(selected)
        receipt["started_at"] = NOW.isoformat()
        with self.assertRaisesRegex(CONTRACT.ContractError, "duration exceeds"):
            CONTRACT.validate_result(selected, receipt)


if __name__ == "__main__":
    unittest.main()
