import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

function configure(path, config, previous = null, active = null, activate = false) {
  return JSON.parse(execFileSync('python', ['-c', `
import json, sys
from pathlib import Path
from openubmc_target_runtime.configuration import LocalConfigurationStore
args=json.load(sys.stdin)
store=LocalConfigurationStore(Path(args['path']),kind='kb')
saved=store.save(args['config'],expected_revision=args['previous'])
if args['activate']: saved=store.activate(saved['revision'],expected_active_revision=args['active'])
print(json.dumps(saved))
`], { input: JSON.stringify({ path, config, previous, active, activate }),
    env: { ...process.env, PYTHONPATH: resolve('../openubmc-target-runtime') }, encoding: 'utf8' }));
}

async function setup(t) {
  const dir = await mkdtemp(join(tmpdir(), 'openubmc-activation-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const path = join(dir, 'kb.json');
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [resolve('src/server.js'), '--config', path], stderr: 'pipe',
    env: { ...process.env, HOME: dir, XDG_CONFIG_HOME: dir,
      OPENUBMC_KB_USERNAME: '', OPENUBMC_KB_PASSWORD: '', OPENUBMC_KB_CLIENT_SECRET: '',
      OPENUBMC_MCP_TOKEN_CACHE: join(dir, 'token.json'), OPENUBMC_MCP_FORMAL_RUN: '0',
      OPENUBMC_MCP_PARENT_PID: String(process.pid), OPENUBMC_MCP_LIFECYCLE_DIR: join(dir, 'lifecycle') } });
  const client = new Client({ name: 'activation-test', version: '1.0.0' });
  t.after(() => client.close());
  await client.connect(transport);
  return { path, client, dir };
}

test('live stdio KB uses only activated configurations and pins in-flight account', { timeout: 12000 }, async t => {
  const { path, client } = await setup(t);
  const accounts = [];
  let release, started;
  const pendingResponse = new Promise(r => { release = r; });
  const requestStarted = new Promise(r => { started = r; });
  const http = createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk;
    accounts.push(JSON.parse(body).account);
    if (accounts.length === 1) { started(); await pendingResponse; }
    response.end(JSON.stringify({ data: { need_captcha_verification: true } }));
  });
  http.listen(0, '127.0.0.1'); await once(http, 'listening');
  t.after(() => { release(); http.closeAllConnections(); http.close(); });
  const base = `http://127.0.0.1:${http.address().port}`;
  const config = username => ({ username, password: 'fixture-password', clientSecret: 'fixture-secret',
    lightragUrl: base, userCenterUrl: base, oauthBaseUrl: base, requestTimeoutMs: 4000 });
  const query = () => client.callTool({ name: 'openubmc_kb_query', arguments: { query: 'fan' } });
  assert.equal((await query()).structuredContent.error.code, 'KB_CREDENTIALS_MISSING');
  const saved = configure(path, config('first'));
  assert.equal((await query()).structuredContent.error.code, 'KB_CREDENTIALS_MISSING');
  const active = configure(path, config('first'), saved.revision, null, true);
  const pending = query();
  const firstOutcome = await Promise.race([requestStarted.then(() => 'started'), pending.then(result => result.structuredContent)]);
  assert.equal(firstOutcome, 'started');
  configure(path, config('second'), active.revision, active.revision, true);
  const second = await query();
  assert.equal(second.structuredContent.error.code, 'KB_INTERACTION_REQUIRED');
  release();
  assert.equal((await pending).structuredContent.error.code, 'KB_INTERACTION_REQUIRED');
  assert.deepEqual(accounts, ['first', 'second']);
});

test('activated endpoint cannot receive a token cached for the previous endpoint', { timeout: 10000 }, async t => {
  const { FileTokenStore, createTokenOwner } = await import('../src/auth/token-store.js');
  const { loadConfig } = await import('../src/config.js');
  const { path, client, dir } = await setup(t);
  const tokens = [];
  const http = createServer(async (request, response) => {
    tokens.push({ url: request.url, auth: request.headers.authorization || '' });
    response.setHeader('content-type', 'application/json');
    response.end(request.url.includes('checkLogin')
      ? JSON.stringify({ data: { need_captcha_verification: true } })
      : JSON.stringify({ response: 'fixture context', references: [] }));
  });
  http.listen(0, '127.0.0.1'); await once(http, 'listening');
  t.after(() => { http.closeAllConnections(); http.close(); });
  const base = `http://127.0.0.1:${http.address().port}`;
  const config = { username: 'fixture', password: 'fixture-password', clientSecret: 'fixture-secret',
    lightragUrl: base + '/first', userCenterUrl: base, oauthBaseUrl: base };
  const active = configure(path, config, null, null, true);
  const loaded = await loadConfig(path);
  await new FileTokenStore(join(dir, 'token.json'), createTokenOwner(loaded)).save({ accessToken: 'fixture-first-token', expiresAt: Date.now() + 3600000 });
  const query = () => client.callTool({ name: 'openubmc_kb_query', arguments: { query: 'fan' } });
  assert.equal((await query()).structuredContent.ok, true);
  configure(path, { ...config, lightragUrl: base + '/second' }, active.revision, active.revision, true);
  const result = await query();
  assert.equal(result.structuredContent.error?.code, 'KB_INTERACTION_REQUIRED');
  assert.equal(tokens.some(item => item.url.includes('/second') && item.auth.includes('fixture-first-token')), false);
  assert.equal(tokens[0].auth, 'Bearer fixture-first-token');
});
