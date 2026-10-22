import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { once } from "node:events";
import { request as httpRequest } from "node:http";
import { test } from "node:test";
import { createPreviewApp } from "../src/server.js";
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
  const instance = createPreviewApp({
    html: "<!doctype html><title>openUBMC</title>",
    backendFactory: () => { const backend = new FakeBackends(); backends.push(backend); return backend; } });
  const http = instance.app.listen(0, "127.0.0.1");
  await once(http, "listening");
  const url = `http://127.0.0.1:${http.address().port}`;
  t.after(async () => { await instance.close(); http.closeAllConnections(); await new Promise(resolve => http.close(resolve)); });
  return { ...instance, url, backends };
}
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

test("public MCP and OAuth endpoints are absent", async t => {
  const f = await fixture(t);
  for (const route of ["/mcp", "/login", "/authorize", "/token", "/register", "/.well-known/oauth-protected-resource/mcp"]) {
    assert.equal((await fetch(f.url + route)).status, 404);
  }
  assert.equal(await requestWithHost(`${f.url}/preview`, "untrusted.example"), 403);
  assert.equal(f.backends.length, 0);
});

test("the built widget is self-contained", async () => {
  const html = await readFile(new URL("../dist/panel.html", import.meta.url), "utf8");
  assert.ok(!html.includes("<!--SCRIPT-->"));
  assert.ok(!html.includes("<!--STYLE-->"));
  assert.ok(!/<script[^>]+src=/.test(html));
  assert.ok(!/<link[^>]+href=["']https?:/.test(html));
});
