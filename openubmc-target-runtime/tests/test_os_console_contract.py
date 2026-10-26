"""Console selectors stay bounded, indivisible and independent of BMC identity."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openubmc_target_runtime.semantic_runtime import ObservationQuery, AgentPreflightError
from openubmc_target_runtime.observation import reusable_selector_fragments, selected_scope_complete, aggregate_observation_partitions


class ConsoleContractTests(unittest.TestCase):
    def test_summary_keeps_the_collected_transport_including_older_evidence(self):
        from openubmc_target_runtime.os_console_contract import os_console_summary
        for transport in ('bmc-ssh-sol', 'ipmi-sol'):
            with self.subTest(transport=transport):
                self.assertEqual(os_console_summary({'transport': transport})['transport'], transport)

    def query(self, names=None):
        return ObservationQuery.from_query({'target': '192.0.2.10', 'selectors': [
            {'id': 'host', 'kind': 'os_console', 'names': ['identity', 'network'] if names is None else names}]})

    def test_untrusted_names_and_commands_are_rejected_before_collection(self):
        for names in ([], ['reboot'], ['identity', 'identity'], ['network; reboot'], [{}], [1]):
            with self.subTest(names=names), self.assertRaises(AgentPreflightError):
                self.query(names)
        with self.assertRaises(AgentPreflightError):
            ObservationQuery.from_query({'target': '192.0.2.10', 'selectors': [
                {'kind': 'os_console', 'names': ['identity'], 'queries': ['id']}]})
        with self.assertRaises(AgentPreflightError):
            ObservationQuery.from_query({'target': '192.0.2.10', 'selectors': [
                {'id': 'a', 'kind': 'os_console', 'names': ['identity']},
                {'id': 'b', 'kind': 'os_console', 'names': ['network']}]})

    def test_probes_share_one_session_and_never_reuse_value_fragments(self):
        query = self.query()
        self.assertEqual(len(query.collection_partitions()), 1)
        self.assertEqual(query.collection_partitions()[0].selectors[0].names, ('identity', 'network'))
        fragments, missing = reusable_selector_fragments(query, query, {})
        self.assertEqual(fragments, ())
        self.assertEqual(missing, query.selectors)
        self.assertFalse(selected_scope_complete({'result': {'os_console': {'host': {
            'complete': True, 'requested': ['identity']}}}}, query))

    def test_mixed_bmc_and_os_partitions_keep_console_source_and_identity(self):
        query = ObservationQuery.from_query({'target': '192.0.2.10', 'selectors': [
            {'id': 'bmc', 'kind': 'capability', 'names': ['ssh']},
            {'id': 'host', 'kind': 'os_console', 'names': ['identity', 'network']}]})
        parts = query.collection_partitions()
        self.assertEqual(len(parts), 2)
        console = {'requested': ['identity', 'network'], 'complete': True,
                   'identity': {'boot_id': 'os-boot'}, 'gaps': []}
        raw = aggregate_observation_partitions(query, ((parts[0], {'ok': True, 'result': {
            'capabilities': {'ssh_transport': True}}}), (parts[1], {'ok': True, 'result': {
            'os_console': {'host': console}}})))
        self.assertEqual(raw['result']['os_console']['host'], console)
        self.assertTrue(selected_scope_complete(raw, query))


if __name__ == '__main__':
    unittest.main()
