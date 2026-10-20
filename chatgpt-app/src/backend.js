import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const READ_TOOLS = new Set(["runtime_status", "case_read", "evidence_read", "evidence_query"]);

export function backendCommands(pluginRoot) {
  if (!pluginRoot) return {};
  const root = path.resolve(pluginRoot);
  const launcher = path.join(root, "scripts/openubmc-mcp-bootstrap.js");
  if (!existsSync(launcher)) throw new Error("OPENUBMC_PLUGIN_ROOT must identify an installed plugin.");
  return Object.fromEntries(["runtime", "operator", "kb"].map(kind => [kind, {
    // The bootstrap qualifies the Agent catalog. Operator uses the same verified
    // package launcher directly so its disjoint catalog is not mistaken for setup failure.
    command: kind === "operator" ? (process.env.OPENUBMC_PLUGIN_PYTHON || (process.platform === "win32" ? "python" : "python3")) : process.execPath,
    args: kind === "operator" ? ["-I", "-B", path.join(root, "scripts/pluginctl.py"), "runtime"] : [launcher, kind === "kb" ? "kb" : "runtime"],
    cwd: root,
    env: { OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE: kind === "operator" ? "operator" : "agent" }
  }]));
}

// Only transport clients and task identity live here. The Runtime owns every Run transition.
export class Backends {
  constructor(commands = {}, taskId = `chatgpt-${randomUUID()}`) {
    this.commands = commands;
    this.taskId = taskId;
    this.clients = new Map();
    this.pending = new Map();
    this.states = {};
    this.closed = false;
  }

  async get(kind) {
    if (this.closed) throw new Error("BACKEND_CLOSED");
    if (this.clients.has(kind)) return this.clients.get(kind);
    if (this.pending.has(kind)) return this.pending.get(kind);
    const command = this.commands[kind];
    if (!command) throw new Error("BACKEND_NOT_CONFIGURED");
    const pending = (async () => {
      const client = new Client({ name: "openubmc-chatgpt", version: "0.1.0" });
      const transport = new StdioClientTransport({
        ...command,
        env: { ...process.env, ...command.env, OPENUBMC_MCP_TASK_ID: this.taskId },
        // Backend stderr may contain private local diagnostics; never relay it into HTTP logs.
        stderr: "ignore"
      });
      try {
        await client.connect(transport, { timeout: 600_000 });
        if (this.closed) throw new Error("BACKEND_CLOSED");
        this.clients.set(kind, client);
        this.states[kind] = "connected";
        client.onclose = () => {
          this.clients.delete(kind);
          this.states[kind] = "disconnected";
        };
        return client;
      } catch (error) {
        await transport.close().catch(() => {});
        this.states[kind] = "unavailable";
        throw error;
      } finally {
        this.pending.delete(kind);
      }
    })();
    this.pending.set(kind, pending);
    return pending;
  }

  async tools(kind) {
    try {
      const client = await this.get(kind);
      const { tools } = await client.listTools({}, { timeout: 60_000 });
      const selected = tools.filter(tool => {
        if (kind === "runtime") return ["observe", "execute"].includes(tool.name);
        if (kind === "operator") return READ_TOOLS.has(tool.name);
        return ["openubmc_kb_query", "openubmc_kb_status", "openubmc_kb_list"].includes(tool.name);
      });
      this.states[kind] = selected.length ? "connected" : tools.some(tool => tool.name === "openubmc_setup_status") ? "setup_required" : "incompatible";
      return selected;
    } catch {
      this.states[kind] = this.commands[kind] ? "unavailable" : "not_configured";
      return [];
    }
  }

  async call(kind, name, args, options = {}) {
    const client = await this.get(kind);
    return client.callTool({
      name,
      arguments: args,
      // Never accept caller-supplied task or operation bindings.
      _meta: {
        task_id: this.taskId,
        "openubmc/operationId": "chatgpt-" + createHash("sha256")
          .update(JSON.stringify([this.taskId, options.requestId ?? randomUUID()])).digest("hex").slice(0, 32)
      }
    }, undefined, { timeout: 900_000, signal: options.signal });
  }

  status() {
    return Object.fromEntries(["runtime", "operator", "kb"].map(kind => [kind,
      this.states[kind] ?? (this.commands[kind] ? "not_started" : "not_configured")
    ]));
  }

  async close() {
    this.closed = true;
    await Promise.allSettled(this.pending.values());
    await Promise.allSettled([...this.clients.values()].map(client => client.close()));
    this.clients.clear();
  }
}
