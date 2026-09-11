"""Component acceptance through the public execute transport and impact CLI."""

import copy
import json
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "openubmc-build/tests"))
import test_change_impact as fixtures
from test_agent_gateway import (
    RuntimeMcpService,
    SemanticBackend,
    accept_diagnosis,
    gate_binding,
    artifact_ref,
    compiled_validation_payload,
    SQLiteRuntimeRepository,
    FilesystemBlobRepository,
)


class ComponentAcceptanceTests(unittest.TestCase):
    analyze = fixtures.ImpactCliTests.analyze

    def setUp(self):
        fixtures.ImpactCliTests.setUp(self)
        self.service = RuntimeMcpService(SemanticBackend())
        self.addCleanup(self.service.close)
        self.impact = json.loads(self.analyze("interface/mds/model.json").stdout)
        self.task = "component-acceptance"
        self.sequence = 0

    def start(self, strategy="source-only"):
        turn = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
                "delivery_strategy": strategy,
                "purpose": "repair component contract",
            },
            task_id=self.task,
            operation_id="component-start",
        )
        return accept_diagnosis(self.service, turn, task_id=self.task)

    def validations(self):
        values = []
        for component in self.impact["components"]:
            name = component["component"]
            readiness = name + "-readiness"
            values.append(
                {
                    "component": name,
                    "source": component["source"],
                    "dependencies": component["dependencies"],
                    "dependency_readiness": {
                        "readiness_id": readiness,
                        "status": "ready",
                        "resolution": "available",
                        "summary": "fixture dependencies available",
                        "check_commands": ["conan graph info ."],
                        "evidence_ids": [name + "-dependency-log"],
                        "attempt_count": 1,
                        "reused_by": ["official_ut", "build"],
                    },
                    "validation_results": [
                        {
                            "kind": kind,
                            "status": status,
                            "summary": "fixture checked execution",
                            "commands": [command],
                            "evidence_ids": [name + "-" + kind + "-log"],
                            "dependency_readiness_id": readiness,
                        }
                        for kind, status, command in [
                            ("official_ut", "passed", "bmcgo test"),
                            ("build", "compiled", "bmcgo build"),
                        ]
                    ],
                }
            )
        return values

    def respond(self, turn, rows, impact=True, status="completed", **extra):
        self.sequence += 1
        payload = {
            "source_revision": "fixture-revision",
            "authored_files": ["interface/mds/model.json"],
            "verification_plan": ["component official UT and build"],
            **extra,
        }
        if turn["gate"]["name"] == "build.artifact":
            product = self.root / "fixture.hpm"
            product.write_bytes(b"fixture firmware")
            payload = {
                "source_revision": "fixture-revision",
                **compiled_validation_payload("product"),
                "artifact_ref": artifact_ref(
                    product,
                    kind="openubmc-hpm",
                    target="192.0.2.20",
                    run_id=turn["run_id"],
                    version="fixture",
                ),
                **extra,
            }
        if impact:
            payload["change_impact"] = self.impact
            payload["component_validation"] = rows
        return self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": turn["run_id"],
                **gate_binding(turn),
                "response": {
                    "status": status,
                    "summary": "source change validated",
                    "payload": payload,
                },
            },
            task_id=self.task,
            operation_id="component-response-" + str(self.sequence),
        )

    def test_missing_consumer_does_not_complete_and_can_be_resubmitted(self):
        turn = self.start()
        rows = self.validations()
        with self.assertRaisesRegex(ValueError, "component"):
            self.respond(turn, rows[:-1])
        final = self.respond(turn, rows)
        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])

    def test_component_bindings_and_failed_checks_cannot_be_swapped_or_omitted(self):
        turn = self.start()
        for damage in ("duplicate", "source", "dependency", "ut", "build", "absent_ut"):
            with self.subTest(damage=damage):
                rows = copy.deepcopy(self.validations())
                if damage == "duplicate":
                    rows.append(rows[0])
                elif damage == "source":
                    rows[0]["source"] = rows[1]["source"]
                elif damage == "dependency":
                    rows[0]["dependencies"]["interface"]["git_head"] = "0" * 40
                elif damage == "ut":
                    rows[0]["validation_results"][0]["status"] = "failed_after_start"
                elif damage == "build":
                    rows[0]["validation_results"][1]["status"] = "compile_failed"
                else:
                    rows[0]["validation_results"].pop(0)
                    rows[0]["dependency_readiness"]["reused_by"] = ["build"]
                with self.assertRaisesRegex(ValueError, "component"):
                    self.respond(turn, rows)
        final = self.respond(turn, self.validations())
        self.assertEqual(final["state"], "completed")
        duplicate = self.respond(turn, self.validations())
        self.assertEqual(duplicate["outcome"], final["outcome"])

    def test_accepted_scope_survives_build_handoff_and_cannot_shrink(self):
        turn = self.start("build-upgrade")
        build = self.respond(turn, self.validations())
        self.assertEqual(build["gate"]["name"], "build.artifact")
        original = self.impact
        smaller = copy.deepcopy(original)
        smaller["components"] = smaller["components"][:2]
        smaller["dependency_edges"] = [
            edge for edge in smaller["dependency_edges"] if edge["consumer"] != "leaf"
        ]
        unsigned = {key: value for key, value in smaller.items() if key != "digest"}
        smaller["digest"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode()
            ).hexdigest()
        )
        self.impact = smaller
        with self.assertRaisesRegex(ValueError, "impact cannot change"):
            self.respond(build, self.validations())
        self.impact = original
        with self.assertRaisesRegex(ValueError, "component scope incomplete"):
            self.respond(build, [], impact=False)
        final = self.respond(build, self.validations())
        self.assertEqual(final["state"], "completed")

    def test_legacy_single_component_response_remains_accepted(self):
        final = self.respond(self.start(), [], impact=False)
        self.assertEqual(final["state"], "completed")

    def test_partial_component_evidence_survives_restart_and_only_missing_work_is_submitted(
        self,
    ):
        self.service.close()
        database, blobs = self.root / "runtime.sqlite", self.root / "blobs"
        self.service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=SQLiteRuntimeRepository(database),
            blob_repository=FilesystemBlobRepository(blobs),
        )
        self.addCleanup(self.service.close)
        turn = self.start()
        rows = self.validations()
        partial = self.respond(turn, rows[:-1], status="partial")
        self.assertEqual(partial["state"], "waiting_response")
        self.assertGreater(
            partial["gate"]["gate_version"], turn["gate"]["gate_version"]
        )
        self.service.close()
        self.service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=SQLiteRuntimeRepository(database),
            blob_repository=FilesystemBlobRepository(blobs),
        )
        self.addCleanup(self.service.close)
        resumed = self.service.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": turn["run_id"]},
            task_id=self.task,
            operation_id="component-restart",
        )
        final = self.respond(resumed, rows[-1:])
        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])


if __name__ == "__main__":
    unittest.main()
