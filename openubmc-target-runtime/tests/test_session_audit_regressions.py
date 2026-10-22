from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openubmc_target_runtime.diagnostic_receipt import _structured_results
from openubmc_target_runtime.agent_gateway import AgentGateway
from openubmc_target_runtime.mcp import JsonRpcMcpEndpoint
from openubmc_target_runtime.semantic_runtime import AgentPreflightError, ObservationQuery, ObservationSelector


class SessionAuditRegressions(unittest.TestCase):
    def test_invalid_systemd_selections_have_a_typed_actionable_rejection(self):
        for values in [{}, {'names':[]}, {'names':['ras.service','failed']},
                       {'names':['ras.service'],'queries':['status']}, {'names':['not-a-unit']}]:
            with self.subTest(values=values):
                with self.assertRaises(AgentPreflightError) as raised:
                    ObservationSelector.from_value({'id':'services','kind':'systemd',**values},1)
                self.assertEqual(raised.exception.detail.field, 'selectors[0].names')
                self.assertIn('allowed_forms', raised.exception.detail.limit)
        for names in [['ras.service'],['failed']]:
            self.assertEqual(ObservationSelector.from_value({'kind':'systemd','names':names},1).names, tuple(names))

    def test_systemd_corrections_preserve_target_and_other_selectors(self):
        for names in [[], ['ras.service','failed']]:
            query = {'target':'192.0.2.10','selectors':[
                {'id':'mdb','kind':'mdb','queries':['lsprop Object0']},
                {'id':'service','kind':'systemd','names':names}]}
            with self.subTest(names=names):
                with self.assertRaises(AgentPreflightError) as raised:
                    ObservationQuery.from_query(query)
                result = AgentGateway.error('observe', raised.exception, arguments=query)
                self.assertIsNone(result['next_action'])
                self.assertEqual(result['error']['example']['target'], query['target'])
                self.assertEqual(result['error']['example']['selectors'][0], query['selectors'][0])
                self.assertEqual(result['error']['example']['selectors'][1]['kind'], 'systemd')
                self.assertIn('separate observations', result['next_guidance'])
        query = {'target':'192.0.2.10','selectors':[
            {'id':'service','kind':'systemd','names':['ras.service'],'queries':['status']}]}
        with self.assertRaises(AgentPreflightError) as raised:
            ObservationQuery.from_query(query)
        result = AgentGateway.error('observe', raised.exception, arguments=query)
        self.assertEqual(result['next_action'], {'target':'192.0.2.10','selectors':[
            {'id':'service','kind':'systemd','names':['ras.service']}]})
        self.assertIn('queries', query['selectors'][0], 'correction must not mutate input')

    def test_service_access_and_scope_gaps_are_distinct_from_freshness(self):
        for gap, category in [('permission_denied','access'),('unit_not_found','scope'),
                              ('boot_changed','freshness')]:
            with self.subTest(gap=gap):
                results = _structured_results({'result':{'systemd':{'services':{
                    'requested':['ras.service'],'complete':False,'gaps':[gap]}}}},[],{})
                service = next(item for item in results if item['kind']=='systemd')
                self.assertEqual(service['status'], 'unavailable')
                detail = service['value']['gap_details'][0]
                self.assertEqual(detail['code'], gap)
                self.assertEqual(detail['category'], category)
                self.assertTrue(detail['recovery_action'])

    def test_empty_clock_values_are_not_evaluable_evidence(self):
        for delta in [{'before':None,'after':None,'elapsed_seconds':None,'comparable':False},
                      {'before':'','after':'','elapsed_seconds':None,'comparable':False}]:
            with self.subTest(delta=delta):
                results = _structured_results({'result':{'freshness':{'bmc_time_delta':delta}}},[],{'logs':'app.log'})
                clock = next(item for item in results if item['result_id']=='target-clock')
                self.assertEqual(clock['status'], 'unavailable')
                self.assertEqual(clock['gap'], 'target_clock_not_comparable')

    def test_clock_movement_is_still_reported_when_comparable(self):
        delta = {'before':'2026-10-22 01:00:00 +0000','after':'2026-10-22 00:59:00 +0000',
                 'elapsed_seconds':-60,'comparable':True,'clock_moved_backwards':True}
        results = _structured_results({'result':{'freshness':{'bmc_time_delta':delta}}},[],{'logs':'app.log'})
        clock = next(item for item in results if item['result_id']=='target-clock')
        self.assertEqual(clock['status'],'available')
        self.assertTrue(clock['value']['clock_moved_backwards'])

    def test_secret_and_contract_rejections_do_not_suggest_unchanged_retries(self):
        for value in [{'error':'secret_material_rejected','message':'secret material rejected'},
                      {'error':'AgentGatewayError','message':'execute kind must be start, respond, resume, or control'}]:
            with self.subTest(value=value):
                text = JsonRpcMcpEndpoint._human_summary('execute',value,error=True)
                self.assertNotIn('沿用同一任务 ID 重试',text)
                self.assertIn('修正',text)


if __name__ == '__main__':
    unittest.main()
