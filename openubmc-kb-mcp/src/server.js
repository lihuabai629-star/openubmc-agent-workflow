#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { pathToFileURL } from "node:url";

import { OneIdClient } from "./auth/oneid-client.js";
import { loadConfig } from "./config.js";
import { LightRagClient } from "./lightrag-client.js";
import { registerTools } from "./tools.js";


function configPath(argv) {
  const index = argv.indexOf("--config");
  return index >= 0 ? argv[index + 1] : undefined;
}


export async function createServer(path) {
  const config = await loadConfig(path, { allowMissingCredentials: true });
  const auth = new OneIdClient(config);
  const lightrag = new LightRagClient(config, auth);
  const server = new McpServer(
    { name: "openubmc-kb-mcp-server", version: "1.3.0" },
    {
      instructions: "Use the read-only openUBMC knowledge-base tools for candidate discovery. Runtime and repository evidence remain authoritative."
    }
  );
  registerTools(server, lightrag);
  return server;
}


async function main() {
  const server = await createServer(configPath(process.argv.slice(2)));
  const transport = new StdioServerTransport();
  transport.onerror = error => {
    console.error(error instanceof Error ? error.message : "MCP stdio transport error");
  };
  await server.connect(transport);
}


if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
