import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { Backends } from "../src/backend.js";
const command = { command: process.execPath, args: [fileURLToPath(new URL("./stdio-fixture.mjs", import.meta.url))] };

test("stdio bridge binds task and repeat request identity without changing arguments", async t => {
  const a = new Backends({ runtime: command }, "chatgpt-task-a");
  const b = new Backends({ runtime: command }, "chatgpt-task-b");
  t.after(async () => { await a.close(); await b.close(); });
  const args = { kind: "resume", run_id: "run-original" };
  const first = await a.call("runtime", "execute", args, { requestId: 99 });
  const repeated = await a.call("runtime", "execute", args, { requestId: 99 });
  const other = await b.call("runtime", "execute", args, { requestId: 99 });
  assert.deepEqual(first.structuredContent.arguments, args);
  assert.equal(first.structuredContent.metadata.task_id, "chatgpt-task-a");
  assert.equal(first.structuredContent.metadata["openubmc/operationId"], repeated.structuredContent.metadata["openubmc/operationId"]);
  assert.notEqual(first.structuredContent.metadata["openubmc/operationId"], other.structuredContent.metadata["openubmc/operationId"]);
});

test("catalog keeps the semantic Agent boundary and limits operator access to reads", async t => {
  const backends = new Backends({ runtime: command, operator: command, kb: command });
  t.after(() => backends.close());
  assert.deepEqual((await backends.tools("runtime")).map(tool => tool.name), ["observe", "execute"]);
  assert.deepEqual((await backends.tools("operator")).map(tool => tool.name), ["runtime_status", "case_read"]);
  assert.deepEqual((await backends.tools("kb")).map(tool => tool.name), ["openubmc_kb_query"]);
});

test("an unconfigured backend is explicit and does not start a process", async () => {
  const backends = new Backends();
  assert.deepEqual(await backends.tools("runtime"), []);
  assert.equal(backends.status().runtime, "not_configured");
  await assert.rejects(backends.call("runtime", "execute", {}), /NOT_CONFIGURED/);
  await backends.close();
  await assert.rejects(backends.get("runtime"), /CLOSED/);
});
