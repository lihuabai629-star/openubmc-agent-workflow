import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const script = fileURLToPath(
  new URL("../../scripts/config_page.py", import.meta.url),
);

async function configurationPage(t) {
  const directory = await mkdtemp(
    path.join(tmpdir(), "openubmc-config-browser-"),
  );
  t.after(() => rm(directory, { recursive: true, force: true }));
  const server = spawn(
    "python3",
    ["-B", script, "--config-home", directory, "--no-browser"],
    {
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderr = "";
  server.stderr.on("data", (data) => {
    stderr += data;
  });
  t.after(async () => {
    if (server.exitCode !== null) return;
    const stopped = once(server, "exit");
    server.kill("SIGTERM");
    const timer = setTimeout(() => server.kill("SIGKILL"), 3000);
    try {
      await stopped;
    } finally {
      clearTimeout(timer);
    }
  });
  const lines = createInterface({ input: server.stdout });
  const exited = once(server, "exit").then(() => {
    throw new Error("Configuration server stopped: " + stderr);
  });
  const [url] = await Promise.race([once(lines, "line"), exited]);
  const session = new URL(url);
  const token = session.hash.slice(1);
  async function api(endpoint, data) {
    const response = await fetch(session.origin + endpoint, {
      method: data ? "POST" : "GET",
      headers: {
        "X-OpenUBMC-Session": token,
        Origin: session.origin,
        "Content-Type": "application/json",
      },
      ...(data ? { body: JSON.stringify(data) } : {}),
    });
    assert.equal(response.status, 200);
    return response.json();
  }
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.OPENUBMC_TEST_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.OPENUBMC_TEST_CHROMIUM_EXECUTABLE }
      : {}),
  });
  t.after(() => browser.close());
  return { page: await browser.newPage(), api, url };
}

test(
  "renaming a credential keeps default and IP selections after activation",
  { timeout: 30000 },
  async (t) => {
    const { page, api, url } = await configurationPage(t);
    const saved = await api("/api/save", {
      kind: "targets",
      expected_revision: null,
      config: {
        schema_version: 1,
        credentials: {
          common: {
            user: "fixture",
            password: { action: "replace", value: "local-fixture-password" },
          },
          other: {
            user: "other-fixture",
            password: {
              action: "replace",
              value: "other-local-fixture-password",
            },
          },
        },
        defaults: {
          bmc: { ssh: "common", redfish: "common" },
          os: { ssh: "other" },
        },
        targets: {
          "192.0.2.10": { bmc: { ssh: "common", redfish: "common" } },
        },
      },
    });
    await api("/api/activate", {
      kind: "targets",
      revision: saved.revision,
      expected_active_revision: null,
    });
    await page.goto(url);
    await page
      .getByRole("textbox", { name: "名称", exact: true })
      .first()
      .fill("");
    await page
      .getByRole("textbox", { name: "名称", exact: true })
      .first()
      .pressSequentially("renamed");
    await page.getByRole("button", { name: "保存并生效", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "配置已生效" }).waitFor();
    const current = (await api("/api/state")).targets;
    assert.deepEqual(current.config.defaults, {
      bmc: { ssh: "renamed", redfish: "renamed" },
      os: { ssh: "other" },
    });
    assert.deepEqual(current.config.targets, {
      "192.0.2.10": { bmc: { ssh: "renamed", redfish: "renamed" } },
    });
    assert.equal(current.active_revision, current.revision);
    assert.notEqual(current.revision, saved.revision);
    assert.equal(current.config.credentials.renamed.password_set, true);
    assert.equal(current.config.credentials.common, undefined);
    assert.equal(current.verified, false);
  },
);

test("plugin maintenance previews changes before apply and exposes undo", { timeout: 30000 }, async (t) => {
  const { page, url } = await configurationPage(t);
  const calls = [];
  await page.route("**/api/plugin", async (route) => {
    const data = route.request().postDataJSON();
    calls.push(data);
    const replies = {
      status: { version: "2.0.14", integrity: true, runtime: true, kb: false,
        configuration: { ready: false, conflict: false, servers: ["openubmc-kb"] } },
      preview: { preview_id: "preview-fixture", would_change: true, servers: ["openubmc-kb"] },
      apply: { changed: true, transaction: "transaction-fixture" },
      undo: { restored: true },
    };
    await route.fulfill({ json: replies[data.action] });
  });
  await page.goto(url);
  await page.locator("#plugin-maintenance summary").click();
  assert.equal(await page.locator("#plugin-apply").isVisible(), false);
  await page.locator("#plugin-check").click();
  await page.getByText("版本：2.0.14", { exact: true }).waitFor();
  await page.locator("#plugin-preview").click();
  await page.locator("#plugin-apply").waitFor();
  assert.equal(calls.some((c) => c.action === "apply"), false);
  await page.locator("#plugin-apply").click();
  await page.locator("#plugin-undo").waitFor();
  await page.locator("#plugin-undo").click();
  await page.getByText("已恢复修复前的配置。", { exact: true }).waitFor();
  assert.deepEqual(calls.find((c) => c.action === "apply"), { action: "apply", preview_id: "preview-fixture" });
  assert.deepEqual(calls.find((c) => c.action === "undo"), { action: "undo", transaction: "transaction-fixture" });
});
