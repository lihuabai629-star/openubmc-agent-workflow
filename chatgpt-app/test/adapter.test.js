import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { once } from "node:events";
import { request as httpRequest } from "node:http";
import { test } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { createApp } from "../src/server.js";
import { PANEL_URI, LEGACY_PANEL_URI, presentResult } from "../src/panel.js";

const KEY = randomBytes(32).toString("base64url");
const schema = { type: "object", properties: {}, additionalProperties: true };
const descriptor = name => ({ name, inputSchema: schema });
function requestWithHost(url, host) {
  return new Promise((resolve, reject) => {
    const request = httpRequest(url, { headers: { Host: host } }, response => {
      response.resume(); resolve(response.statusCode);
    });
    request.on("error", reject); request.end();
  });
}
class FakeBackends {
  constructor() { this.calls = []; this.closed = false; }
  async tools(kind) {
    return ({ runtime: ["observe", "execute"], operator: ["runtime_status", "case_read"], kb: ["openubmc_kb_query"] })[kind].map(descriptor);
  }
  status() { return { runtime: "connected", operator: "connected", kb: "connected" }; }
  async call(kind, name, args, options) {
    this.calls.push({ kind, name, args, options });
    return { content: [{ type: "text", text: "Current Gate" }],
      structuredContent: { state: "gate", run_id: args.run_id, gate: { gate_id: "g-1", name: "developer.change" }, next_action: null },
      _meta: { "openubmc/host-continuity": { identity: "preserve-me" } } };
  }
  async close() { this.closed = true; }
}

async function fixture(t) {
  const backends = [];
  const instance = await createApp({ origin: "http://127.0.0.1:8765", key: KEY,
    html: "<!doctype html><title>openUBMC</title>", preview: true,
    backendFactory: () => { const backend = new FakeBackends(); backends.push(backend); return backend; } });
  const http = instance.app.listen(0, "127.0.0.1");
  await once(http, "listening");
  const url = `http://127.0.0.1:${http.address().port}`;
  t.after(async () => { await instance.close(); http.closeAllConnections(); await new Promise(resolve => http.close(resolve)); });
  return { ...instance, url, backends };
}
async function client(t, fixture, clientId = "chatgpt-client") {
  const token = fixture.provider.issue(clientId).access_token;
  const transport = new StreamableHTTPClientTransport(new URL(`${fixture.url}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${token}` } }
  });
  const instance = new Client({ name: "adapter-test", version: "1" });
  await instance.connect(transport);
  t.after(async () => { await transport.terminateSession().catch(() => {}); await instance.close(); });
  return { instance, transport };
}

test("HTTP discovery requires OAuth and rejects foreign origins and hosts", async t => {
  const f = await fixture(t);
  const response = await fetch(`${f.url}/mcp`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  assert.equal(response.status, 401);
  assert.match(response.headers.get("www-authenticate"), /oauth-protected-resource/);
  const meta = await (await fetch(`${f.url}/.well-known/oauth-protected-resource/mcp`)).json();
  assert.equal(meta.resource, "http://127.0.0.1:8765/mcp");
  assert.equal((await fetch(`${f.url}/healthz`, { headers: { Origin: "https://untrusted.example" } })).status, 403);
  assert.equal(await requestWithHost(`${f.url}/healthz`, "untrusted.example"), 403);
  assert.equal((await fetch(`${f.url}/preview`, { headers: { Host: "127.0.0.1:8765" } })).status, 200);
  assert.equal(f.backends.length, 0, "discovery and preview do not start a backend");
});

test("local panel submits knowledge queries without a ChatGPT host and only permits protected reads", async t => {
  const f = await fixture(t);
  const html = await (await fetch(`${f.url}/preview`)).text();
  const token = html.match(/name="openubmc-preview-token" content="([^"]+)"/)[1];
  const submit = (name, args = {}, headers = {}) => fetch(`${f.url}/preview/call`, {
    method: "POST", headers: { "Content-Type": "application/json", "x-openubmc-preview-token": token, ...headers },
    body: JSON.stringify({ name, arguments: args })
  });
  assert.equal(f.backends.length, 0);
  assert.equal((await submit("openubmc_kb_query", {}, { "x-openubmc-preview-token": "wrong" })).status, 403);
  assert.equal((await submit("openubmc_kb_query", {}, { Origin: "https://chatgpt.com" })).status, 403);
  assert.equal((await submit("execute")).status, 403);
  assert.equal((await submit("observe")).status, 403);
  assert.equal((await submit("evidence_attach")).status, 403);
  assert.equal((await submit("openubmc_kb_query", "invalid")).status, 400);
  assert.equal(f.backends.length, 0, "rejected requests never start the plugin");
  const args = { query: "温度传感器未上报如何排查" };
  const response = await submit("openubmc_kb_query", args, { Origin: f.url });
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result._meta["openubmc/panel"].tool, "openubmc_kb_query");
  assert.equal(f.backends[0].calls[0].kind, "kb");
  assert.deepEqual(f.backends[0].calls[0].args, args);
  await f.close();
  assert.equal(f.backends[0].closed, true);
});

test("local knowledge failures return a usable error without replay or private diagnostics", async t => {
  const f = await fixture(t);
  const token = (await (await fetch(`${f.url}/preview`)).text()).match(/name="openubmc-preview-token" content="([^"]+)"/)[1];
  const submit = () => fetch(`${f.url}/preview/call`, { method: "POST",
    headers: { "Content-Type": "application/json", "x-openubmc-preview-token": token },
    body: JSON.stringify({ name: "openubmc_kb_query", arguments: { query: "openUBMC" } }) });
  await submit();
  let calls = 0;
  f.backends[0].call = async () => { calls++; throw new Error(`private ${KEY}`); };
  const result = await (await submit()).json();
  assert.equal(calls, 1);
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.retry_performed, false);
  assert.match(result.content[0].text, /知识库服务/);
  assert.ok(!JSON.stringify(result).includes(KEY));
});

test("OAuth login, PKCE, one-time codes, audience binding and refresh rotation", async t => {
  const f = await fixture(t);
  const request = async (route, data) => fetch(`${f.url}${route}`, { method: "POST", redirect: "manual",
    headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(data) });
  const registration = await fetch(`${f.url}/register`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
    client_name: "ChatGPT", redirect_uris: ["http://127.0.0.1/callback"], token_endpoint_auth_method: "none",
    grant_types: ["authorization_code", "refresh_token"], response_types: ["code"]
  }) });
  assert.equal(registration.status, 201);
  const registered = await registration.json();
  const verifier = randomBytes(32).toString("base64url");
  const query = new URLSearchParams({ client_id: registered.client_id, response_type: "code", redirect_uri: "http://127.0.0.1/callback",
    code_challenge: createHash("sha256").update(verifier).digest("base64url"), code_challenge_method: "S256", scope: "openubmc", state: "keep-state", resource: "http://127.0.0.1:8765/mcp" });
  const authorization = await fetch(`${f.url}/authorize?${query}`, { redirect: "manual" });
  assert.equal(authorization.status, 302);
  const loginUrl = new URL(authorization.headers.get("location"), f.url);
  const requestId = loginUrl.searchParams.get("request");
  const page = await (await fetch(loginUrl)).text();
  assert.ok(!page.includes(KEY));
  assert.equal((await request("/login", { request: requestId, key: "wrong" })).status, 403);
  const login = await request("/login", { request: requestId, key: KEY });
  assert.equal(login.status, 302);
  const callback = new URL(login.headers.get("location"));
  assert.equal(callback.searchParams.get("state"), "keep-state");
  const tokenArgs = { grant_type: "authorization_code", client_id: registered.client_id, code: callback.searchParams.get("code"),
    redirect_uri: "http://127.0.0.1/callback", code_verifier: verifier, resource: "http://127.0.0.1:8765/mcp" };
  assert.equal((await request("/token", { ...tokenArgs, code_verifier: "wrong" })).status, 400);
  assert.equal((await request("/token", { ...tokenArgs, resource: "https://untrusted.example/mcp" })).status, 400);
  const response = await request("/token", tokenArgs);
  assert.equal(response.status, 200);
  const tokens = await response.json();
  assert.equal((await request("/token", tokenArgs)).status, 400, "authorization code is single-use");
  assert.equal((await request("/login", { request: requestId, key: KEY })).status, 400, "consent is single-use");
  const info = await f.provider.verifyAccessToken(tokens.access_token);
  assert.equal(info.clientId, registered.client_id);
  const refresh = { grant_type: "refresh_token", client_id: registered.client_id, refresh_token: tokens.refresh_token };
  assert.equal((await request("/token", refresh)).status, 200);
  assert.equal((await request("/token", refresh)).status, 400, "refresh token rotates");
});

test("both resource formats, private operator tools and Runtime payloads survive HTTP", async t => {
  const f = await fixture(t);
  const c = await client(t, f);
  const { tools } = await c.instance.listTools();
  const execute = tools.find(tool => tool.name === "execute");
  assert.equal(execute._meta.ui.resourceUri, PANEL_URI);
  assert.equal(execute._meta["openai/outputTemplate"], LEGACY_PANEL_URI);
  assert.equal(execute.annotations.readOnlyHint, false);
  assert.deepEqual(tools.find(tool => tool.name === "case_read")._meta.ui.visibility, ["app"]);
  assert.equal(tools.find(tool => tool.name === "case_read")._meta["openai/visibility"], "private");
  for (const [uri, mime] of [[PANEL_URI, "text/html;profile=mcp-app"], [LEGACY_PANEL_URI, "text/html+skybridge"]]) {
    const resource = await c.instance.readResource({ uri });
    assert.equal(resource.contents[0].mimeType, mime);
    assert.match(resource.contents[0].text, /openUBMC/);
    assert.ok(!resource.contents[0].text.includes("openubmc-preview-token"));
    assert.deepEqual(resource.contents[0]._meta.ui.csp.connectDomains, []);
  }
  const args = { kind: "respond", run_id: "run-1", gate_id: "g-1", gate_version: 7, schema_digest: "d".repeat(64), submission_id: "submission-1", response: { status: "completed" } };
  const result = await c.instance.callTool({ name: "execute", arguments: args, _meta: { task_id: "spoofed" } });
  assert.deepEqual(f.backends[0].calls[0].args, args);
  assert.equal(result.structuredContent.run_id, "run-1");
  assert.equal(result.structuredContent.gate.gate_id, "g-1");
  assert.equal(result.structuredContent.next_action, null);
  assert.equal(result._meta["openubmc/host-continuity"].identity, "preserve-me");
  assert.equal(result._meta["openubmc/panel"].tool, "execute");
  await assert.rejects(c.instance.callTool({ name: "evidence_attach", arguments: {} }));
});

test("MCP sessions isolate clients and close their backends", async t => {
  const f = await fixture(t);
  const a = await client(t, f, "client-a");
  const b = await client(t, f, "client-b");
  assert.notEqual(a.transport.sessionId, b.transport.sessionId);
  const tokenB = f.provider.issue("client-b").access_token;
  const response = await fetch(`${f.url}/mcp`, { method: "POST", headers: {
    Authorization: `Bearer ${tokenB}`, "Content-Type": "application/json", Accept: "application/json, text/event-stream",
    "mcp-session-id": a.transport.sessionId, "mcp-protocol-version": "2025-06-18"
  }, body: JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }) });
  assert.equal(response.status, 403);
  await a.transport.terminateSession();
  assert.equal(f.backends[0].closed, true);
  assert.equal(f.backends[1].closed, false);
});

test("backend failure never replays an operation or claims completion", async t => {
  const f = await fixture(t), c = await client(t, f);
  let calls = 0;
  f.backends[0].call = async () => { calls++; throw new Error(`private diagnostic ${KEY}`); };
  const result = await c.instance.callTool({ name: "execute", arguments: { kind: "resume", run_id: "run-1" } });
  assert.equal(calls, 1);
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.retry_performed, false);
  assert.ok(!JSON.stringify(result).includes(KEY));
  assert.equal(presentResult(result).completed, false);
});

test("display projection requires an explicit successful Outcome", () => {
  assert.equal(presentResult({ structuredContent: { state: "completed" } }).completed, false);
  assert.equal(presentResult({ structuredContent: { state: "completed", outcome: { status: "completed" } } }).completed, true);
  assert.equal(presentResult({ isError: true, structuredContent: { state: "completed", outcome: { status: "completed" } } }).completed, false);
  assert.equal(presentResult({ structuredContent: { status: "connected" } }).completed, false);
  const projection = presentResult({ structuredContent: {
    case_id: "run-read", status: "open", current_turn: { state: "gate" },
    current_gate: { name: "developer.change" }, evidence_refs: [{ evidence_id: "ev-1" }]
  } });
  assert.equal(projection.runId, "run-read");
  assert.equal(projection.state, "gate");
  assert.equal(projection.gate, "developer.change");
  assert.equal(projection.evidence[0].evidence_id, "ev-1");
  assert.equal(presentResult({ structuredContent: { status: "terminal", run_outcome: { status: "completed" } } }).completed, true);
});

test("public mode requires HTTPS and the preview is not served on the public host", async t => {
  await assert.rejects(createApp({ origin: "http://example.com", key: KEY }), /HTTPS/);
  const f = await createApp({ origin: "https://openubmc.example", key: KEY, html: "preview", preview: true, backendFactory: () => new FakeBackends() });
  const http = f.app.listen(0, "127.0.0.1"); await once(http, "listening");
  t.after(async () => { await f.close(); http.closeAllConnections(); await new Promise(resolve => http.close(resolve)); });
  const url = `http://127.0.0.1:${http.address().port}`;
  assert.equal(await requestWithHost(`${url}/preview`, "openubmc.example"), 403);
  const registration = await fetch(`${url}/register`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ redirect_uris: ["https://evil.example/callback"] }) });
  assert.equal(registration.status, 400);
});

test("the built widget is self-contained", async () => {
  const html = await readFile(new URL("../dist/panel.html", import.meta.url), "utf8");
  assert.ok(!html.includes("<!--SCRIPT-->"));
  assert.ok(!html.includes("<!--STYLE-->"));
  assert.ok(!/<script[^>]+src=/.test(html));
  assert.ok(!/<link[^>]+href=["']https?:/.test(html));
});
