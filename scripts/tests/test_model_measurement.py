"""Failure taxonomy uses observed provider and MCP facts, not tool counts alone."""
import sys
from pathlib import Path
import unittest
import tempfile
import json
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime_model_measurement import classify_failure, attempt


class ModelMeasurementTests(unittest.TestCase):
    def test_provider_errors_are_transport_failures_even_after_a_gate(self):
        self.assertEqual(classify_failure(False,[{'status':500}],[],True),('transport','provider_http_500'))
        self.assertEqual(classify_failure(False,[{'status':200,'error_class':'TimeoutError'}],[],False),('transport','provider_TimeoutError'))

    def test_model_invalid_input_and_runtime_invariant_are_distinct(self):
        self.assertEqual(classify_failure(False,[{'status':200}],['AgentPreflightError'],True),('model','invalid_request'))
        self.assertEqual(classify_failure(False,[{'status':200}],['RuntimeError'],True),('runtime','RuntimeError'))
        self.assertEqual(classify_failure(False,[{'status':200}],[],False),('plugin','mcp_not_initialized'))
        self.assertEqual(classify_failure(True,[{'status':200}],['AgentPreflightError'],True),(None,None))

    def test_failed_launch_remains_an_attempt_with_a_durable_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            args=SimpleNamespace(source=root,codex=str(root/'missing'),env_key='FIXTURE_KEY',timeout=1)
            row=attempt(args,root,SimpleNamespace(records=[],server_port=1),'fixture',1,'A',0)
            self.assertEqual(row['failure_class'],'harness')
            self.assertFalse(row['valid'])
            self.assertEqual(len(list(root.glob('*/started.json'))),1)
            self.assertEqual(json.loads((root/'fixture-01-A/result.json').read_text())['failure_class'],'harness')


if __name__=='__main__':unittest.main()
