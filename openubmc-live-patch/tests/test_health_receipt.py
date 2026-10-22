from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy_live_file as deploy
from runtime_cli import _identities


HEALTHY = 'check startup status completely, total components count: 57, normal count: 57'


class HealthReceiptTests(unittest.TestCase):
    def read_logs(self, payload):
        completed = subprocess.CompletedProcess([],0,json.dumps(payload),'')
        with patch.object(deploy.subprocess,'run',return_value=completed):
            return deploy.run_tool(['python','collect_logs.py','--json'],90)

    def test_request_metadata_and_tail_truncation_do_not_affect_log_health(self):
        payload = {'request':{'grep':['StartupCheck failed','init service failed']},
                   'result':{'entries':[{'ok':True,'lines':[HEALTHY]}]},
                   'padding':'metadata ' * 1500}
        result = self.read_logs(payload)
        self.assertEqual(result['startup_status']['startup_errors_seen'],False)
        self.assertTrue(result['startup_status']['startup_normal'])

    def test_real_startup_failure_is_not_discarded(self):
        result = self.read_logs({'result':{'entries':[{'ok':True,'lines':['init service failed: fan',HEALTHY]}]}})
        self.assertTrue(result['startup_status']['startup_errors_seen'])

    def test_bracketed_plain_log_lines_are_still_supported(self):
        self.assertTrue(deploy.startup_status_from_logs('[INFO] '+HEALTHY)['startup_normal'])

    def test_collection_failure_cannot_borrow_success_from_metadata(self):
        result = self.read_logs({'request':{'example':HEALTHY},'result':{'entries':[{'ok':False,'error':'denied','lines':[]}]}})
        self.assertFalse(result['startup_status']['startup_normal'])

    def test_local_file_identity_uses_the_same_canonical_path_as_the_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory)/'one.lua'
            second = Path(directory)/'two.lua'
            first.write_text('return true\n')
            second.write_bytes(first.read_bytes())
            arguments = {'ip':'192.0.2.1','remote_path':'/opt/bmc/apps/example.lua','local_path':str(first)}
            one = _identities(arguments,action='apply',local_sha256='a'*64)
            two = _identities({**arguments,'local_path':str(second)},action='apply',local_sha256='a'*64)
            self.assertEqual(one[0],two[0])
            self.assertNotEqual(one[1],two[1])
            equivalent = _identities({**arguments,'local_path':str(first.parent/'.'/first.name)},action='apply',local_sha256='a'*64)
            self.assertEqual(one,equivalent)


if __name__ == '__main__':
    unittest.main()
