import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, readdir, writeFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

test("stdio SIGTERM drains a slow authentication request by its total deadline", async t => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-deadline-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  let started;
  const requestStarted = new Promise(resolveStarted => { started = resolveStarted; });
  const http = createServer(async (_request, response) => {
    started();
    await delay(400);
    response.end(JSON.stringify({ data: { need_captcha_verification: true } }));
  });
  http.listen(0, "127.0.0.1");
  await once(http, "listening");
  t.after(() => { http.closeAllConnections(); http.close(); });
  const base = `http://127.0.0.1:${http.address().port}`;
  const configPath = join(dir, "config.json");
  await writeFile(configPath, JSON.stringify({
    lightragUrl: base, userCenterUrl: base, oauthBaseUrl: base,
    username: "fixture-user", password: "fixture-password", clientSecret: "fixture-secret",
    requestTimeoutMs: 100
  }));
  const transport = new StdioClientTransport({
    command: process.execPath, args: [resolve("src/server.js"), "--config", configPath],
    stderr: "pipe",
    env: { ...process.env, OPENUBMC_KB_USERNAME: "", OPENUBMC_KB_PASSWORD: "", OPENUBMC_KB_CLIENT_SECRET: "",
      OPENUBMC_MCP_TOKEN_CACHE: join(dir, "token.json"), OPENUBMC_MCP_FORMAL_RUN: "0",
      OPENUBMC_MCP_PARENT_PID: String(process.pid), OPENUBMC_MCP_LIFECYCLE_DIR: join(dir, "lifecycle"),
      OPENUBMC_MCP_LIFECYCLE_POLL_SECONDS: "0.01" }
  });
  const client = new Client({ name: "deadline-test", version: "1.0.0" });
  t.after(() => client.close());
  await client.connect(transport);
  const stopped = new Promise(resolveStopped => { client.onclose = resolveStopped; });
  const pending = client.callTool({ name: "openubmc_kb_query", arguments: { query: "fan" } });
  await requestStarted;
  process.kill(transport.pid, "SIGTERM");
  const result = await pending;
  assert.equal(result.structuredContent.error.code, "KB_TIMEOUT");
  await Promise.race([stopped, delay(1500).then(() => assert.fail("stdio process did not drain and stop"))]);
});

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
      OPENUBMC_MCP_SOURCE_COMMIT: "a".repeat(40),
      OPENUBMC_MCP_MODEL_IDENTITY: JSON.stringify({ model: "gpt-5.6-sol" }),
      OPENUBMC_MCP_CODEX_IDENTITY: JSON.stringify({ version: "codex-cli 0.150.0" }),
      OPENUBMC_MCP_FORMAL_RUN: "1",
      OPENUBMC_MCP_PARENT_PID: String(process.pid),
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
      OPENUBMC_KB_STATE_PATH: join(dir, "kb-state"),
      OPENUBMC_TARGET_RUNTIME_STATE_DIR: join(dir, "runtime-state")
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
  assert.equal(lifecycle.source_commit, "a".repeat(40));
  assert.equal(lifecycle.formal_run, true);
  assert.deepEqual(lifecycle.model_identity, { model: "gpt-5.6-sol" });
  assert.deepEqual(lifecycle.codex_identity, { version: "codex-cli 0.150.0" });
  assert.equal(lifecycle.parent_pid, process.pid);
  assert.equal(lifecycle.parent_identity_verified, true);
  assert.equal(lifecycle.parent_identity_currently_verified, true);
  assert.equal(lifecycle.state_path, join(dir, "kb-state"));
  assert.equal(lifecycle.runtime_state_root, join(dir, "runtime-state"));
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

test("stdio server recognizes explicit task closeout notification", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-task-closeout-"));
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
      OPENUBMC_MCP_TASK_ID: "kb-task-closeout",
      OPENUBMC_MCP_SESSION_ID: "kb-task-closeout",
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
      OPENUBMC_MCP_LIFECYCLE_POLL_SECONDS: "0.01"
    }
  });
  child.stdin.write(`${JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "codex", version: "1" }
    }
  })}\n`);
  child.stdin.write(`${JSON.stringify({
    jsonrpc: "2.0",
    method: "notifications/openubmc-task-complete",
    params: {}
  })}\n`);
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    const timeout = setTimeout(
      () => rejectExit(new Error("task closeout did not stop KB MCP")),
      3000
    );
    child.once("exit", code => {
      clearTimeout(timeout);
      resolveExit(code);
    });
    child.once("error", error => {
      clearTimeout(timeout);
      rejectExit(error);
    });
  }).finally(() => {
    if (child.exitCode === null) child.kill();
  });

  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );
  assert.equal(returnCode, 0);
  assert.equal(lifecycle.active_requests, 0);
  assert.equal(lifecycle.exit_reason, "task-closeout");
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

test("stdio server records invalid identity environment as startup-error", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-invalid-identity-"));
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
      OPENUBMC_MCP_TASK_ID: "kb-invalid-identity",
      OPENUBMC_MCP_SESSION_ID: "kb-invalid-identity",
      OPENUBMC_MCP_MODEL_IDENTITY: "[]",
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot
    }
  });
  let stderr = "";
  child.stderr.on("data", chunk => { stderr += chunk.toString(); });
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    child.once("exit", resolveExit);
    child.once("error", rejectExit);
  });

  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );
  assert.equal(returnCode, 1);
  assert.match(stderr, /OPENUBMC_MCP_MODEL_IDENTITY must be a JSON object/);
  assert.equal(lifecycle.exit_reason, "startup-error");
});

test("formal stdio server rejects missing model and Codex identity", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-formal-identity-"));
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
      OPENUBMC_MCP_TASK_ID: "formal-missing-identity",
      OPENUBMC_MCP_SESSION_ID: "formal-missing-identity",
      OPENUBMC_MCP_FORMAL_RUN: "1",
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot
    }
  });
  let stderr = "";
  child.stderr.on("data", chunk => { stderr += chunk.toString(); });
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    child.once("exit", resolveExit);
    child.once("error", rejectExit);
  });
  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );

  assert.equal(returnCode, 1);
  assert.match(stderr, /formal MCP run requires model and Codex identity/);
  assert.equal(lifecycle.formal_run, true);
  assert.equal(lifecycle.exit_reason, "startup-error");
});

test("formal stdio server rejects a live non-parent owner", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-formal-parent-"));
  const lifecycleRoot = join(dir, "processes");
  const unrelated = spawn(
    process.execPath,
    ["-e", "setTimeout(() => {}, 30000)"],
    { stdio: "ignore" }
  );
  const child = spawn(process.execPath, [
    resolve("src/server.js"),
    "--config",
    join(dir, "missing.json")
  ], {
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      OPENUBMC_MCP_CLIENT: "codex",
      OPENUBMC_MCP_TASK_ID: "formal-parent-task",
      OPENUBMC_MCP_SESSION_ID: "formal-parent-session",
      OPENUBMC_MCP_SOURCE_COMMIT: "a".repeat(40),
      OPENUBMC_MCP_MODEL_IDENTITY: JSON.stringify({ model: "gpt-5.6-sol" }),
      OPENUBMC_MCP_CODEX_IDENTITY: JSON.stringify({
        version: "codex-cli 0.151.0"
      }),
      OPENUBMC_MCP_FORMAL_RUN: "1",
      OPENUBMC_MCP_PARENT_PID: String(unrelated.pid),
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot,
      OPENUBMC_TARGET_RUNTIME_STATE_DIR: join(dir, "runtime-state")
    }
  });
  let stderr = "";
  child.stderr.on("data", chunk => { stderr += chunk.toString(); });
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    child.once("exit", resolveExit);
    child.once("error", rejectExit);
  });
  const unrelatedExit = new Promise(resolveExit => {
    unrelated.once("exit", resolveExit);
  });
  unrelated.kill();
  await unrelatedExit;
  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );

  assert.equal(returnCode, 1);
  assert.match(stderr, /formal MCP run requires direct parent identity/);
  assert.equal(lifecycle.parent_pid, unrelated.pid);
  assert.equal(lifecycle.exit_reason, "startup-error");
});

test("formal stdio server rejects unknown ownership and source", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-stdio-formal-owner-"));
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
      OPENUBMC_MCP_MODEL_IDENTITY: JSON.stringify({ model: "gpt-5.6-sol" }),
      OPENUBMC_MCP_CODEX_IDENTITY: JSON.stringify({ version: "codex-cli 0.151.0" }),
      OPENUBMC_MCP_FORMAL_RUN: "1",
      OPENUBMC_MCP_PARENT_PID: String(process.pid),
      OPENUBMC_MCP_LIFECYCLE_DIR: lifecycleRoot
    }
  });
  let stderr = "";
  child.stderr.on("data", chunk => { stderr += chunk.toString(); });
  const returnCode = await new Promise((resolveExit, rejectExit) => {
    child.once("exit", resolveExit);
    child.once("error", rejectExit);
  });
  const records = await readdir(lifecycleRoot);
  const lifecycle = JSON.parse(
    await readFile(join(lifecycleRoot, records[0]), "utf8")
  );

  assert.equal(returnCode, 1);
  assert.match(stderr, /formal MCP run requires task ID/);
  assert.match(stderr, /session ID/);
  assert.match(stderr, /source commit/);
  assert.equal(lifecycle.task_id, "unknown-task");
  assert.equal(lifecycle.session_id, "unknown-session");
  assert.equal(lifecycle.exit_reason, "startup-error");
});
