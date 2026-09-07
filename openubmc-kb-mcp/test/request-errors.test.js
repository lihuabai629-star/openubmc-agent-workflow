import test from "node:test";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { OneIdClient } from "../src/auth/oneid-client.js";
import { LightRagClient } from "../src/lightrag-client.js";
import { registerTools } from "../src/tools.js";

const config = {
  lightragUrl: "https://kb.example.test",
  userCenterUrl: "https://login.example.test",
  username: "fixture-user",
  password: "fixture-password",
  clientSecret: "fixture-client-secret",
  credentialsConfigured: true
};

async function query(t, { fetch, configured = true, cached = false }) {
  const selected = { ...config, credentialsConfigured: configured };
  const auth = new OneIdClient(selected, {
    fetch,
    tokenStore: {
      load: async () => cached
        ? { accessToken: "fixture-token", expiresAt: Date.now() + 600_000 }
        : undefined,
      save: async () => {},
      clear: async () => {}
    }
  });
  const server = new McpServer({ name: "kb-error-test", version: "1.0.0" });
  registerTools(server, new LightRagClient(selected, auth, fetch));
  const client = new Client({ name: "kb-error-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  t.after(async () => { await client.close(); await server.close(); });
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  return client.callTool({ name: "openubmc_kb_query", arguments: { query: "fan control" } });
}

test("MCP CAPTCHA failure requires human interaction without retry", async t => {
  const result = await query(t, {
    fetch: async () => Response.json({ data: { need_captcha_verification: true } })
  });
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error.code, "KB_INTERACTION_REQUIRED");
  assert.equal(result.structuredContent.error.retryable, false);
  assert.match(result.structuredContent.error.recovery, /interactive/i);
});

test("MCP classifies HTTP failures without exposing upstream authentication data", async t => {
  for (const [status, code, retryable] of [
    [401, "KB_AUTHENTICATION_FAILED", false],
    [403, "KB_PERMISSION_DENIED", false],
    [429, "KB_RATE_LIMITED", true],
    [503, "KB_SERVICE_UNAVAILABLE", true],
    [418, "KB_TOOL_FAILED", false]
  ]) {
    const result = await query(t, {
      fetch: async () => Response.json({ message: config.password, error_description: config.clientSecret }, { status })
    });
    assert.equal(result.isError, true);
    assert.equal(result.structuredContent.error.code, code);
    assert.equal(result.structuredContent.error.retryable, retryable);
    assert.ok(result.structuredContent.error.recovery);
    assert.ok(result.content[0].text.includes(result.structuredContent.error.recovery));
    assert.equal(JSON.stringify(result).includes(config.password), false);
    assert.equal(JSON.stringify(result).includes(config.clientSecret), false);
  }
});

test("MCP retries evidenced transient network failure but not unknown local errors", async t => {
  for (const [error, code, retryable] of [
    [new TypeError("fetch failed", { cause: { code: "ECONNRESET" } }), "KB_NETWORK_ERROR", true],
    [new TypeError(config.password), "KB_TOOL_FAILED", false],
    [Object.assign(new Error(config.clientSecret), { code: "KB_UNTRUSTED_MESSAGE" }), "KB_TOOL_FAILED", false],
    [Object.assign(new Error(config.password), { status: "constructor" }), "KB_TOOL_FAILED", false]
  ]) {
    const result = await query(t, { fetch: async () => { throw error; } });
    assert.equal(result.structuredContent.error.code, code);
    assert.equal(result.structuredContent.error.retryable, retryable);
    assert.equal(JSON.stringify(result).includes(config.password), false);
    assert.equal(JSON.stringify(result).includes(config.clientSecret), false);
  }
});

test("MCP reports denied KB access without repeating authentication", async t => {
  const requests = [];
  const result = await query(t, {
    cached: true,
    fetch: async url => { requests.push(String(url)); return Response.json({}, { status: 403 }); }
  });
  assert.equal(result.structuredContent.error.code, "KB_PERMISSION_DENIED");
  assert.deepEqual(requests, ["https://kb.example.test/api/v1/rag/retrieve"]);
});

test("MCP missing credentials direct the caller to local configuration without network access", async t => {
  const result = await query(t, {
    configured: false,
    fetch: async () => { assert.fail("missing credentials must not cause a network request"); }
  });
  assert.equal(result.structuredContent.error.code, "KB_CREDENTIALS_MISSING");
  assert.equal(result.structuredContent.error.retryable, false);
  assert.match(result.structuredContent.error.recovery, /local private/);
});
