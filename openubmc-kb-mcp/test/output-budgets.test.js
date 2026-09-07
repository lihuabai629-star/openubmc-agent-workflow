import test from "node:test";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { OneIdClient } from "../src/auth/oneid-client.js";
import { LightRagClient } from "../src/lightrag-client.js";
import { registerTools } from "../src/tools.js";

async function connect(t, fetch, { cached = true } = {}) {
  const config = { lightragUrl: "https://kb.example.test", userCenterUrl: "https://login.example.test", username: "fixture", password: "fixture", clientSecret: "fixture", credentialsConfigured: true };
  const auth = new OneIdClient(config, { fetch, tokenStore: {
    load: async () => cached ? ({ accessToken: "fixture-token", expiresAt: Date.now() + 600_000 }) : undefined,
    save: async () => {}, clear: async () => {}
  } });
  const server = new McpServer({ name: "kb-budget-test", version: "1" });
  registerTools(server, new LightRagClient(config, auth, fetch));
  const client = new Client({ name: "kb-budget-test", version: "1" });
  const [ct, st] = InMemoryTransport.createLinkedPair();
  t.after(async () => { await client.close(); await server.close(); });
  await server.connect(st); await client.connect(ct);
  return client;
}

test("MCP query bounds the complete Unicode receipt including references and metadata", async t => {
  const client = await connect(t, async () => Response.json({
    response: "回答", metadata: { unknown: "秘密".repeat(20000) },
    references: Array.from({ length: 100 }, (_, i) => ({ reference_id: String(i), file_path: "文档".repeat(500) }))
  }));
  for (const response_format of ["json", "markdown"]) {
    const result = await client.callTool({ name: "openubmc_kb_query", arguments: { query: "fan", response_format } });
    assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
    assert.equal(result.structuredContent.result.response, "回答");
    assert.equal(result.structuredContent.result.truncated, true);
    assert.ok(result.structuredContent.result.truncation_reasons.includes("fields_omitted"));
    assert.ok(result.structuredContent.result.references.length > 0);
    assert.equal(result.structuredContent.result.metadata, undefined);
    assert.equal(JSON.stringify(result).includes("\ufffd"), false);
  }
});

test("MCP bounds status/list fields and reports truthful complete small results", async t => {
  const client = await connect(t, async url => {
    if (String(url).endsWith("pipeline_status")) return Response.json({ busy: false, history_messages: ["ready"] });
    if (String(url).endsWith("status_counts")) return Response.json({ status_counts: { processed: 4, failed: 0 } });
    return Response.json({ documents: Array.from({ length: 100 }, (_, id) => ({ id: String(id), file_path: "文件".repeat(2000), content_summary: "内容".repeat(400) })), pagination: { page: 1, total_pages: 2, total_count: 200 }, metadata: "x".repeat(100000) });
  });
  const status = await client.callTool({ name: "openubmc_kb_status", arguments: {} });
  assert.equal(status.structuredContent.result.truncated, false);
  assert.equal(status.structuredContent.result.counts.status_counts.processed, 4);
  for (const response_format of ["json", "markdown"]) {
    const result = await client.callTool({ name: "openubmc_kb_list", arguments: { response_format } });
    assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
    assert.equal(result.structuredContent.result.truncated, true);
    assert.equal(result.structuredContent.result.pagination.total_count, 200);
    assert.ok(result.structuredContent.result.documents.length > 0);
    assert.equal(result.structuredContent.result.metadata, undefined);
  }
});

test("MCP cancels an oversized HTTP stream before consuming its full body", async t => {
  let chunks = 0;
  let cancelled = false;
  const client = await connect(t, async () => new Response(new ReadableStream({
    pull(controller) {
      chunks += 1;
      if (chunks > 1000) controller.close();
      else controller.enqueue(new TextEncoder().encode("x".repeat(65536)));
    },
    cancel() { cancelled = true; }
  })));
  const result = await client.callTool({ name: "openubmc_kb_query", arguments: { query: "fan" } });
  assert.equal(result.structuredContent.error?.code, "KB_RESPONSE_TOO_LARGE");
  assert.equal(result.structuredContent.error.retryable, false);
  assert.equal(cancelled, true);
  assert.ok(chunks <= 35, `read ${chunks} chunks`);
});

test("MCP status bounds JSON-escaped control characters and marks markdown partial", async t => {
  const client = await connect(t, async url => Response.json(String(url).endsWith("pipeline_status")
    ? { busy: false, latest_message: "\u0001".repeat(1024), job_name: "\u0001".repeat(1024), history_messages: Array(10).fill("\u0001".repeat(1024)) }
    : { status_counts: { processed: 2 } }));
  const result = await client.callTool({ name: "openubmc_kb_status", arguments: { response_format: "markdown" } });
  assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
  assert.equal(result.structuredContent.result.truncated, true);
  assert.match(result.content[0].text, /incomplete/i);
});


test("MCP applies a smaller HTTP body budget during authentication", async t => {
  let chunks = 0;
  let cancelled = false;
  const client = await connect(t, async () => new Response(new ReadableStream({
    pull(controller) { chunks += 1; controller.enqueue(new Uint8Array(65536)); },
    cancel() { cancelled = true; }
  })), { cached: false });
  const result = await client.callTool({ name: "openubmc_kb_query", arguments: { query: "fan" } });
  assert.equal(result.structuredContent.error.code, "KB_RESPONSE_TOO_LARGE");
  assert.equal(cancelled, true);
  assert.ok(chunks <= 7);
});
