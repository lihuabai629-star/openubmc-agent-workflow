import test from "node:test";
import assert from "node:assert/strict";

import { createTools } from "../src/tools.js";

test("defines the three openUBMC knowledge-base tools", () => {
  const tools = createTools({});
  assert.deepEqual(tools.map(tool => tool.name), [
    "openubmc_kb_query",
    "openubmc_kb_status",
    "openubmc_kb_list"
  ]);
  assert.equal(tools.every(tool => tool.annotations.readOnlyHint), true);
  assert.ok(tools[0].inputSchema.query);
  assert.ok(tools[0].outputSchema.ok);
});

test("tool handlers delegate to the LightRAG client", async () => {
  const calls = [];
  const client = {
    query: async input => { calls.push(["query", input]); return { answer: "a" }; },
    status: async () => { calls.push(["status"]); return { ready: true }; },
    list: async input => { calls.push(["list", input]); return { documents: [] }; }
  };
  const tools = createTools(client);
  const result = await tools[0].handler({ query: "BMC启动", mode: "local" });
  await tools[1].handler({});
  await tools[2].handler({ page: 1 });
  assert.deepEqual(JSON.parse(result.content[0].text), {
    ok: true,
    result: { answer: "a" }
  });
  assert.deepEqual(result.structuredContent, { ok: true, result: { answer: "a" } });
  assert.deepEqual(calls, [
    ["query", { query: "BMC启动", mode: "local" }],
    ["status"],
    ["list", { page: 1 }]
  ]);
});

test("query rejects empty text before contacting LightRAG", async () => {
  let called = false;
  const tools = createTools({ query: async () => { called = true; } });
  await assert.rejects(() => tools[0].handler({ query: "   " }), /query/);
  assert.equal(called, false);
});
