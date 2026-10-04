import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { FileTokenStore } from "../src/auth/token-store.js";
import { readConfigurationJson } from "../src/configuration.js";
import { McpProcessLifecycle } from "../src/process-lifecycle.js";
import { ensurePrivateDirectory, hardenNewFile, verifyPrivatePath,
  windowsProcessState } from "../src/windows-private.js";

const windows = process.platform === "win32";
const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

function setupHelper() {
  if (!process.env.OPENUBMC_WINDOWS_PYTHON) {
    const selected = spawnSync("py", ["-3.12", "-c", "import sys; print(sys.executable)"],
      { windowsHide: true, encoding: "utf8" });
    process.env.OPENUBMC_WINDOWS_PYTHON = selected.status === 0
      ? selected.stdout.trim() : "python";
  }
  process.env.OPENUBMC_WINDOWS_HELPER = join(sourceRoot,
    "openubmc-target-runtime", "tools", "windows_platform_helper.py");
}

function grantOtherUserRead(path) {
  const result = spawnSync("icacls.exe", [path, "/grant", "*S-1-5-32-545:R"],
    { windowsHide: true, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

test("Windows KB files and lifecycle records stay private; permissive ACLs are rejected",
  { skip: !windows }, async () => {
    setupHelper();
    const base = await mkdtemp(join(homedir(), "openubmc-kb-private-"));
    const root = join(base, "private");
    try {
      ensurePrivateDirectory(root);
      const tokenPath = join(root, "token.json");
      const store = new FileTokenStore(tokenPath, "owner");
      assert.equal(await store.save({ accessToken: "fixture-secret", expiresAt: 100 }), true);
      verifyPrivatePath(tokenPath);
      assert.equal((await store.load()).accessToken, "fixture-secret");
      grantOtherUserRead(tokenPath);
      assert.equal(await store.load(), undefined);
      assert.equal(await store.save({ accessToken: "replacement", expiresAt: 200 }), true);
      verifyPrivatePath(tokenPath);
      assert.equal((await store.load()).accessToken, "replacement");

      const configPath = join(root, "kb.json");
      await writeFile(configPath, '{"username":"fixture"}\n');
      hardenNewFile(configPath);
      assert.equal((await readConfigurationJson(configPath, { privateFile: true })).username,
        "fixture");
      grantOtherUserRead(configPath);
      await assert.rejects(readConfigurationJson(configPath, { privateFile: true }),
        { code: "KB_CONFIGURATION_INVALID" });

      const lifecycle = new McpProcessLifecycle({
        component: "knowledge-mcp", version: "fixture", client: "codex",
        taskId: "task", sessionId: "session", parentPid: process.pid,
        processId: process.pid, statePath: configPath, lifecycleRoot: join(root, "lifecycle"),
        idleTimeoutSeconds: 30
      });
      assert.equal(lifecycle.status().parent_identity_verified, true);
      verifyPrivatePath(lifecycle.recordPath);
      assert.equal(JSON.parse(await readFile(lifecycle.recordPath, "utf8")).process_id,
        process.pid);
      assert.notEqual(windowsProcessState(process.pid).identity, "unknown");
    } finally {
      await rm(base, { recursive: true, force: true });
    }
  });
