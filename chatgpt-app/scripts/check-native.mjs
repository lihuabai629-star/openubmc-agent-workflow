import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { PANEL_URI, PANEL_READ_TOOL } from "../src/panel.js";

const { values } = parseArgs({ options: {
  "plugin-root": { type: "string", default: process.env.OPENUBMC_PLUGIN_ROOT },
  query: { type: "string" }
} });
const client = new Client({ name: "openubmc-panel-check", version: "0.1.0" });
const transport = new StdioClientTransport({ command: process.execPath,
  args: [fileURLToPath(new URL("../src/native.js", import.meta.url)),
    ...(values["plugin-root"] ? ["--plugin-root", values["plugin-root"]] : [])], stderr: "ignore" });
async function check(stage, action) {
  const started = performance.now();
  const details = await action();
  process.stdout.write(JSON.stringify({ stage, ok: true, milliseconds: Math.round(performance.now() - started), ...details }) + "\n");
}
try {
  await check("initialize", () => client.connect(transport));
  await check("discovery", async () => {
    const { tools } = await client.listTools();
    assert.deepEqual(tools.map(tool => tool.name), ["openubmc_panel", PANEL_READ_TOOL]);
  });
  await check("open", async () => {
    const result = await client.callTool({ name: "openubmc_panel", arguments: {} });
    assert.ok(!result.isError);
  });
  await check("resource", async () => {
    const page = await client.readResource({ uri: PANEL_URI });
    assert.ok(page.contents[0].text.includes('name="openubmc-read-tool"'));
    return { bytes: Buffer.byteLength(page.contents[0].text) };
  });
  if (values.query) await check("knowledge", async () => {
    const answer = await client.callTool({ name: PANEL_READ_TOOL,
      arguments: { name: "openubmc_kb_query", arguments: {
        query: values.query, only_need_context: false, include_references: true
      } } }, undefined, { timeout: 180_000 });
    assert.ok(!answer.isError && answer.structuredContent?.result?.response);
    const result = answer.structuredContent.result;
    return { answerCharacters: result.response.length, sources: result.references?.length ?? 0 };
  });
} catch {
  process.stderr.write("Panel check failed. Check the built resource, plugin root, and local backend readiness.\n");
  process.exitCode = 1;
} finally {
  await client.close();
}
