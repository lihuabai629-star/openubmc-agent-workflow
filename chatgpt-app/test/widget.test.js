import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { test } from "node:test";
import { build } from "esbuild";

// Run the actual widget script against an in-memory host and DOM.
const bundle = await build({ entryPoints: [fileURLToPath(new URL("../web/app.js", import.meta.url))],
  bundle: true, write: false, format: "iife", plugins: [{ name: "in-memory-host", setup(builder) {
    builder.onResolve({ filter: /^@modelcontextprotocol\/ext-apps$/ }, () => ({ path: "host", namespace: "test" }));
    builder.onLoad({ filter: /.*/, namespace: "test" }, () => ({ contents: "export const App = globalThis.TestHost;" }));
  } }] });
const template = await readFile(new URL("../web/panel.html", import.meta.url), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

for (const native of [true, false]) test(`${native ? "native" : "standard MCP"} widget submits a knowledge question and renders the answer and sources`, async () => {
  const elements = new Map([...template.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(([tag, id]) => [id, {
    value: "", textContent: "", hidden: /\bhidden\b/.test(tag), disabled: /\bdisabled\b/.test(tag),
    listeners: {}, classList: { add() {}, toggle() {} },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    setAttribute() {}, replaceChildren(...children) { this.children = children; }
  }]));
  const requests = [];
  class TestHost {
    async connect() {}
    getHostVersion() { return { name: "Test Codex" }; }
    getHostContext() { return { availableDisplayModes: ["fullscreen"] }; }
    async callServerTool(request) {
      requests.push(request);
      return { structuredContent: { result: { response: "MDB 提供资源对象及组件间接口。", references: [
        { reference_id: "1", file_path: "MDB 开发指南" }
      ] } }, _meta: { "openubmc/panel": { tool: "openubmc_kb_query" } } };
    }
  }
  const document = { documentElement: { dataset: {} }, getElementById: id => elements.get(id),
    querySelector: selector => native && selector.includes("openubmc-read-tool") ? { content: "openubmc_panel_read" } : null,
    createElement: () => ({ textContent: "" }) };
  vm.runInNewContext(bundle.outputFiles[0].text, { TestHost, document, window: { parent: {} } });
  await flush();
  assert.equal(elements.get("search").disabled, false);
  assert.equal(requests.length, 0, "opening the widget does not query a backend");
  elements.get("query").value = "MDB 是什么？";
  elements.get("search-form").listeners.submit({ preventDefault() {} });
  assert.equal(elements.get("search").disabled, true);
  await flush();
  assert.equal(requests.length, 1);
  const expected = { name: "openubmc_kb_query", arguments: { query: "MDB 是什么？", only_need_context: false, include_references: true } };
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0])), native ? { name: "openubmc_panel_read", arguments: expected } : expected);
  assert.equal(elements.get("kb-answer").textContent, "MDB 提供资源对象及组件间接口。");
  assert.equal(elements.get("references").children[0].textContent, "1  MDB 开发指南");
  assert.equal(elements.get("search").disabled, false);
  assert.equal(elements.get("pin").hidden, true);
});
