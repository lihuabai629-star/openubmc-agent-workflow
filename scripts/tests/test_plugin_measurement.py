"""The measurement CLI retains cancelled preparation and stops its workers."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'plugin_measurement.py'


class PluginMeasurementTests(unittest.TestCase):
    def test_cancelled_preparation_retains_failure_and_reaps_separate_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);plugin=root/'plugin';(plugin/'scripts').mkdir(parents=True)
            (plugin/'plugin-lock.json').write_text(json.dumps({'source_commit':'a'*40,'content_digest':'b'*64,'version':'fixture'}))
            (plugin/'scripts/pluginctl.py').write_text('''import subprocess,sys,time,os
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True)
Path(os.environ['MEASUREMENT_FIXTURE_PIDS']).write_text(str(os.getpid())+' '+str(child.pid))
time.sleep(120)
''')
            pids_file=root/'pids';output=root/'result.json'
            process=subprocess.Popen([sys.executable,str(SCRIPT),'--plugin',str(plugin),'--output',str(output)],
                                     env=dict(os.environ,MEASUREMENT_FIXTURE_PIDS=str(pids_file)),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+5
                while not pids_file.exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(pids_file.exists())
                process.terminate();stdout,stderr=process.communicate(timeout=5)
                self.assertNotEqual(process.returncode,0)
                self.assertFalse(json.loads(output.read_text())['dependency_prepare_ok'])
                for pid in pids_file.read_text().split():
                    stat=Path('/proc')/pid/'stat'
                    self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')
            finally:
                if pids_file.exists():
                    for pid in pids_file.read_text().split():
                        try:os.kill(int(pid),signal.SIGKILL)
                        except ProcessLookupError:pass
                process.kill();process.communicate()


if __name__=='__main__':unittest.main()
