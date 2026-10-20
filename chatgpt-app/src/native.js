import { readFile } from "node:fs/promises";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Backends, backendCommands } from "./backend.js";
import { createMcpServer } from "./server.js";
import { PANEL_READ_TOOL } from "./panel.js";

// Native Codex navigation discovers the global entrypoint from tools/list.
// No HTTP service, browser token, or OAuth grant is involved in stdio access.
export async function startNative({ pluginRoot, transport = new StdioServerTransport() } = {}) {
  const built = await readFile(new URL("../dist/panel.html", import.meta.url), "utf8");
  const html = built.replace("</head>", `<meta name="openubmc-read-tool" content="${PANEL_READ_TOOL}"></head>`);
  const backends = new Backends(backendCommands(pluginRoot));
  const server = createMcpServer(backends, html, { native: true });
  server.onclose = () => { void backends.close(); };
  await server.connect(transport);
  return { server, backends, async close() { await server.close(); await backends.close(); } };
}

async function main() {
  const { values } = parseArgs({ options: { "plugin-root": { type: "string", default: process.env.OPENUBMC_PLUGIN_ROOT } } });
  const service = await startNative({ pluginRoot: values["plugin-root"] });
  process.once("SIGINT", () => void service.close());
  process.once("SIGTERM", () => void service.close());
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(() => {
  process.stderr.write("Native openUBMC panel startup failed. Check the plugin root and built panel.\n");
  process.exitCode = 1;
});
