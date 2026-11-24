"""Discovery names remain visible after both observation projection passes."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openubmc_target_runtime import RuntimeMcpService
from tests.test_agent_gateway import SemanticBackend


class DiscoveryBackend(SemanticBackend):
    def __init__(self, lines):
        super().__init__()
        self.lines = lines

    def observe_query(self, task, arguments, context):
        value = super().observe_query(task, arguments, context)
        for child in value['result']['lanes']['ssh'].values():
            child['payload']['result'] = {'stdout_lines': self.lines}
        return value


class DiscoveryProjectionTests(unittest.TestCase):
    def receipt(self, query, lines):
        service = RuntimeMcpService(DiscoveryBackend(lines))
        self.addCleanup(service.close)
        return service.call_exposed_tool('observe', {
            'target': '192.0.2.10', 'selectors': [{'id': 'discovery', 'kind': 'mdb', 'queries': [query]}],
        }, task_id='discovery-projection', operation_id='discovery-projection-1')

    def test_names_after_the_old_single_line_limit_survive_the_soft_display_target(self):
        names = [f'Component_{index:03}' for index in range(294)] + ['ManagerVersion', 'FirmwareInventory']
        line = '  '.join(names)
        for query in ['lsclass', 'lsmc', 'lsobj Component']:
            with self.subTest(query=query):
                receipt = self.receipt(query, [line])
                value = receipt['results']['discovery']['values'][0]['value']
                self.assertEqual(value['stdout_lines'], [line])
                self.assertTrue(value['discovery']['projection_complete'])
                self.assertEqual(value['discovery']['query'], query)
                self.assertEqual(value['discovery']['source_bytes'], len(line.encode()))
                self.assertFalse(receipt.get('projection_truncated', False))
                self.assertIn('observation_ref', receipt)

    def test_oversized_discovery_marks_its_visible_prefix_and_keeps_the_evidence(self):
        line = '  '.join(f'Component_{index:06}' for index in range(4000))
        receipt = self.receipt('lsclass', [line])
        value = receipt['results']['discovery']['values'][0]['value']
        self.assertLessEqual(len('\n'.join(value['stdout_lines']).encode()), 16 * 1024)
        self.assertFalse(value['discovery']['projection_complete'])
        self.assertEqual(value['discovery']['source_bytes'], len(line.encode()))
        self.assertTrue(receipt['projection_truncated'])
        self.assertTrue(receipt['content_compacted'])
        self.assertIn('discovery', receipt['next_guidance'])
        self.assertIn('observation_ref', receipt)

    def test_multiline_discovery_and_empty_results_keep_exact_names(self):
        for lines in [[], ['Component_0', 'Component_1', 'ManagerVersion_0']]:
            with self.subTest(lines=lines):
                value = self.receipt('lsobj Component', lines)['results']['discovery']['values'][0]['value']
                self.assertEqual(value['stdout_lines'], lines)
                self.assertTrue(value['discovery']['projection_complete'])


if __name__ == '__main__':
    unittest.main()
