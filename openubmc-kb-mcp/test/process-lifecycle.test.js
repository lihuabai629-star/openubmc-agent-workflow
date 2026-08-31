import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  McpProcessLifecycle,
  installMcpProcessSignalHandlers
} from "../src/process-lifecycle.js";


test("records ownership and never expires while a request is active", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-lifecycle-"));
  let monotonic = 100;
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "1.3.0",
    client: "codex",
    taskId: "kb-task",
    sessionId: "kb-session",
    sourceCommit: "a".repeat(40),
    modelIdentity: { model: "gpt-5.6-sol" },
    codexIdentity: {
      version: "codex-cli 0.150.0",
      executable_sha256: `sha256:${"b".repeat(64)}`
    },
    formalRun: true,
    parentPid: 1200,
    processId: 1201,
    statePath: join(root, "kb-state"),
    runtimeStateRoot: join(root, "runtime-state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30,
    monotonicClock: () => monotonic,
    wallClock: () => 1787872800000,
    processAlive: pid => pid === 1200,
    processIdentity: pid => `process-${pid}-start`
  });

  await lifecycle.request(async () => {
    monotonic += 60;
    assert.equal(lifecycle.status().lifecycle_state, "active");
    assert.equal(lifecycle.exitReasonIfDue(), null);
  });
  monotonic += 30;
  assert.equal(lifecycle.exitReasonIfDue(), "idle-timeout");

  const recorded = JSON.parse(await readFile(lifecycle.recordPath, "utf8"));
  assert.equal(recorded.component, "knowledge-mcp");
  assert.equal(recorded.task_id, "kb-task");
  assert.equal(recorded.session_id, "kb-session");
  assert.equal(recorded.source_commit, "a".repeat(40));
  assert.equal(recorded.formal_run, true);
  assert.deepEqual(recorded.model_identity, { model: "gpt-5.6-sol" });
  assert.deepEqual(recorded.codex_identity, {
    version: "codex-cli 0.150.0",
    executable_sha256: `sha256:${"b".repeat(64)}`
  });
  assert.equal(recorded.parent_identity_verified, true);
  assert.equal(recorded.parent_identity_currently_verified, true);
  assert.equal(recorded.process_identity, "process-1201-start");
  assert.equal(recorded.state_path, join(root, "kb-state"));
  assert.equal(recorded.runtime_state_root, join(root, "runtime-state"));
  assert.equal(recorded.lifecycle_state, "stopped");
  assert.equal(recorded.exit_reason, "idle-timeout");
  assert.match(lifecycle.recordPath, /knowledge-mcp-1201-process-1201-start\.json$/);
});

test("unknown ownership still yields parent-exited after parent loss", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-orphan-"));
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "1.3.0",
    client: "unknown-client",
    taskId: "unknown-task",
    sessionId: "unknown-session",
    parentPid: 1200,
    processId: 1201,
    statePath: join(root, "kb-state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30,
    processAlive: pid => pid === 1201,
    processIdentity: pid => `process-${pid}-start`
  });

  assert.equal(lifecycle.status().lifecycle_state, "orphaned");
  assert.equal(lifecycle.exitReasonIfDue(), "parent-exited");
});

test("an unreadable live parent identity remains unknown-owner", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-parent-unknown-"));
  let parentIdentity = "process-1200-start";
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "1.3.0",
    client: "codex",
    taskId: "kb-task",
    sessionId: "kb-session",
    parentPid: 1200,
    processId: 1201,
    statePath: join(root, "kb-state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30,
    processAlive: pid => pid === 1200,
    processIdentity: pid => pid === 1200 ? parentIdentity : "process-1201-start"
  });
  parentIdentity = "unknown";

  assert.equal(lifecycle.status().lifecycle_state, "unknown-owner");
  assert.equal(lifecycle.status().parent_identity_verified, true);
  assert.equal(lifecycle.status().parent_identity_currently_verified, false);
});

test("startup ownership stays unknown until parent identity is verified", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-parent-startup-"));
  let parentIdentity = "unknown";
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "1.3.0",
    client: "codex",
    taskId: "kb-task",
    sessionId: "kb-session",
    parentPid: 1200,
    processId: 1201,
    statePath: join(root, "kb-state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30,
    processAlive: pid => pid === 1200,
    processIdentity: pid => pid === 1200 ? parentIdentity : "process-1201-start"
  });
  assert.equal(lifecycle.status().lifecycle_state, "unknown-owner");

  parentIdentity = "process-1200-start";
  assert.equal(lifecycle.status().parent_identity, "process-1200-start");
  assert.equal(lifecycle.status().parent_identity_verified, true);
  assert.equal(lifecycle.status().parent_identity_currently_verified, true);
  assert.equal(lifecycle.status().lifecycle_state, "idle");
});

test("requested shutdown drains active work and timeout must be finite", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-shutdown-"));
  const options = {
    component: "knowledge-mcp",
    version: "1.3.0",
    client: "codex",
    taskId: "kb-task",
    sessionId: "kb-session",
    parentPid: 1200,
    processId: 1201,
    statePath: join(root, "kb-state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30,
    processAlive: pid => pid === 1200,
    processIdentity: pid => `process-${pid}-start`
  };
  const lifecycle = new McpProcessLifecycle(options);

  await lifecycle.request(async () => {
    lifecycle.requestExit("client-terminated");
    assert.equal(lifecycle.shutdownRequested, true);
    assert.equal(lifecycle.exitReasonIfDue(), null);
  });
  assert.equal(lifecycle.exitReasonIfDue(), "client-terminated");
  assert.equal(lifecycle.shutdownRequested, true);
  assert.throws(() => lifecycle.beginRequest(), /shutting down/);
  assert.throws(
    () => new McpProcessLifecycle({ ...options, idleTimeoutSeconds: Infinity }),
    /finite/
  );
  for (const parentPid of [NaN, Infinity]) {
    assert.throws(
      () => new McpProcessLifecycle({ ...options, parentPid }),
      /parentPid/
    );
  }
  assert.throws(
    () => new McpProcessLifecycle({ ...options, taskId: false }),
    /taskId must be a string/
  );
});

test("explicit task closeout drains active work before recording exit", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-task-closeout-"));
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "1.3.0",
    client: "codex",
    taskId: "kb-task",
    sessionId: "kb-session",
    parentPid: 1200,
    processId: 1201,
    statePath: join(root, "kb-state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30,
    processAlive: pid => pid === 1200,
    processIdentity: pid => `process-${pid}-start`
  });

  await lifecycle.request(async () => {
    lifecycle.requestTaskCloseout();
    assert.equal(lifecycle.exitReasonIfDue(), null);
    assert.equal(lifecycle.status().shutdown_requested, "task-closeout");
  });

  assert.equal(lifecycle.exitReasonIfDue(), "task-closeout");
  const record = JSON.parse(await readFile(lifecycle.recordPath, "utf8"));
  assert.equal(record.active_requests, 0);
  assert.equal(record.exit_reason, "task-closeout");
});

test("repeated termination signals remain handled until explicit cleanup", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-repeat-signal-"));
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "test",
    client: "test-client",
    taskId: "signal-task",
    sessionId: "signal-session",
    parentPid: process.ppid,
    processId: process.pid,
    statePath: join(root, "state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30
  });
  const removeSignalHandlers = installMcpProcessSignalHandlers(lifecycle);
  try {
    lifecycle.beginRequest();
    process.kill(process.pid, "SIGTERM");
    process.kill(process.pid, "SIGINT");
    while (!lifecycle.shutdownRequested) {
      await new Promise(resolveImmediate => setImmediate(resolveImmediate));
    }
    assert.equal(lifecycle.activeRequests, 1);
    lifecycle.endRequest();
    assert.equal(lifecycle.exitReasonIfDue(), "client-terminated");
  } finally {
    removeSignalHandlers();
  }
});

test("abrupt process exit records a stopped lifecycle even with active work", async () => {
  const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-forced-exit-"));
  const lifecycle = new McpProcessLifecycle({
    component: "knowledge-mcp",
    version: "test",
    client: "test-client",
    taskId: "forced-task",
    sessionId: "forced-session",
    parentPid: process.ppid,
    processId: process.pid,
    statePath: join(root, "state"),
    lifecycleRoot: join(root, "processes"),
    idleTimeoutSeconds: 30
  });
  lifecycle.beginRequest();

  lifecycle.recordForcedExit("process-exit");

  const record = JSON.parse(await readFile(lifecycle.recordPath, "utf8"));
  assert.equal(record.lifecycle_state, "stopped");
  assert.equal(record.active_requests, 0);
  assert.equal(record.exit_reason, "process-exit");
});

test("SIGTERM and SIGINT drain active responses before lifecycle exit", async () => {
  for (const terminationSignal of ["SIGTERM", "SIGINT"]) {
    const root = await mkdtemp(join(tmpdir(), "openubmc-mcp-signal-"));
    const lifecycle = new McpProcessLifecycle({
      component: "knowledge-mcp",
      version: "test",
      client: "test-client",
      taskId: "signal-task",
      sessionId: "signal-session",
      parentPid: process.ppid,
      processId: process.pid,
      statePath: join(root, "state"),
      lifecycleRoot: join(root, "processes"),
      idleTimeoutSeconds: 30
    });
    const removeSignalHandlers = installMcpProcessSignalHandlers(lifecycle);
    const output = [];
    try {
      await lifecycle.request(async () => {
        output.push("active");
        process.kill(process.pid, terminationSignal);
        while (!lifecycle.shutdownRequested) {
          await new Promise(resolveImmediate => setImmediate(resolveImmediate));
        }
        assert.equal(lifecycle.exitReasonIfDue(), null);
        output.push("response");
      });
      assert.equal(lifecycle.exitReasonIfDue(), "client-terminated");
      const record = JSON.parse(await readFile(lifecycle.recordPath, "utf8"));
      assert.deepEqual(output, ["active", "response"]);
      assert.equal(record.active_requests, 0);
      assert.equal(record.exit_reason, "client-terminated");
    } finally {
      removeSignalHandlers();
    }
  }
});
