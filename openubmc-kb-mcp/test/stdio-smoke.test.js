import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

test("stdio server initializes and lists all tools without authenticating", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-"));
  const configPath = join(dir, "config.json");
  await writeFile(configPath, JSON.stringify({
    lightragUrl: "https://kb.example.com",
    userCenterUrl: "https://usercenter.example.com",
    oauthBaseUrl: "https://oauth.example.com",
    clientId: "client",
    redirectUri: "openubmc://openubmc.openubmc-auth/callback",
    scopes: ["openid", "offline_access"],
    username: "user",
    password: "password"
  }));

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve("src/server.js"), "--config", configPath],
    stderr: "pipe"
  });
  let stderr = "";
  transport.stderr?.on("data", chunk => { stderr += chunk.toString(); });
  const client = new Client({ name: "smoke-test", version: "1.0.0" });
  try {
    await client.connect(transport).catch(error => {
      throw new Error(`${error.message}${stderr ? `: ${stderr.trim()}` : ""}`);
    });
    const result = await client.listTools();
    assert.deepEqual(result.tools.map(tool => tool.name), [
      "openubmc_kb_query",
      "openubmc_kb_status",
      "openubmc_kb_list"
    ]);
  } finally {
    await client.close();
  }
});

test("stdio server starts without a credential file and reports unconfigured status", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-unconfigured-"));
  const configPath = join(dir, "missing.json");
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve("src/server.js"), "--config", configPath],
    stderr: "pipe"
  });
  const client = new Client({ name: "smoke-test", version: "1.0.0" });
  try {
    await client.connect(transport);
    const result = await client.callTool({ name: "openubmc_kb_status", arguments: {} });
    assert.equal(result.structuredContent.ok, true);
    assert.equal(result.structuredContent.result.configured, false);
  } finally {
    await client.close();
  }
});
