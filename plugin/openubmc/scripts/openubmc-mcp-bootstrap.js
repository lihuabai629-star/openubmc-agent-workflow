#!/usr/bin/env node
"use strict";

// This file deliberately uses only Node built-ins. It must be able to explain a
// broken or incomplete installation before either packaged backend can start.
const childProcess = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const capability = process.argv[2];
if (!new Set(["runtime", "kb"]).has(capability)) {
  process.stderr.write("usage: openubmc-mcp-bootstrap.js runtime|kb\n");
  process.exit(2);
}

const pluginRoot = path.resolve(__dirname, "..");
const hostPlatform = process.env.OPENUBMC_PLUGIN_HOST_PLATFORM || process.platform;
const wslExecutable = process.env.OPENUBMC_PLUGIN_WSL_EXE || "wsl.exe";
const childEnvironment = { ...process.env, PYTHONDONTWRITEBYTECODE: "1" };
delete childEnvironment.PYTHONPATH;
delete childEnvironment.PYTHONHOME;

function safeWriteError(value) {
  if (value) process.stderr.write(String(value).slice(-8192));
}

function relaySafeProgress(buffer) {
  for (const line of decodeWindowsOutput(buffer).split(/\r?\n/)) {
    try {
      const value = JSON.parse(line);
      const progress = {};
      for (const key of ["stage", "status", "pid", "timeout_seconds", "elapsed_seconds", "exit_code"]) {
        if (Object.hasOwn(value, key)) progress[key] = value[key];
      }
      if (progress.stage && progress.status) process.stderr.write(`${JSON.stringify(progress)}\n`);
    } catch (_error) {
      // Dependency tools may echo authenticated URLs or local configuration.
      // Their unstructured output stays local to this process.
    }
  }
}

function run(command, args, options = {}) {
  return childProcess.spawnSync(command, args, {
    env: childEnvironment,
    encoding: null,
    maxBuffer: 16 * 1024 * 1024,
    timeout: options.timeout || 30_000,
    windowsHide: true,
  });
}

function decodeWindowsOutput(buffer) {
  if (!buffer || buffer.length === 0) return "";
  const bytes = Buffer.from(buffer);
  const hasNuls = bytes.subarray(0, Math.min(bytes.length, 256)).includes(0);
  return bytes.toString(hasNuls ? "utf16le" : "utf8").replace(/^\uFEFF/, "").replace(/\0/g, "");
}

function hostConfigPath() {
  const base = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  return path.join(base, "openubmc", "plugin-host.json");
}

function readSelectedDistro() {
  if (process.env.OPENUBMC_WSL_DISTRO) return process.env.OPENUBMC_WSL_DISTRO.trim();
  try {
    const record = JSON.parse(fs.readFileSync(hostConfigPath(), "utf8"));
    return record && record.schema === "openubmc.plugin-host.v1" && typeof record.wsl_distro === "string"
      ? record.wsl_distro.trim()
      : "";
  } catch (_error) {
    return "";
  }
}

function listDistros() {
  const listed = run(wslExecutable, ["--list", "--quiet"]);
  if (listed.error || listed.status !== 0) return { ok: false, distros: [] };
  const distros = decodeWindowsOutput(listed.stdout)
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter((value, index, values) => value && values.indexOf(value) === index);
  return { ok: true, distros };
}

function resolveWindowsBackend() {
  const discovered = listDistros();
  if (!discovered.ok || discovered.distros.length === 0) {
    return { ok: false, reason: "wsl_unavailable", distros: [] };
  }
  const selected = readSelectedDistro();
  if (selected && !discovered.distros.includes(selected)) {
    return { ok: false, reason: "selected_wsl_unavailable", distros: discovered.distros, selected_wsl: selected };
  }
  if (!selected && discovered.distros.length > 1) {
    return { ok: false, reason: "wsl_selection_required", distros: discovered.distros };
  }
  const distro = selected || discovered.distros[0];
  const converted = run(wslExecutable, ["-d", distro, "--exec", "wslpath", "-a", "-u", pluginRoot]);
  if (converted.error || converted.status !== 0) {
    return { ok: false, reason: "plugin_path_unavailable_in_wsl", distros: discovered.distros, selected_wsl: distro };
  }
  const linuxRoot = decodeWindowsOutput(converted.stdout).trim();
  if (!linuxRoot.startsWith("/")) {
    return { ok: false, reason: "plugin_path_unavailable_in_wsl", distros: discovered.distros, selected_wsl: distro };
  }
  const python = run(wslExecutable, ["-d", distro, "--exec", "python3", "-c", "import sys; assert sys.version_info >= (3, 11)"], { timeout: 15_000 });
  if (python.error || python.status !== 0) {
    return { ok: false, reason: "wsl_python_unavailable", distros: discovered.distros, selected_wsl: distro };
  }
  const windowsHome = process.env.USERPROFILE || os.homedir();
  const windowsCodex = process.env.CODEX_HOME || path.win32.join(windowsHome, ".codex");
  const convertHostPath = (value) => {
    const result = run(wslExecutable, ["-d", distro, "--exec", "wslpath", "-a", "-u", value]);
    return result.error || result.status !== 0 ? "" : decodeWindowsOutput(result.stdout).trim();
  };
  const linuxHostHome = convertHostPath(windowsHome);
  const linuxCodexHome = convertHostPath(windowsCodex);
  if (!linuxHostHome.startsWith("/") || !linuxCodexHome.startsWith("/")) {
    return { ok: false, reason: "windows_configuration_path_unavailable_in_wsl", distros: discovered.distros, selected_wsl: distro };
  }
  const identityEnvironment = [];
  for (const key of [
    "OPENUBMC_MCP_CLIENT",
    "OPENUBMC_MCP_TASK_ID",
    "OPENUBMC_MCP_SESSION_ID",
    "OPENUBMC_MCP_FORMAL_RUN",
    "OPENUBMC_MCP_MODEL_IDENTITY",
    "OPENUBMC_MCP_CODEX_IDENTITY",
    "OPENUBMC_EVALUATION_TASK_ID",
  ]) {
    const value = process.env[key];
    if (typeof value === "string" && value.length <= 4096 && !/[\0\r\n]/.test(value)) {
      identityEnvironment.push(`${key}=${value}`);
    }
  }
  return {
    ok: true,
    command: wslExecutable,
    prefix: [
      "-d", distro, "--exec", "env",
      "OPENUBMC_EXECUTION_HOST=windows-wsl",
      `OPENUBMC_SELECTED_WSL_DISTRO=${distro}`,
      ...identityEnvironment,
      "python3", "-I", "-B", `${linuxRoot}/scripts/pluginctl.py`,
    ],
    distros: discovered.distros,
    selected_wsl: distro,
    configurationArgs: ["--home", linuxHostHome, "--codex-home", linuxCodexHome],
  };
}

function resolvePosixBackend() {
  const python = process.env.OPENUBMC_PLUGIN_PYTHON || "python3";
  const checked = run(python, ["-c", "import sys; assert sys.version_info >= (3, 11)"], { timeout: 15_000 });
  if (checked.error || checked.status !== 0) return { ok: false, reason: "python_unavailable" };
  return {
    ok: true,
    command: python,
    prefix: ["-I", "-B", path.join(pluginRoot, "scripts", "pluginctl.py")],
    configurationArgs: [],
  };
}

function resolveBackend() {
  return hostPlatform === "win32" ? resolveWindowsBackend() : resolvePosixBackend();
}

function prepareBackend(backend) {
  const prepared = run(
    backend.command,
    [...backend.prefix, "prepare", "--capability", capability],
    { timeout: 600_000 },
  );
  relaySafeProgress(prepared.stderr);
  if (prepared.error && prepared.error.code === "ETIMEDOUT") return { ok: false, reason: "dependency_prepare_timeout" };
  if (prepared.error || prepared.status !== 0) return { ok: false, reason: "dependency_prepare_failed" };
  return { ok: true };
}

function parseJsonOutput(buffer) {
  try {
    const value = JSON.parse(decodeWindowsOutput(buffer));
    return value && typeof value === "object" ? value : null;
  } catch (_error) {
    return null;
  }
}

function preflightBackend(backend) {
  const cleanup = run(backend.command, [...backend.prefix, "cleanup-retired"], { timeout: 15_000 });
  const cleanupReport = parseJsonOutput(cleanup.stdout);
  const checked = run(
    backend.command,
    [...backend.prefix, "doctor", "--capability", capability, ...backend.configurationArgs],
    { timeout: 30_000 },
  );
  if (!checked.error && checked.status === 0) {
    return { ok: true, cleanup: cleanupReport };
  }
  const report = parseJsonOutput(checked.stdout);
  const rawError = String(report?.capabilities?.[capability]?.error || "").toLowerCase();
  let reason = "dependency_preflight_failed";
  if (rawError.includes("not prepared")) reason = "dependencies_not_prepared";
  else if (rawError.includes("cache drift")) reason = "dependency_cache_drift";
  else if (rawError.includes("node 20")) reason = "wsl_node_unavailable";
  else if (decodeWindowsOutput(checked.stderr).includes("plugin file inventory mismatch")) reason = "package_integrity_failed";
  return { ok: false, reason, cleanup: cleanupReport };
}

function proxyBackend(backend) {
  const child = childProcess.spawn(backend.command, [...backend.prefix, capability], {
    env: childEnvironment,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  process.stdin.pipe(child.stdin);
  child.stdout.pipe(process.stdout);
  child.stderr.pipe(process.stderr);
  const stop = () => {
    if (!child.killed) child.kill();
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  process.stdin.once("end", () => child.stdin.end());
  child.once("error", (error) => {
    safeWriteError(`openUBMC backend failed to start: ${error.code || "spawn_failed"}\n`);
    process.exitCode = 1;
  });
  child.once("exit", (code) => {
    process.exitCode = code === null ? 1 : code;
  });
}

function publicStatus(failure) {
  const status = {
    schema: "openubmc.plugin-setup.v1",
    status: "setup_required",
    capability,
    host: hostPlatform === "win32" ? "windows" : "linux",
    reason: failure.reason,
    next_action:
      hostPlatform === "win32"
        ? "Use the openUBMC setup tools in this task, then start a new task."
        : "Use openubmc_setup_prepare in this task, then start a new task.",
    execution_host: hostPlatform === "win32" ? "wsl" : "linux",
    native_windows_build_supported: false,
  };
  if (Array.isArray(failure.distros)) status.available_wsl_distros = failure.distros;
  if (failure.selected_wsl) status.selected_wsl = failure.selected_wsl;
  return status;
}

function saveSelectedDistro(distro, available) {
  if (typeof distro !== "string" || !available.includes(distro)) {
    throw new Error("Select one of the available WSL distributions exactly as listed.");
  }
  const target = hostConfigPath();
  const parent = path.dirname(target);
  fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
  const temporary = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(
    temporary,
    `${JSON.stringify({ schema: "openubmc.plugin-host.v1", wsl_distro: distro }, null, 2)}\n`,
    { mode: 0o600 },
  );
  fs.renameSync(temporary, target);
}

function repairConfiguration(backend) {
  const operations = [];
  for (const command of ["repair-overrides", "migrate"]) {
    const mode = command === "migrate" ? ["--disable-only"] : [];
    const preview = run(
      backend.command,
      [...backend.prefix, command, ...mode, "--preview", ...backend.configurationArgs],
      { timeout: 30_000 },
    );
    const previewReport = parseJsonOutput(preview.stdout);
    if (preview.error || !previewReport?.ok) {
      return { ok: false, reason: "configuration_repair_conflict" };
    }
    if (!previewReport.would_change) continue;
    const applied = run(
      backend.command,
      [...backend.prefix, command, ...mode, ...backend.configurationArgs],
      { timeout: 30_000 },
    );
    const appliedReport = parseJsonOutput(applied.stdout);
    if (applied.error || applied.status !== 0 || !appliedReport?.ok) {
      return { ok: false, reason: "configuration_repair_failed" };
    }
    operations.push({
      operation: command,
      transaction: appliedReport.transaction || null,
      mcp_servers: appliedReport.changes?.mcp_servers || [],
      skill_count: appliedReport.changes?.skills?.length || 0,
    });
  }
  return { ok: true, operations };
}

const configurationProcesses = new Set();

function openConfiguration(backend, kind) {
  return new Promise((resolve, reject) => {
    const child = childProcess.spawn(
      backend.command,
      [...backend.prefix, "configure", "--kind", kind, "--open-browser"],
      { env: childEnvironment, stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
    );
    configurationProcesses.add(child);
    let output = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error("configuration_page_timeout"));
    }, 15_000);
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      output += chunk.toString("utf8");
      const line = output.split(/\r?\n/, 1)[0].trim();
      if (!line.includes("\n") && !output.includes("\n")) return;
      if (!/^http:\/\/(?:127\.0\.0\.1|\[::1\]):[0-9]+\/#[-A-Za-z0-9_]+$/.test(line)) {
        settled = true;
        clearTimeout(timer);
        child.kill();
        reject(new Error("configuration_page_invalid_url"));
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(line);
    });
    child.stderr.resume();
    child.once("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error("configuration_page_start_failed"));
      }
    });
    child.once("exit", () => {
      configurationProcesses.delete(child);
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error("configuration_page_start_failed"));
      }
    });
  });
}

function serveSetup(failure) {
  const status = publicStatus(failure);
  const lineReader = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  function reply(id, result) {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`);
  }
  function error(id, code, message) {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } })}\n`);
  }
  lineReader.on("line", (line) => {
    if (!line.trim()) return;
    let request;
    try {
      request = JSON.parse(line);
    } catch (_parseError) {
      error(null, -32700, "Parse error");
      return;
    }
    if (request.id === undefined || request.id === null) return;
    if (request.method === "initialize") {
      reply(request.id, {
        protocolVersion: request.params?.protocolVersion || "2024-11-05",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: `openubmc-${capability}-setup`, version: "1" },
        instructions: "The openUBMC backend needs local setup. Call openubmc_setup_status for the structured reason.",
      });
      return;
    }
    if (request.method === "ping") {
      reply(request.id, {});
      return;
    }
    if (request.method === "tools/list") {
      reply(request.id, {
        tools: [
          {
            name: "openubmc_setup_status",
            description: "Return the local openUBMC plugin setup blocker without exposing credentials.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          },
          {
            name: "openubmc_setup_select_wsl",
            description: "Select an installed WSL distribution for the openUBMC Linux backend.",
            inputSchema: {
              type: "object",
              properties: { distro: { type: "string", description: "An exact name from available_wsl_distros." } },
              required: ["distro"],
              additionalProperties: false,
            },
          },
          {
            name: "openubmc_setup_prepare",
            description: "Prepare the locked dependencies for this openUBMC capability outside MCP initialization.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          },
          {
            name: "openubmc_setup_open_configuration",
            description: "Open the private local openUBMC configuration page and return its loopback URL.",
            inputSchema: {
              type: "object",
              properties: { kind: { type: "string", enum: ["targets", "kb", "conan"] } },
              required: ["kind"],
              additionalProperties: false,
            },
          },
          {
            name: "openubmc_setup_repair_configuration",
            description: "Back up and repair recognized stale openUBMC MCP overrides and exact-name loose Skills.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          },
        ],
      });
      return;
    }
    if (request.method === "tools/call") {
      let response = status;
      let isError = false;
      if (request.params?.name === "openubmc_setup_select_wsl") {
        try {
          saveSelectedDistro(request.params?.arguments?.distro, status.available_wsl_distros || []);
          response = {
            schema: status.schema,
            status: "configuration_saved",
            selected_wsl: request.params.arguments.distro,
            next_action: "Start a new Codex task to initialize the openUBMC backend.",
          };
        } catch (selectionError) {
          response = { ...status, error: selectionError.message };
          isError = true;
        }
      } else if (request.params?.name === "openubmc_setup_prepare") {
        const refreshed = resolveBackend();
        if (!refreshed.ok) {
          response = publicStatus(refreshed);
          isError = true;
        } else {
          const prepared = prepareBackend(refreshed);
          if (prepared.ok) {
            response = {
              schema: status.schema,
              status: "preparation_completed",
              capability,
              next_action: "Start a new Codex task to use the prepared openUBMC backend.",
            };
          } else {
            response = publicStatus({ ...refreshed, ...prepared, ok: false });
            isError = true;
          }
        }
      } else if (request.params?.name === "openubmc_setup_open_configuration") {
        const kind = request.params?.arguments?.kind;
        if (!new Set(["targets", "kb", "conan"]).has(kind)) {
          error(request.id, -32602, "Invalid configuration kind");
          return;
        }
        const refreshed = resolveBackend();
        if (!refreshed.ok) {
          response = publicStatus(refreshed);
          isError = true;
        } else {
          openConfiguration(refreshed, kind).then((url) => {
            const opened = {
              schema: status.schema,
              status: "configuration_page_ready",
              kind,
              url,
              next_action: "Open the loopback URL and keep this task running while editing.",
            };
            reply(request.id, {
              content: [{ type: "text", text: JSON.stringify(opened) }],
              structuredContent: opened,
              isError: false,
            });
          }).catch((configurationError) => {
            const failed = { ...status, reason: configurationError.message };
            reply(request.id, {
              content: [{ type: "text", text: JSON.stringify(failed) }],
              structuredContent: failed,
              isError: true,
            });
          });
          return;
        }
      } else if (request.params?.name === "openubmc_setup_repair_configuration") {
        const refreshed = resolveBackend();
        if (!refreshed.ok) {
          response = publicStatus(refreshed);
          isError = true;
        } else {
          const repaired = repairConfiguration(refreshed);
          if (repaired.ok) {
            response = {
              schema: status.schema,
              status: "configuration_repaired",
              operations: repaired.operations,
              next_action: "Start a new Codex task and check openUBMC Runtime and knowledge service health.",
            };
          } else {
            response = publicStatus({ ...refreshed, ...repaired });
            isError = true;
          }
        }
      } else if (request.params?.name !== "openubmc_setup_status") {
        error(request.id, -32602, "Unknown setup tool");
        return;
      }
      reply(request.id, {
        content: [{ type: "text", text: JSON.stringify(response) }],
        structuredContent: response,
        isError,
      });
      return;
    }
    if (request.method === "resources/list" || request.method === "prompts/list") {
      reply(request.id, { [request.method.startsWith("resources") ? "resources" : "prompts"]: [] });
      return;
    }
    error(request.id, -32601, "Method not found");
  });
  lineReader.once("close", () => {
    for (const child of configurationProcesses) child.kill();
  });
}

const backend = resolveBackend();
if (!backend.ok) {
  serveSetup(backend);
} else {
  const preflight = preflightBackend(backend);
  if (!preflight.ok) serveSetup({ ...backend, ...preflight, ok: false });
  else proxyBackend(backend);
}
