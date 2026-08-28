import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
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
    stderr: "pipe",
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "test-client",
      OPENUBMC_MCP_TASK_ID: "stdio-smoke",
      OPENUBMC_MCP_SESSION_ID: "stdio-smoke",
      OPENUBMC_MCP_LIFECYCLE_DIR: join(dir, "processes")
    }
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
    stderr: "pipe",
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "test-client",
      OPENUBMC_MCP_TASK_ID: "stdio-unconfigured",
      OPENUBMC_MCP_SESSION_ID: "stdio-unconfigured",
      OPENUBMC_MCP_LIFECYCLE_DIR: join(dir, "processes")
    }
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

test("stdio server records task-scoped lifecycle ownership", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-lifecycle-"));
  const lifecycleRoot = join(dir, "processes");
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve("src/server.js"), "--config", join(dir, "missing.json")],
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "codex",
      OPENUBMC_MCP_TASK_ID: "kb-lifecycle-task",
      OPENUBMC_MCP_SESSION_ID: "kb-lifecycle-session",
      OPENUBMC_MCP_PARENT_PID: String(process.pid),
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
      OPENUBMC_KB_STATE_PATH: join(dir, "kb-state")
    }
  });
  const client = new Client({ name: "smoke-test", version: "1.0.0" });
  try {
    await client.connect(transport);
    await client.listTools();
  } finally {
    await client.close();
  }

  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );
  assert.equal(records.length, 1);
  assert.equal(lifecycle.client, "codex");
  assert.equal(lifecycle.task_id, "kb-lifecycle-task");
  assert.equal(lifecycle.session_id, "kb-lifecycle-session");
  assert.equal(lifecycle.parent_pid, process.pid);
  assert.equal(lifecycle.lifecycle_state, "stopped");
  assert.equal(lifecycle.exit_reason, "stdin-closed");
});

test("stdio server exits after its recorded parent is gone", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-orphan-"));
  const lifecycleRoot = join(dir, "processes");
  const missingParentPid = Number(
    (await readFile("/proc/sys/kernel/pid_max", "utf8")).trim()
  ) + 1;
  const child = spawn(process.execPath, [
    resolve("src/server.js"),
    "--config",
    join(dir, "missing.json")
  ], {
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "codex",
      OPENUBMC_MCP_TASK_ID: "kb-orphan-task",
      OPENUBMC_MCP_SESSION_ID: "kb-orphan-session",
      OPENUBMC_MCP_PARENT_PID: String(missingParentPid),
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
      OPENUBMC_MCP_LIFECYCLE_POLL_SECONDS: "0.01"
    }
  });
  let timeout;
  const returnCode = await Promise.race([
    new Promise((resolveExit, rejectExit) => {
      child.once("exit", resolveExit);
      child.once("error", rejectExit);
    }),
    new Promise((_, rejectTimeout) => {
      timeout = setTimeout(
        () => rejectTimeout(new Error("orphan MCP did not exit")),
        3000
      );
    })
  ]).finally(() => {
    clearTimeout(timeout);
    if (child.exitCode === null) child.kill();
  });

  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );
  assert.equal(returnCode, 0);
  assert.equal(lifecycle.lifecycle_state, "stopped");
  assert.equal(lifecycle.exit_reason, "parent-exited");
});

test("stdio server exits when stdin closes before initialization", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-empty-"));
  const lifecycleRoot = join(dir, "processes");
  const child = spawn(process.execPath, [
    resolve("src/server.js"),
    "--config",
    join(dir, "missing.json")
  ], {
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "codex",
      OPENUBMC_MCP_TASK_ID: "kb-empty-task",
      OPENUBMC_MCP_SESSION_ID: "kb-empty-session",
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
      OPENUBMC_MCP_LIFECYCLE_POLL_SECONDS: "0.01"
    }
  });
  child.stdin.end();
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    child.once("exit", resolveExit);
    child.once("error", rejectExit);
  });

  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );
  assert.equal(returnCode, 0);
  assert.equal(lifecycle.lifecycle_state, "stopped");
  assert.equal(lifecycle.exit_reason, "stdin-closed");
});

test("stdio server records startup failure after lifecycle creation", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-startup-failure-"));
  const lifecycleRoot = join(dir, "processes");
  const configPath = join(dir, "invalid.json");
  await writeFile(configPath, "{not-json");
  const child = spawn(process.execPath, [
    resolve("src/server.js"),
    "--config",
    configPath
  ], {
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "codex",
      OPENUBMC_MCP_TASK_ID: "kb-startup-failure",
      OPENUBMC_MCP_SESSION_ID: "kb-startup-failure",
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot
    }
  });
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    child.once("exit", resolveExit);
    child.once("error", rejectExit);
  });

  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );
  assert.equal(returnCode, 1);
  assert.equal(lifecycle.lifecycle_state, "stopped");
  assert.equal(lifecycle.exit_reason, "startup-error");
});

test("stdio server records invalid lifecycle environment as startup-error", async () => {
  for (const [name, value] of [
    ["OPENUBMC_MCP_PARENT_PID", "1e3"],
    ["OPENUBMC_MCP_LIFECYCLE_POLL_SECONDS", "not-a-number"]
  ]) {
    const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-invalid-env-"));
    const lifecycleRoot = join(dir, "processes");
    const child = spawn(process.execPath, [
      resolve("src/server.js"),
      "--config",
      join(dir, "missing.json")
    ], {
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        OPENUBMC_MCP_CLIENT: "codex",
        OPENUBMC_MCP_TASK_ID: "kb-invalid-env",
        OPENUBMC_MCP_SESSION_ID: "kb-invalid-env",
        OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
        [name]: value
      }
    });
    const returnCode = await new Promise((resolveExit, rejectExit) => {
      child.once("exit", resolveExit);
      child.once("error", rejectExit);
    });

    const records = await readdir(lifecycleRoot);
    const lifecycle = JSON.parse(
      await readFile(join(lifecycleRoot, records[0]), "utf8")
    );
    assert.equal(returnCode, 1);
    assert.equal(lifecycle.lifecycle_state, "stopped");
    assert.equal(lifecycle.exit_reason, "startup-error");
  }
});
