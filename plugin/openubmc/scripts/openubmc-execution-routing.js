"use strict";

// Transport routing only. Runtime owns Run, Gate, Effect, and Outcome state.
// The bootstrap cannot observe or authorize shell calls made by the host.
const crypto = require("crypto");

const SCHEMA = "openubmc.execution-routing/v1";
const RUNTIME_TOOLS = new Set(["observe", "execute"]);

function runtimeProtocolHealthy(report) {
  const health = report?.mcp_health?.runtime;
  if (health?.ok !== true) return false;
  // pluginctl.doctor obtains the names from a bounded MCP initialize/tools/list
  // probe. A missing list cannot establish this Agent Interface.
  return Array.isArray(health.tools)
    && health.tools.includes("observe") && health.tools.includes("execute");
}

function operationFor(request) {
  if (request?.method !== "tools/call") return null;
  const name = request?.params?.name;
  if (!RUNTIME_TOOLS.has(name)) return null;
  if (name === "observe") return "evidence";
  const argumentsValue = request?.params?.arguments;
  const intent = argumentsValue && typeof argumentsValue === "object" ? argumentsValue.intent : null;
  if (intent === "upgrade-and-verify") return "upgrade";
  if (intent === "diagnosis-only" || intent === "diagnose-and-fix") return "diagnose";
  // A continuation is bound to its durable Run; the transport cannot safely
  // reconstruct its original intent from this single message.
  return "runtime-execute";
}

function routeRuntimeCall(request, { report, hostPlatform, selectedWsl = null, sequence }) {
  const operation = operationFor(request);
  if (operation === null) return null;
  if (!runtimeProtocolHealthy(report)) {
    return { allowed: false, reason_code: "mcp_protocol_unhealthy" };
  }
  const argumentsValue = request?.params?.arguments ?? {};
  const scopeDigest = crypto.createHash("sha256")
    .update(JSON.stringify(argumentsValue)).digest("hex");
  const receipt = {
    schema: SCHEMA,
    receipt_id: crypto.randomUUID(),
    path: "structured-runtime-mcp",
    operation,
    tool: request.params.name,
    client_environment: hostPlatform === "win32" ? "windows" : hostPlatform,
    execution_host: hostPlatform === "win32" ? "wsl" : hostPlatform,
    selected_wsl: hostPlatform === "win32" ? selectedWsl : null,
    requested_scope_digest: `sha256:${scopeDigest}`,
    evidence_boundary: "typed_runtime_mcp_result",
    protocol: { healthy: true, source: "pluginctl.doctor", probe: "initialize/tools/list" },
    fallback: null,
    structured_call_number: sequence,
  };
  return { allowed: true, receipt };
}

module.exports = { routeRuntimeCall, runtimeProtocolHealthy };
