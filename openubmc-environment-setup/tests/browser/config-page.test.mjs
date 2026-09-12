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

async function configurationPage(t, options = []) {
  const directory = await mkdtemp(
    path.join(tmpdir(), "openubmc-config-browser-"),
  );
  t.after(() => rm(directory, { recursive: true, force: true }));
  const server = spawn(
    "python3",
    ["-B", script, "--config-home", directory, "--no-browser", ...options],
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
  const completion = options.includes("--wait-for-save") ? once(lines, "line") : null;
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
  return { page: await browser.newPage(), api, url, completion };
}

test("a focused page saves the requested account and signals completion without a chat reply", { timeout: 30000 }, async (t) => {
  const { page, url, completion } = await configurationPage(t, ["--kind", "kb", "--wait-for-save"]);
  await page.goto(url);
  await page.getByLabel("openUBMC 账号", { exact: true }).fill("fixture");
  await page.getByLabel("密码", { exact: true }).fill("local-kb-password");
  await page.getByLabel("OAuth 应用密钥", { exact: true }).fill("local-app-secret");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "可以回到原任务继续" }).waitFor();
  const [line] = await completion;
  const receipt = JSON.parse(line);
  assert.equal(receipt.event, "configuration_saved");
  assert.equal(receipt.kind, "kb");
  assert.equal(receipt.configured, true);
  assert.equal(line.includes("local-kb-password"), false);
  assert.equal(line.includes("local-app-secret"), false);
  assert.equal(await page.getByRole("button", { name: "保存", exact: true }).isDisabled(), true);
});

test("a default BMC account is saved for both protocols with one action", { timeout: 30000 }, async (t) => {
  const { page, api, url } = await configurationPage(t);
  await page.goto(url);
  await page.getByRole("textbox", { name: "BMC 用户名", exact: true }).fill("fixture");
  await page.getByLabel("BMC 密码", { exact: true }).fill("browser-local-secret");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "配置已生效" }).waitFor();
  const current = (await api("/api/state")).targets;
  assert.equal(current.revision, current.active_revision);
  assert.equal(current.config.defaults.bmc.ssh, current.config.defaults.bmc.redfish);
  assert.equal(current.config.credentials[current.config.defaults.bmc.ssh].user, "fixture");
  assert.equal(current.config.credentials[current.config.defaults.bmc.ssh].password_set, true);
  assert.equal(current.verified, false);
  assert.equal(await page.getByRole("textbox", { name: "名称", exact: true }).count(), 0);
  assert.equal(await page.getByLabel("BMC 密码", { exact: true }).isVisible(), false);
});

test("a device has a BMC override and an associated OS with its own account", { timeout: 30000 }, async (t) => {
  const { page, api, url } = await configurationPage(t);
  await page.goto(url);
  await page.getByRole("textbox", { name: "BMC 用户名", exact: true }).fill("default-user");
  await page.getByLabel("BMC 密码", { exact: true }).fill("default-secret");
  await page.getByText("设备与关联 OS", { exact: true }).click();
  await page.getByRole("button", { name: "添加设备", exact: true }).click();
  const device = page.getByRole("group", { name: "设备", exact: true });
  await device.getByLabel("BMC IP", { exact: true }).fill("192.0.2.10");
  await device.getByLabel("此 BMC 使用不同账号").check();
  await device.getByLabel("设备 BMC 用户名", { exact: true }).fill("special-user");
  await device.getByLabel("设备 BMC 密码", { exact: true }).fill("special-secret");
  await device.getByLabel("关联 OS IP", { exact: true }).fill("192.0.2.20");
  await device.getByLabel("此 OS 使用不同账号").check();
  await device.getByLabel("设备 OS 用户名", { exact: true }).fill("os-user");
  await device.getByLabel("设备 OS 密码", { exact: true }).fill("os-secret");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "配置已生效" }).waitFor();
  const config = (await api("/api/state")).targets.config;
  assert.deepEqual(config.devices, { "192.0.2.10": { os_ip: "192.0.2.20" } });
  assert.equal(config.targets["192.0.2.10"].bmc.ssh, config.targets["192.0.2.10"].bmc.redfish);
  assert.equal(config.credentials[config.targets["192.0.2.10"].bmc.ssh].user, "special-user");
  assert.equal(config.credentials[config.targets["192.0.2.20"].os.ssh].user, "os-user");
  assert.equal(config.credentials[config.defaults.bmc.ssh].user, "default-user");
  assert.equal(JSON.stringify(config).includes("os-secret"), false);
});

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
    await page.getByRole("button", { name: "高级账号管理", exact: true }).click();
    await page
      .getByRole("textbox", { name: "名称", exact: true })
      .first()
      .fill("");
    await page
      .getByRole("textbox", { name: "名称", exact: true })
      .first()
      .pressSequentially("renamed");
    await page.getByRole("button", { name: "保存", exact: true }).click();
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

test("existing independent protocol accounts are preserved instead of silently unified", { timeout: 30000 }, async (t) => {
  const { page, api, url } = await configurationPage(t);
  await api("/api/save", { kind: "targets", expected_revision: null, config: {
    schema_version: 1,
    credentials: {
      ssh: { user: "ssh-user", password: { action: "replace", value: "ssh-secret" } },
      rf: { user: "rf-user", password: { action: "replace", value: "rf-secret" } },
    },
    defaults: { bmc: { ssh: "ssh", redfish: "rf" } },
    devices: { "192.0.2.10": { os_ip: "192.0.2.20" } },
  } });
  await page.goto(url);
  await page.getByRole("button", { name: "返回常用配置", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "请先将两种协议选择为同一账号并保存" }).waitFor();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "配置已生效" }).waitFor();
  const config = (await api("/api/state")).targets.config;
  assert.deepEqual(config.defaults, { bmc: { ssh: "ssh", redfish: "rf" } });
  assert.deepEqual(config.devices, { "192.0.2.10": { os_ip: "192.0.2.20" } });
  assert.equal(config.credentials.ssh.password_set, true);
  assert.equal(config.credentials.rf.password_set, true);
});
