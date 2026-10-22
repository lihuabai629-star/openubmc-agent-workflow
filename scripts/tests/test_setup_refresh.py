"""Exercise repeated setup status calls through the actual MCP entrypoint."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SetupRefreshTests(unittest.TestCase):
    def test_recovery_selects_the_failed_layer(self):
        script = r'''
const assert = require('node:assert/strict');
const {createSetupOperations, preparationFailure} = require(process.argv[1]);
const adapter = {decodeOutput:String,hostPlatform:'linux',packageIdentity:()=>({})};
const ops = createSetupOperations(adapter,'kb');
for (const [reason,match] of [
  ['dependency_cache_drift',/openubmc_setup_prepare/],
  ['configuration_repair_required',/openubmc_setup_repair_configuration/],
  ['package_integrity_failed',/Reinstall/],
  ['configuration_repair_conflict',/conflict/],
  ['mcp_protocol_unhealthy',/protocol|MCP/],
]) {
  const status = ops.publicStatus({reason});
  assert.match(status.next_action,match);
  if (reason !== 'dependency_cache_drift') assert.doesNotMatch(status.next_action,/openubmc_setup_prepare/);
}
assert.equal(preparationFailure(adapter,{stderr:'plugin file inventory mismatch',status:2}), 'package_integrity_failed');
'''
        result = subprocess.run(['node', '-e', script,
            str(ROOT/'plugin/openubmc/scripts/openubmc-bootstrap-operations.js')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refresh_preserves_startup_failure_and_reports_current_readiness(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {PassThrough} = require('node:stream');
const directory = process.argv[1];
async function check(capability) {
  const input = new PassThrough();
  const output = new PassThrough();
  let captured = '';
  output.on('data', chunk => {captured += chunk;});
  const reports = [false, true, false].map((ready, index) => ({
    capabilities: {[capability]: {startup_ready: index !== 2}},
    codex_configuration: {ready: true},
    mcp_health: {[capability]: {ok: ready}},
  }));
  const adapter = {hostPlatform:'linux', decodeOutput:String,
    resolveBackend:() => ({ok:true, command:'fixture',prefix:[],configurationArgs:[]}),
    packageIdentity:() => ({version:'fixture',source_commit:'a'.repeat(40),integrity:true}),
    run() {const report=reports.shift();return {status:report.mcp_health[capability].ok?0:2,
      stdout:JSON.stringify(report),stderr:''};},
  };
  const localRequire = name => name === './openubmc-bootstrap-host.js'
    ? {createHostAdapter:() => adapter}
    : require(name.startsWith('./') ? path.join(directory,name) : name);
  vm.runInNewContext(fs.readFileSync(path.join(directory,'openubmc-mcp-bootstrap.js'),'utf8'), {
    require:localRequire,__dirname:directory,Buffer,
    process:{argv:['node','bootstrap',capability],env:{},platform:'linux',stdin:input,
      stdout:output,stderr:new PassThrough()},
  });
  input.end([1,2].map(id => JSON.stringify({jsonrpc:'2.0',id,method:'tools/call',
    params:{name:'openubmc_setup_status',arguments:{}}})).join('\n')+'\n');
  await new Promise(resolve => setImmediate(resolve));
  const statuses = captured.trim().split('\n').map(line => JSON.parse(line).result.structuredContent);
  assert.equal(statuses.length,2);
  assert.equal(statuses[0].startup_reason,'mcp_protocol_unhealthy');
  assert.equal(statuses[0].reason,'mcp_protocol_unhealthy');
  assert.equal(statuses[0].backend_ready_for_new_task,true);
  assert.match(statuses[0].recovery_action,/new.*task/i);
  assert.equal(statuses[1].startup_reason,'mcp_protocol_unhealthy');
  assert.equal(statuses[1].reason,'dependency_preflight_failed');
  assert.equal(statuses[1].backend_ready_for_new_task,false);
}
(async()=>{for(const capability of ['runtime','kb'])await check(capability);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node','-e',script,
            str(ROOT/'plugin/openubmc/scripts')],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
