import { readFile } from "node:fs/promises";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import express from "express";
import { Backends, backendCommands } from "./backend.js";
import { createLocalPreview } from "./local-preview.js";

export function createPreviewApp({ html, backendFactory }) {
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "64kb" }));
  const local = createLocalPreview(html, backendFactory);
  app.use("/preview", local.router);
  app.use((error, _req, res, _next) => res.status(error.status === 413 ? 413 : 400).json({ error: "Invalid request." }));
  return { app, close: () => local.close() };
}

async function main() {
  const { values } = parseArgs({ options: {
    port: { type: "string", default: process.env.OPENUBMC_PANEL_PORT || "8765" },
    "plugin-root": { type: "string", default: process.env.OPENUBMC_PLUGIN_ROOT }
  } });
  const port = Number(values.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid port.");
  const commands = backendCommands(values["plugin-root"]);
  const html = await readFile(new URL("../dist/panel.html", import.meta.url), "utf8");
  const service = createPreviewApp({ html, backendFactory: () => new Backends(commands) });
  const listener = service.app.listen(port, "127.0.0.1", () => {
    process.stdout.write(`Local panel preview: http://127.0.0.1:${port}/preview\n`);
  });
  listener.on("error", () => { process.stderr.write("Could not bind the local preview port.\n"); process.exitCode = 1; });
  async function shutdown() {
    listener.close();
    await service.close();
    listener.closeAllConnections();
  }
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(() => {
  process.stderr.write("Local preview startup failed. Check the plugin root, port, and built panel.\n");
  process.exitCode = 1;
});
