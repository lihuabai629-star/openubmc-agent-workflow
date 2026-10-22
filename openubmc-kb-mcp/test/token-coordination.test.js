import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { FileTokenStore } from "../src/auth/token-store.js";
import { OneIdClient } from "../src/auth/oneid-client.js";
import { privateFixtureDirectory } from "../test-support/private-fixture.js";

const storeModule = new URL("../src/auth/token-store.js", import.meta.url).href;
const clientModule = new URL("../src/auth/oneid-client.js", import.meta.url).href;

function worker(code, args) {
  const child = spawn(process.execPath, ["--input-type=module", "-e", code, ...args],
    { stdio: ["pipe", "pipe", "pipe"] });
  let errors = "";
  child.stderr.on("data", chunk => { errors += chunk; });
  child.done = once(child, "close").then(([status]) => {
    assert.equal(status, 0, errors);
  });
  child.done.catch(() => {});
  return child;
}

test("two processes refresh a rotating token once and preserve the shared cache", async t => {
  const dir = await privateFixtureDirectory("openubmc-shared-token-");
  const path = join(dir, "cache");
  const calls = join(dir, "calls");
  const store = new FileTokenStore(path, "account");
  await store.save({ accessToken: "old", refreshToken: "refresh-1", expiresAt: 0 });
  const source = `
    import {appendFile} from 'node:fs/promises';
    import {once} from 'node:events';
    import {FileTokenStore} from ${JSON.stringify(storeModule)};
    import {OneIdClient} from ${JSON.stringify(clientModule)};
    const client = new OneIdClient({credentialsConfigured:true,tokenEndpoint:'https://fixture.invalid'}, {
      tokenStore:new FileTokenStore(process.argv[1],'account'),
      fetch:async(_url,options)=>{
        if (!options.body.includes('refresh_token=refresh-1')) throw Error('unexpected login');
        await appendFile(process.argv[2],'refresh\\n');
        return new Response(JSON.stringify({access_token:'fresh',refresh_token:'refresh-2',expires_in:3600}));
      }
    });
    await client.loadCachedToken();
    process.stdout.write('ready');
    await once(process.stdin,'data');
    const token = await client.getAccessToken();
    if(token !== 'fresh') throw Error('wrong token');
    process.stdin.destroy();
  `;
  const children = [worker(source, [path, calls]), worker(source, [path, calls])];
  t.after(() => children.forEach(child => child.kill()));
  await Promise.all(children.map(child => once(child.stdout, "data")));
  children.forEach(child => child.stdin.end("go"));
  await Promise.all(children.map(child => child.done));
  assert.equal(await readFile(calls, "utf8"), "refresh\n");
  assert.equal((await store.load()).refreshToken, "refresh-2");
});

test("a late 401 invalidates only the token actually rejected", async () => {
  const dir = await privateFixtureDirectory("openubmc-late-token-");
  const store = new FileTokenStore(join(dir, "cache"), "account");
  await store.save({ accessToken: "old", refreshToken: "r1", expiresAt: 0 });
  const client = new OneIdClient({ credentialsConfigured: true }, { tokenStore: store });
  await client.loadCachedToken();
  const fresh = { accessToken: "fresh", refreshToken: "r2", expiresAt: Date.now() + 3600000 };
  await store.withLock(() => store.save(fresh));
  await client.invalidateAccessToken("old");
  assert.deepEqual(await store.load(), fresh);
  assert.equal(await client.getAccessToken(), "fresh");
  await new FileTokenStore(join(dir, "cache"), "previous-account-revision").clear();
  assert.deepEqual(await store.load(), fresh);
});

test("kernel lock recovers after process death and a waiting request can cancel", async t => {
  const dir = await privateFixtureDirectory("openubmc-token-lock-");
  const path = join(dir, "cache");
  const child = worker(`
    import {FileTokenStore} from ${JSON.stringify(storeModule)};
    await new FileTokenStore(process.argv[1],'account').withLock(async()=>{
      process.stdout.write('locked');
      // A pending Promise alone cannot keep this lock-holder alive.
      process.stdin.resume();
      await new Promise(resolve=>process.stdin.on('end',resolve));
    });
  `, [path]);
  t.after(() => child.kill("SIGKILL"));
  await once(child.stdout, "data");
  const store = new FileTokenStore(path, "account");
  const abort = new AbortController();
  let entered = false;
  const pending = store.withLock(() => { entered = true; }, { signal: abort.signal });
  abort.abort(new Error("cancelled lock wait"));
  await assert.rejects(pending, /cancelled lock wait/);
  assert.equal(entered, false);
  const closed = once(child, "close");
  child.kill("SIGKILL");
  await closed;
  await store.withLock(() => { entered = true; });
  assert.equal(entered, true);
});
