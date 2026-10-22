import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { createMcpServer } from "../src/mcp.js";
import { PANEL_URI, PANEL_ICON, PANEL_READ_TOOL } from "../src/panel.js";
import { fileURLToPath } from "node:url";

test("Codex native discovery and opening never start backends; reads connect on demand", async t => {
  const calls = [];
  const backends = {
    async tools(kind) {
      calls.push(kind);
      return (kind === "kb" ? ["openubmc_kb_query"] : ["case_read", "runtime_status"])
        .map(name => ({ name, inputSchema: { type: "object" } }));
    },
    status() { return { kb: "connected" }; },
    async call(kind, name, args) { return { content: [], structuredContent: { kind, name, args } }; }
  };
  const server = createMcpServer(backends, "<!doctype html><title>openUBMC</title>");
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "native-test", version: "1" });
  t.after(async () => { await client.close(); await server.close(); });
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  const { tools } = await client.listTools();
  assert.deepEqual(calls, []);
  assert.deepEqual(tools.map(tool => tool.name), ["openubmc_panel", PANEL_READ_TOOL]);
  const opener = tools.find(tool => tool.name === "openubmc_panel");
  assert.equal(opener.title, "openUBMC");
  assert.equal(opener.icons[0].src, PANEL_ICON);
  assert.deepEqual(opener._meta["openai/ui"].entrypoints, [{ type: "global" }]);
  assert.equal(opener._meta.ui.resourceUri, PANEL_URI);
  assert.equal(opener.securitySchemes, undefined);
  assert.equal(opener._meta.securitySchemes, undefined);
  assert.equal(tools.filter(tool => tool._meta["openai/ui"]).length, 1);
  assert.ok(!tools.some(tool => ["execute", "observe", "evidence_attach"].includes(tool.name)));
  assert.deepEqual(tools.find(tool => tool.name === PANEL_READ_TOOL)._meta.ui.visibility, ["app"]);
  const panel = await client.callTool({ name: "openubmc_panel", arguments: {} });
  assert.equal(panel.isError, undefined);
  assert.equal(panel.structuredContent.kind, "panel");
  await client.readResource({ uri: PANEL_URI });
  assert.deepEqual(calls, []);
  const query = { query: "Scanner", only_need_context: false };
  const answer = await client.callTool({ name: PANEL_READ_TOOL, arguments: { name: "openubmc_kb_query", arguments: query } });
  assert.deepEqual(answer.structuredContent.args, query);
  assert.deepEqual(calls, ["kb"]);
  assert.equal(answer._meta["openubmc/panel"].tool, "openubmc_kb_query");
  for (const name of ["execute", "observe", "evidence_attach", "unknown"]) {
    await assert.rejects(client.callTool({ name: PANEL_READ_TOOL, arguments: { name, arguments: {} } }));
  }
  await assert.rejects(client.callTool({ name: PANEL_READ_TOOL, arguments: { name: "case_read", arguments: [] } }));
  await assert.rejects(client.callTool({ name: "execute", arguments: {} }));
  assert.deepEqual(calls, ["kb"]);
});

test("native stdio starts without an HTTP server or any configured plugin", async t => {
  const client = new Client({ name: "native-stdio-test", version: "1" });
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [fileURLToPath(new URL("../src/native.js", import.meta.url))],
    env: { ...process.env, OPENUBMC_PLUGIN_ROOT: "" }, stderr: "pipe" });
  t.after(() => client.close());
  await client.connect(transport);
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map(tool => tool.name), ["openubmc_panel", PANEL_READ_TOOL]);
  const resource = await client.readResource({ uri: PANEL_URI });
  const built = await readFile(new URL("../dist/panel.html", import.meta.url), "utf8");
  assert.equal(resource.contents[0].text, built.replace("</head>", `<meta name="openubmc-read-tool" content="${PANEL_READ_TOOL}"></head>`));
  assert.ok(!/<meta\b[^>]*name=["']openubmc-preview-token["']/i.test(resource.contents[0].text));
  const unavailable = await client.callTool({ name: PANEL_READ_TOOL, arguments: { name: "openubmc_kb_query", arguments: { query: "Scanner" } } });
  assert.equal(unavailable.isError, true);
  assert.equal(unavailable.structuredContent.error, "BACKEND_UNAVAILABLE");
  const reopened = await client.callTool({ name: "openubmc_panel", arguments: {} });
  assert.equal(reopened.isError, undefined);
});
