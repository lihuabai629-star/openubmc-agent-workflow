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
    def test_startup_failure_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);plugin=root/'plugin';plugin.mkdir()
            (plugin/'plugin-lock.json').write_text(json.dumps({'source_commit':'a'*40,'content_digest':'b'*64,'version':'fixture'}))
            output=root/'result.json'
            process=subprocess.run([sys.executable,str(SCRIPT),'--plugin',str(plugin),'--output',str(output)],
                                   env=dict(os.environ,PATH=''),capture_output=True,timeout=5)
            self.assertNotEqual(process.returncode,0)
            report=json.loads(output.read_text())
            self.assertFalse(report['ok'])
            self.assertEqual(report['failure_stage'],'preflight')

    def test_cancellation_retains_probe_and_window_history(self):
        for phase in ('probe','window'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);plugin=root/'plugin';(plugin/'scripts').mkdir(parents=True)
                (plugin/'plugin-lock.json').write_text(json.dumps({'source_commit':'a'*40,'content_digest':'b'*64,'version':'fixture'}))
                (plugin/'scripts/pluginctl.py').write_text('''import json,sys,time,os
from pathlib import Path
if sys.argv[1]=='prepare':sys.exit(0)
Path(os.environ['FIXTURE_READY']).write_text(str(os.getpid()))
if os.environ['FIXTURE_PHASE']=='probe':time.sleep(120)
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request:continue
 method=request['method']
 if method=='initialize':result={}
 elif method=='tools/list':result={'tools':[{'name':x} for x in (['observe','execute'] if sys.argv[1]=='runtime' else ['openubmc_kb_query','openubmc_kb_status','openubmc_kb_list'])]}
 else:result={'isError':True}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
if sys.argv[1]=='kb':Path(os.environ['FIXTURE_READY']).write_text('window')
''')
                ready=root/'ready';output=root/'result.json'
                process=subprocess.Popen([sys.executable,str(SCRIPT),'--plugin',str(plugin),'--output',str(output),'--interval','60'],
                                         env=dict(os.environ,FIXTURE_READY=str(ready),FIXTURE_PHASE=phase),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                worker=None
                try:
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        value=ready.read_text() if ready.exists() else ''
                        if value and (phase=='probe' or value=='window'):break
                        time.sleep(.02)
                    self.assertTrue(value)
                    worker=int(value) if phase=='probe' else None
                    if phase=='window':time.sleep(.1)
                    process.terminate();process.communicate(timeout=5)
                    self.assertNotEqual(process.returncode,0)
                    report=json.loads(output.read_text())
                    self.assertFalse(report['ok'])
                    self.assertEqual(report['failure_class'],'cancelled')
                    self.assertEqual(len(report['records']),1 if phase=='probe' else 2)
                    if worker:
                        stat=Path(f'/proc/{worker}/stat')
                        self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')
                finally:
                    process.kill();process.communicate()
                    if worker:
                        try:os.kill(worker,signal.SIGKILL)
                        except ProcessLookupError:pass

    @unittest.skipUnless(Path('/proc/sys/kernel/pid_max').is_file(), 'requires Linux procfs')
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
