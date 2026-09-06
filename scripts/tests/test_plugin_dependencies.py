"""Dependency recovery through the relocated plugin's public CLI (no network)."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DependencyPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.plugin = self.root/'plugin'
        files = {'.codex-plugin/plugin.json': b'{"name":"openubmc","version":"0.0.0"}',
                 'requirements.lock': b'',
                 'openubmc-kb-mcp/package.json': b'{"name":"fixture","version":"0.0.0"}',
                 'openubmc-kb-mcp/package-lock.json': b'{}',
                 'scripts/launch_runtime.py': b'print("MCP_FIXTURE_READY", flush=True)\n',
                 'scripts/pluginctl.py': (ROOT/'plugin/openubmc/scripts/pluginctl.py').read_bytes()}
        for name, data in files.items():
            path = self.plugin/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        def canonical(value):
            return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)+'\n').encode()
        lock = {'schema':'openubmc.codex-plugin.v1','name':'openubmc','version':'0.0.0', 'source_commit':'a'*40, 'skills':[],
                'manifest_digest':hashlib.sha256(files['.codex-plugin/plugin.json']).hexdigest(),
                'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
        lock['content_digest'] = hashlib.sha256(canonical(lock)).hexdigest()
        (self.plugin/'plugin-lock.json').write_bytes(canonical(lock))
        bin_dir = self.root/'bin'; bin_dir.mkdir()
        npm = bin_dir/'npm'
        npm.write_text('''#!/usr/bin/python3
import os,sys,time,subprocess,signal
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
mode=(root/'mode').read_text() if (root/'mode').exists() else 'success'
(root/'npm-args').write_text(' '.join(sys.argv[1:]))
if mode=='fail': sys.exit(19)
if mode=='hang':
 child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])
 (root/'pids').write_text(str(os.getpid())+' '+str(child.pid))
 time.sleep(120)
if mode=='retry':
 counter=root/'attempts'; n=int(counter.read_text()) if counter.exists() else 0
 counter.write_text(str(n+1))
 if n==0: sys.exit(19)
prefix=Path(sys.argv[sys.argv.index('--prefix')+1])
p=(prefix/'node_modules/fixture.js');p.parent.mkdir(parents=True,exist_ok=True);p.write_text('verified fixture')
''')
        npm.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bin_dir)+os.pathsep+os.environ['PATH'],
                        XDG_DATA_HOME=str(self.root/'data'), PIP_NO_INDEX='1', FIXTURE_ROOT=str(self.root))
        self.env['XDG_CACHE_HOME'] = str(self.root/'cache')
        self.cli = [sys.executable,'-I',str(self.plugin/'scripts/pluginctl.py'),'prepare']

    def prepare(self, *args):
        return subprocess.run([*self.cli,*args],env=self.env,capture_output=True,text=True,timeout=20)

    def mode(self, value):
        (self.root/'mode').write_text(value)

    def test_first_start_prepares_dependencies_and_warm_start_does_not_download(self):
        argv = [sys.executable, '-I', str(self.plugin/'scripts/pluginctl.py'), 'runtime', '--prepare-on-start']
        first = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, 'MCP_FIXTURE_READY\n')
        self.assertIn('"stage": "publish"', first.stderr)
        self.mode('fail')
        warm = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertEqual(warm.stdout, 'MCP_FIXTURE_READY\n')
        self.assertNotIn('"stage"', warm.stderr)
        dependency = next((self.root/'data').rglob('fixture.js'))
        dependency.write_text('unverified dependency')
        damaged = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(damaged.returncode, 0)
        self.assertEqual(damaged.stdout, '')
        self.assertIn('cache drift', damaged.stderr)

    def test_first_start_rejects_modified_package_before_dependency_download(self):
        (self.plugin/'scripts/launch_runtime.py').write_text('print("modified")\n')
        result = subprocess.run([sys.executable, '-I', str(self.plugin/'scripts/pluginctl.py'),
                                 'runtime', '--prepare-on-start'], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('inventory mismatch', result.stderr)
        self.assertFalse((self.root/'npm-args').exists())

    def test_first_start_parent_loss_stops_downloader_and_releases_cache_lock(self):
        self.mode('hang')
        child_script = 'import subprocess,sys,time; subprocess.Popen(sys.argv[1:]); time.sleep(120)'
        argv = [sys.executable, '-I', str(self.plugin/'scripts/pluginctl.py'), 'runtime', '--prepare-on-start']
        with (self.root/'parent.stdout').open('w') as stdout, (self.root/'parent.stderr').open('w') as stderr:
            parent = subprocess.Popen([sys.executable, '-c', child_script, *argv], env=self.env,
                                      stdout=stdout, stderr=stderr)
            try:
                deadline = time.monotonic()+8
                while not (self.root/'pids').exists() and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertTrue((self.root/'pids').exists())
                pids = [int(value) for value in (self.root/'pids').read_text().split()]
                parent.kill()
                parent.wait(timeout=3)
                deadline = time.monotonic()+5
                while list((self.root/'data').rglob('*.staging')) and time.monotonic()<deadline:
                    time.sleep(.05)
                for pid in pids:
                    stat = Path('/proc')/str(pid)/'stat'
                    self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')
                self.assertFalse(list((self.root/'data').rglob('*.staging')))
                self.mode('success')
                result = self.prepare('--offline', '--lock-timeout', '1')
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait()

    def test_spawning_thread_exit_does_not_cancel_a_live_clients_startup(self):
        script = '''import os,subprocess,sys,threading,time
from pathlib import Path
def spawn():
    subprocess.Popen(sys.argv[1:])
    deadline=time.monotonic()+8
    while not (Path(os.environ['FIXTURE_ROOT'])/'npm-args').exists() and time.monotonic()<deadline:
        time.sleep(.02)
t=threading.Thread(target=spawn)
t.start();t.join();time.sleep(2)
'''
        result = subprocess.run([sys.executable, '-c', script, sys.executable, '-I',
                                 str(self.plugin/'scripts/pluginctl.py'), 'runtime', '--prepare-on-start'],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'MCP_FIXTURE_READY\n')
        self.assertNotIn('cancelled', result.stderr)

    def test_failed_repair_preserves_verified_cache_and_offline_reuse(self):
        prepared=self.prepare('--offline')
        self.assertEqual(prepared.returncode,0,prepared.stderr)
        dependencies=Path(json.loads(prepared.stdout)['dependencies'])
        receipt=(dependencies/'receipt.json').read_bytes()
        self.mode('fail')
        failed=self.prepare('--repair','--offline','--retries','0')
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('npm',failed.stderr)
        self.assertEqual((dependencies/'receipt.json').read_bytes(),receipt)
        reused=self.prepare('--offline')
        self.assertEqual(reused.returncode,0,reused.stderr)
        self.assertIn('--offline',(self.root/'npm-args').read_text())
        self.assertFalse(list(dependencies.parent.glob('*.staging*')))

    def test_deadline_reaps_dependency_process_group_and_retry_converges(self):
        self.mode('hang')
        self.env['OPENUBMC_PLUGIN_PREPARE_TIMEOUT_SEC'] = '1'
        failed=self.prepare('--offline')
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('timed out',failed.stderr)
        pids=[int(value) for value in (self.root/'pids').read_text().split()]
        for pid in pids:
            status=Path('/proc')/str(pid)/'stat'
            self.assertTrue(not status.exists() or status.read_text().split()[2]=='Z', f'dependency process survived: {pid}')
        self.mode('success')
        retried=self.prepare('--offline')
        self.assertEqual(retried.returncode,0,retried.stderr)

    def test_cancelled_prepare_reaps_workers_and_cleans_uncommitted_stage(self):
        self.mode('hang')
        process=subprocess.Popen([*self.cli,'--offline'],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+5
        while not (self.root/'pids').exists() and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertTrue((self.root/'pids').exists())
        pids=[int(value) for value in (self.root/'pids').read_text().split()]
        try:
            process.terminate()
            stdout,stderr=process.communicate(timeout=5)
            self.assertNotEqual(process.returncode,0)
            self.assertIn('cancelled',stderr)
            for pid in pids:
                status=Path('/proc')/str(pid)/'stat'
                self.assertTrue(not status.exists() or status.read_text().split()[2]=='Z')
            self.assertFalse(list((self.root/'data').rglob('*.staging*')))
        finally:
            for pid in pids:
                try: os.kill(pid,signal.SIGKILL)
                except ProcessLookupError: pass
            process.kill();process.communicate()

    def test_stage_progress_and_bounded_retry_preserve_hash_install_options(self):
        self.mode('retry')
        prepared=self.prepare('--offline','--retries','1','--pip-timeout','5','--npm-timeout','2','--lock-timeout','1')
        self.assertEqual(prepared.returncode,0,prepared.stderr)
        events=[json.loads(line) for line in prepared.stderr.splitlines() if line.startswith('{')]
        self.assertEqual([e['status'] for e in events if e['stage']=='npm'],['started','failed','started','completed'])
        self.assertEqual((self.root/'attempts').read_text(),'2')
        self.assertTrue(any(e['stage']=='publish' and e['status']=='completed' for e in events))
        self.assertEqual(self.prepare('--npm-timeout','nan').returncode,2)

    def test_hard_kill_does_not_release_lock_while_installer_is_alive(self):
        self.mode('hang')
        process=subprocess.Popen([*self.cli,'--offline'],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+5
        while not (self.root/'pids').exists() and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertTrue((self.root/'pids').exists())
        pids=[int(value) for value in (self.root/'pids').read_text().split()]
        try:
            process.kill();process.wait(timeout=2)
            self.mode('success')
            waiting=self.prepare('--offline','--lock-timeout','.2')
            self.assertNotEqual(waiting.returncode,0,waiting.stdout)
            self.assertIn('lock timed out',waiting.stderr)
        finally:
            for pid in pids:
                try: os.kill(pid,signal.SIGKILL)
                except ProcessLookupError: pass
            process.kill();process.communicate()
        recovered=self.prepare('--offline','--lock-timeout','2')
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertFalse(list((self.root/'data').rglob('*.staging*')))

    def test_offline_wheels_still_require_the_locked_hash(self):
        import zipfile
        wheelhouse=self.root/'wheels';wheelhouse.mkdir()
        wheel=wheelhouse/'fixture-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel,'w') as archive:
            archive.writestr('fixture.py','VALUE = 1')
            archive.writestr('fixture-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: fixture\nVersion: 1.0\n')
            archive.writestr('fixture-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('fixture-1.0.dist-info/RECORD','')
        expected=hashlib.sha256(wheel.read_bytes()).hexdigest()
        (self.plugin/'requirements.lock').write_text('fixture==1.0 --hash=sha256:'+expected+'\n')
        lock_path=self.plugin/'plugin-lock.json';lock=json.loads(lock_path.read_text())
        lock['files']['requirements.lock']=hashlib.sha256((self.plugin/'requirements.lock').read_bytes()).hexdigest()
        lock.pop('content_digest')
        lock['content_digest']=hashlib.sha256((json.dumps(lock,sort_keys=True,indent=2,ensure_ascii=False)+'\n').encode()).hexdigest()
        lock_path.write_text(json.dumps(lock))
        self.env['PIP_FIND_LINKS']=str(wheelhouse)
        self.assertEqual(self.prepare('--offline').returncode,0)
        wheel.write_bytes(wheel.read_bytes()+b'tampered')
        failed=self.prepare('--offline','--repair')
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('HASHES',failed.stderr.upper())
        self.assertEqual(self.prepare('--offline').returncode,0)

    def test_interrupted_directory_publish_restores_previous_verified_cache(self):
        prepared=self.prepare('--offline')
        self.assertEqual(prepared.returncode,0,prepared.stderr)
        root=Path(json.loads(prepared.stdout)['dependencies']);original=(root/'receipt.json').read_bytes()
        root.rename(root.with_name(root.name+'.previous'))
        stage=root.with_name(root.name+'.staging');stage.mkdir();(stage/'partial').write_text('incomplete')
        self.mode('fail')
        recovered=self.prepare('--offline')
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertEqual((root/'receipt.json').read_bytes(),original)
        self.assertFalse(stage.exists())

    def test_startup_timings_are_separate_from_mcp_stdout(self):
        timings=self.root/'timings.jsonl'
        result=subprocess.run([*self.cli[:-1],'runtime','--timings',str(timings)],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(result.stdout,'')
        self.assertIn('Dependencies are not prepared',result.stderr)
        stages=[json.loads(line)['stage'] for line in timings.read_text().splitlines()]
        self.assertEqual(stages,['verify','dependency_identity'])

    def test_timing_output_cannot_be_created_inside_immutable_plugin(self):
        path=self.plugin/'timings.jsonl'
        result=subprocess.run([*self.cli[:-1],'verify','--timings',str(path)],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(path.exists())
        verified=subprocess.run([*self.cli[:-1],'verify'],env=self.env,capture_output=True,text=True)
        self.assertEqual(verified.returncode,0,verified.stderr)


if __name__=='__main__': unittest.main()
