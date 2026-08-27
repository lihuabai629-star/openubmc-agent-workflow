from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "scripts" / "evaluation_harness.py"
SPEC = importlib.util.spec_from_file_location("evaluation_harness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class EvaluationHarnessMetadataTests(unittest.TestCase):
    def test_dsh_is_managed_as_an_evaluation_harness_not_a_product_client(self) -> None:
        workflow = json.loads(
            (WORKSPACE / "workflow.json").read_text(encoding="utf-8")
        )

        clients = workflow["clients"]
        harnesses = workflow["evaluation_harnesses"]
        self.assertNotIn("dsh", clients)
        self.assertTrue(
            all(client["role"] == "supported-product-client" for client in clients.values())
        )
        dsh = harnesses["dsh"]
        self.assertEqual(dsh["role"], "evaluation-harness")
        self.assertEqual(dsh["adapter"], "dsh-headless-cli-v1")
        self.assertEqual(dsh["executable"]["command"], "dsh")
        self.assertEqual(dsh["executable"]["package"], "@deepseek-ai/dsh")
        self.assertEqual(
            dsh["model_identity_fields"],
            ["provider", "model", "reasoning_effort"],
        )
        self.assertGreaterEqual(len(dsh["scenarios"]), 3)


class EvaluationHarnessPreflightTests(unittest.TestCase):
    @staticmethod
    def executor(versions: dict[str, str]):
        def run(command, **kwargs):
            del kwargs
            executable = Path(command[0]).name
            return subprocess.CompletedProcess(
                command,
                0,
                versions[executable] + "\n",
                "",
            )

        return run

    def test_preflight_accepts_a_compatible_dsh_without_product_configuration(self) -> None:
        report = harness.preflight_harness(
            WORKSPACE,
            "dsh",
            which=lambda command: f"/tools/{command}",
            executor=self.executor({"dsh": "0.1.1-rc.2", "node": "v22.23.1"}),
        )

        self.assertTrue(report["ready"])
        self.assertEqual(report["role"], "evaluation-harness")
        self.assertEqual(report["adapter"], "dsh-headless-cli-v1")
        self.assertEqual(report["executable"]["version"], "0.1.1-rc.2")
        self.assertEqual(report["node"]["version"], "22.23.1")
        self.assertEqual(report["issues"], [])

    def test_preflight_reports_missing_and_incompatible_dsh(self) -> None:
        missing = harness.preflight_harness(
            WORKSPACE,
            "dsh",
            which=lambda command: None if command == "dsh" else f"/tools/{command}",
            executor=self.executor({"node": "v22.23.1"}),
        )
        incompatible = harness.preflight_harness(
            WORKSPACE,
            "dsh",
            which=lambda command: f"/tools/{command}",
            executor=self.executor({"dsh": "0.0.9", "node": "v22.18.0"}),
        )

        self.assertFalse(missing["ready"])
        self.assertIn("dsh executable is unavailable", missing["issues"])
        self.assertEqual(
            missing["install_command"],
            ["npm", "install", "--global", "@deepseek-ai/dsh@>=0.1.0-rc.7 <0.2.0"],
        )
        self.assertFalse(incompatible["ready"])
        self.assertTrue(any("dsh version" in issue for issue in incompatible["issues"]))
        self.assertTrue(any("Node.js version" in issue for issue in incompatible["issues"]))

    def test_install_places_dsh_in_an_isolated_prefix(self) -> None:
        calls: list[list[str]] = []

        def execute(command, **kwargs):
            del kwargs
            calls.append(list(command))
            if command[0] == "npm":
                executable = Path(command[command.index("--prefix") + 1]) / "node_modules" / ".bin" / "dsh"
                executable.parent.mkdir(parents=True)
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "installed\n", "")
            if Path(command[0]).name == "dsh":
                return subprocess.CompletedProcess(command, 0, "0.1.1-rc.2\n", "")
            return subprocess.CompletedProcess(command, 0, "v22.23.1\n", "")

        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary) / "dsh-tooling"
            report = harness.install_harness(
                WORKSPACE,
                "dsh",
                install_root=install_root,
                executor=execute,
                which=lambda command: "/tools/node" if command == "node" else None,
            )

        self.assertTrue(report["ready"])
        self.assertEqual(report["install_root"], str(install_root))
        self.assertEqual(calls[0][:4], ["npm", "install", "--prefix", str(install_root)])
        self.assertIn("--no-save", calls[0])
        self.assertTrue(calls[0][-1].startswith("@deepseek-ai/dsh@"))


class EvaluationHarnessRunTests(unittest.TestCase):
    def test_formal_dsh_run_is_isolated_and_records_reproducible_identity(self) -> None:
        commit = "a" * 40
        preflight = {
            "ready": True,
            "harness": "dsh",
            "role": "evaluation-harness",
            "adapter": "dsh-headless-cli-v1",
            "executable": {
                "path": "/tools/dsh",
                "version": "0.1.1-rc.2",
            },
        }
        captured: dict[str, object] = {}

        def execute(command, **kwargs):
            captured["command"] = command
            captured["cwd"] = kwargs["cwd"]
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, "qualified\n", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_root = root / "tooling"
            managed_dsh = install_root / "node_modules" / ".bin" / "dsh"
            managed_dsh.parent.mkdir(parents=True)
            managed_dsh.write_text("#!/bin/sh\n", encoding="utf-8")
            preflight["install_root"] = str(install_root)
            preflight["executable"]["path"] = str(managed_dsh)
            settings = root / "settings.yaml"
            settings.write_text(
                "agent-default-model:\n"
                "  provider: cliproxy\n"
                "  model: gpt-5.6-sol\n"
                "  reasoningEffort: xhigh\n",
                encoding="utf-8",
            )
            credentials = root / "credentials.env"
            credentials.write_text("fixture=true\n", encoding="utf-8")
            run_root = root / "run"
            readiness = {
                "ok": True,
                "operational_ready": True,
                "release_identity_verified": True,
                "evaluation_ready": True,
                "source": {
                    "mode": "managed",
                    "dirty": False,
                    "current_commit": commit,
                },
                "release": {
                    "verified": True,
                    "trust_mode": "verified-immutable-source",
                    "source_commit": commit,
                },
            }
            plan = harness.prepare_dsh_run(
                WORKSPACE,
                "dsh",
                run_root=run_root,
                source_commit=commit,
                scenario={"name": "agent-interface-readonly", "version": "v1"},
                model_identity={
                    "provider": "cliproxy",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                },
                settings_source=settings,
                credentials_file=credentials,
                evaluation_readiness=readiness,
                preflight=preflight,
                source_selector=lambda value, *, workspace: value,
            )
            acceptance = {
                "schema": "openubmc-agent-workflow.scenario-acceptance.v1",
                "execution_id": plan["execution_id"],
                "source_commit": commit,
                "scenario": {"name": "agent-interface-readonly", "version": "v1"},
                "accepted": True,
            }
            result = harness.execute_dsh_run(
                plan,
                "perform the qualification",
                acceptance_receipt=acceptance,
                executor=execute,
            )

            isolation = plan["isolation"]
            self.assertEqual(Path(isolation["home"]), run_root / "home")
            self.assertEqual(Path(isolation["harness_home"]), run_root / "dsh-home")
            self.assertEqual(Path(isolation["sessions"]), run_root / "sessions")
            self.assertEqual(Path(isolation["runtime_state"]), run_root / "runtime-state")
            self.assertEqual(Path(isolation["mcp_config"]), run_root / "mcp.patch.yml")
            environment = captured["env"]
            self.assertEqual(environment["HOME"], str(run_root / "home"))
            self.assertEqual(environment["DSH_HOME"], str(run_root / "dsh-home"))
            self.assertEqual(
                environment["OPENUBMC_TARGET_RUNTIME_STATE_DIR"],
                str(run_root / "runtime-state"),
            )
            self.assertEqual(
                environment["OPENUBMC_CREDENTIALS_FILE"],
                str(credentials),
            )
            self.assertNotIn("CODEX_HOME", environment)

            identity = result["identity"]
            self.assertEqual(identity["source_commit"], commit)
            self.assertEqual(identity["harness"]["name"], "dsh")
            self.assertEqual(identity["harness"]["version"], "0.1.1-rc.2")
            self.assertEqual(identity["model"]["model"], "gpt-5.6-sol")
            self.assertEqual(identity["runtime"]["api_version"], "openubmc.target-runtime.v1")
            self.assertTrue(identity["runtime"]["content_digest"].startswith("sha256:"))
            self.assertEqual(
                identity["scenario"],
                {
                    "name": "agent-interface-readonly",
                    "version": "v1",
                    "acceptance": "scenario-receipt-v1",
                },
            )
            self.assertIn("openubmc-debug", identity["skills"]["digests"])
            self.assertTrue(identity["source_tree_digest"].startswith("sha256:"))
            self.assertTrue(identity["evaluation_readiness"]["digest"].startswith("sha256:"))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["harness_status"], "completed")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["stdout"], "qualified\n")

    def test_zero_exit_does_not_pass_without_scenario_acceptance(self) -> None:
        plan = {
            "run_root": "/tmp/not-used",
            "workspace": str(WORKSPACE),
            "command": ["/tools/dsh"],
            "environment": {},
            "identity": {
                "source_commit": "a" * 40,
                "scenario": {
                    "name": "execute-source-only",
                    "version": "v1",
                    "acceptance": "terminal-outcome-v1",
                },
            },
            "execution_id": "execution-one",
            "isolation": {"sessions": "/tmp/not-used/sessions"},
        }

        with tempfile.TemporaryDirectory() as temporary:
            plan["run_root"] = temporary
            result = harness.execute_dsh_run(
                plan,
                "perform the qualification",
                acceptance_receipt={
                    "schema": "openubmc-agent-workflow.scenario-acceptance.v1",
                    "execution_id": "execution-one",
                    "source_commit": "a" * 40,
                    "scenario": {"name": "execute-source-only", "version": "v1"},
                    "accepted": True,
                    "terminal_outcome": {"status": "running"},
                },
                executor=lambda command, **kwargs: subprocess.CompletedProcess(
                    command, 0, "finished\n", ""
                ),
            )

        self.assertEqual(result["harness_status"], "completed")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("terminal Outcome" in issue for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
