import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { after } from "node:test";

import { ensurePrivateDirectory, hardenNewFile } from "../src/windows-private.js";

const roots = [];
const windows = process.platform === "win32";

function setupWindowsHelper() {
  if (!windows) return;
  if (!process.env.OPENUBMC_WINDOWS_PYTHON) {
    const selected = spawnSync("py", ["-3.12", "-c", "import sys; print(sys.executable)"],
      { windowsHide: true, encoding: "utf8" });
    process.env.OPENUBMC_WINDOWS_PYTHON = selected.status === 0
      ? selected.stdout.trim() : "python";
  }
  process.env.OPENUBMC_WINDOWS_HELPER ||= resolve(dirname(fileURLToPath(import.meta.url)),
    "../../openubmc-target-runtime/tools/windows_platform_helper.py");
}

export async function privateFixtureDirectory(prefix) {
  setupWindowsHelper();
  const root = windows
    ? join(process.env.LOCALAPPDATA || homedir(), `${prefix}${randomUUID()}`)
    : await mkdtemp(join(tmpdir(), prefix));
  if (windows) ensurePrivateDirectory(root);
  roots.push(root);
  return root;
}

export async function writePrivateFixture(path, content) {
  await writeFile(path, content);
  if (windows) hardenNewFile(path);
}

after(async () => {
  for (const root of roots.reverse()) await rm(root, { recursive: true, force: true });
});
