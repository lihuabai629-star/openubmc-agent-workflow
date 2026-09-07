import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { OneIdClient } from "../src/auth/oneid-client.js";
import { LightRagClient } from "../src/lightrag-client.js";
import { registerTools } from "../src/tools.js";

async function connect(t, handle, { timeoutMs = 1000, token } = {}) {
  const http = createServer(handle);
  http.listen(0, "127.0.0.1");
  await once(http, "listening");
  t.after(() => { http.closeAllConnections(); http.close(); });
  const base = `http://127.0.0.1:${http.address().port}`;
  const config = {
    lightragUrl: base, userCenterUrl: base, tokenEndpoint: base + "/token",
    username: "fixture-user", password: "fixture-password", clientSecret: "fixture-secret",
    clientId: "fixture-client", credentialsConfigured: true, requestTimeoutMs: timeoutMs
  };
  const auth = new OneIdClient(config, {
    tokenStore: {
      load: async () => token || { accessToken: "fixture-token", expiresAt: Date.now() + 600_000 },
      save: async () => {}, clear: async () => {}
    }
  });
  const server = new McpServer({ name: "kb-lifetime-test", version: "1.0.0" });
  registerTools(server, new LightRagClient(config, auth));
  const client = new Client({ name: "kb-lifetime-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  t.after(async () => { await client.close(); await server.close(); });
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  return client;
}

const query = { name: "openubmc_kb_query", arguments: { query: "fan control" } };

test("MCP KB deadline bounds a slow upstream response", async t => {
  const client = await connect(t, async (_request, response) => {
    await delay(200);
    response.end(JSON.stringify({ response: "late" }));
  }, { timeoutMs: 50 });
  const result = await client.callTool(query, undefined, { timeout: 2000 });
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error.code, "KB_TIMEOUT");
});

test("MCP cancellation closes the in-flight upstream response", async t => {
  let started;
  let closed;
  const upstreamStarted = new Promise(resolve => { started = resolve; });
  const upstreamClosed = new Promise(resolve => { closed = resolve; });
  const client = await connect(t, async (_request, response) => {
    response.on("close", closed);
    response.writeHead(200, { "content-type": "application/json" });
    response.write('{"response":"');
    started();
    await delay(500);
    response.end('late"}');
  });
  const controller = new AbortController();
  const pending = client.callTool(query, undefined, { signal: controller.signal });
  const rejected = assert.rejects(pending);
  await upstreamStarted;
  controller.abort();
  await rejected;
  await Promise.race([upstreamClosed, delay(200).then(() => assert.fail("upstream remained active after cancellation"))]);
});

test("MCP status and list share a deadline through response body reads", async t => {
  const client = await connect(t, async (_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.write("{");
    await delay(200);
    response.end("}");
  }, { timeoutMs: 50 });
  for (const name of ["openubmc_kb_status", "openubmc_kb_list"]) {
    const result = await client.callTool({ name, arguments: {} }, undefined, { timeout: 2000 });
    assert.equal(result.structuredContent.error?.code, "KB_TIMEOUT");
  }
});

test("MCP cancellation stops authentication when its last caller leaves", async t => {
  let started;
  let closed;
  const upstreamStarted = new Promise(resolve => { started = resolve; });
  const upstreamClosed = new Promise(resolve => { closed = resolve; });
  const client = await connect(t, async (request, response) => {
    assert.equal(request.url, "/token");
    response.on("close", closed);
    started();
    await delay(500);
    response.end(JSON.stringify({ access_token: "refreshed", expires_in: 3600 }));
  }, { token: { refreshToken: "fixture-refresh", expiresAt: 0 } });
  const controller = new AbortController();
  const pending = client.callTool(query, undefined, { signal: controller.signal });
  const rejected = assert.rejects(pending);
  await upstreamStarted;
  controller.abort();
  await rejected;
  await Promise.race([upstreamClosed, delay(200).then(() => assert.fail("authentication continued without any callers"))]);
});

test("cancelling one MCP caller does not interrupt another caller's shared refresh", async t => {
  let started;
  const refreshStarted = new Promise(resolve => { started = resolve; });
  const requests = [];
  const client = await connect(t, async (request, response) => {
    requests.push(request.url);
    if (request.url === "/token") {
      started();
      await delay(80);
      response.end(JSON.stringify({ access_token: "refreshed", expires_in: 3600 }));
    } else {
      response.end(JSON.stringify({ response: "available" }));
    }
  }, { token: { refreshToken: "fixture-refresh", expiresAt: 0 } });
  const controller = new AbortController();
  const first = client.callTool(query, undefined, { signal: controller.signal });
  const firstRejected = assert.rejects(first);
  const second = client.callTool(query);
  await refreshStarted;
  controller.abort();
  await firstRejected;
  const result = await second;
  assert.equal(result.structuredContent.ok, true);
  assert.equal(result.structuredContent.result.response, "available");
  assert.deepEqual(requests, ["/token", "/api/v1/rag/retrieve"]);
});

test("a failed status request closes the other in-flight status response", async t => {
  let secondStarted;
  let closed;
  const started = new Promise(resolve => { secondStarted = resolve; });
  const otherClosed = new Promise(resolve => { closed = resolve; });
  const client = await connect(t, async (request, response) => {
    if (request.url.endsWith("pipeline_status")) {
      await started;
      response.writeHead(403);
      response.end("{}");
    } else {
      response.on("close", closed);
      secondStarted();
      await delay(500);
      response.end("{}");
    }
  });
  const result = await client.callTool({ name: "openubmc_kb_status", arguments: {} });
  assert.equal(result.structuredContent.error.code, "KB_PERMISSION_DENIED");
  await Promise.race([otherClosed, delay(200).then(() => assert.fail("status sibling remained active after failure"))]);
});

test("authentication and the 401 refresh retry consume the same MCP deadline", async t => {
  const requests = [];
  let refreshes = 0;
  const client = await connect(t, async (request, response) => {
    requests.push(request.url);
    if (request.url === "/token") {
      refreshes += 1;
      await delay(refreshes === 1 ? 20 : 250);
      response.end(JSON.stringify({ access_token: "refreshed", expires_in: 3600 }));
    } else {
      await delay(20);
      response.writeHead(401);
      response.end("{}");
    }
  }, { timeoutMs: 150, token: { refreshToken: "fixture-refresh", expiresAt: 0 } });
  const result = await client.callTool(query);
  assert.equal(result.structuredContent.error.code, "KB_TIMEOUT");
  assert.deepEqual(requests, ["/token", "/api/v1/rag/retrieve", "/token"]);
});
