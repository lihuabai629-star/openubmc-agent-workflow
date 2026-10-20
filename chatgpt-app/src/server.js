import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import express from "express";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { mcpAuthRouter, getOAuthProtectedResourceMetadataUrl } from "@modelcontextprotocol/sdk/server/auth/router.js";
import { requireBearerAuth } from "@modelcontextprotocol/sdk/server/auth/middleware/bearerAuth.js";
import { CallToolRequestSchema, ListToolsRequestSchema, ListResourcesRequestSchema, ReadResourceRequestSchema, isInitializeRequest, McpError, ErrorCode } from "@modelcontextprotocol/sdk/types.js";
import { Backends, backendCommands } from "./backend.js";
import { LocalOAuthProvider, loginSecret } from "./auth.js";
import { PANEL_URI, LEGACY_PANEL_URI, PANEL_ICON, PANEL_READ_TOOL, toolWithPanel, panelResult, panelResource } from "./panel.js";
import { createLocalPreview } from "./local-preview.js";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const loopback = host => ["127.0.0.1", "localhost", "[::1]"].includes(host);
const PANEL_READ_ROUTES = new Map([
  ["runtime_status", "operator"], ["case_read", "operator"],
  ["evidence_query", "operator"], ["evidence_read", "operator"],
  ["openubmc_kb_query", "kb"], ["openubmc_kb_status", "kb"], ["openubmc_kb_list", "kb"]
]);

export function createMcpServer(backends, html, { native = false } = {}) {
  const server = new Server({ name: "openubmc-chatgpt", version: "0.1.0" }, {
    capabilities: { tools: {}, resources: {} },
    instructions: "Use openubmc_panel to show the openUBMC panel. " + (native ? "The private panel read tool connects to read backends on demand. " : "Runtime observe and execute keep their original contracts. ") + "Only act within the user's authorized target and scope. Display modes and panel placement are chosen by the host. Never infer workflow success from transport connectivity."
  });
  const routes = new Map();
  let catalog;
  async function discover() {
    if (!catalog) catalog = (async () => {
      const groups = await Promise.all(["runtime", "operator", "kb"].map(async kind =>
        (await backends.tools(kind)).map(tool => {
          routes.set(tool.name, kind);
          return toolWithPanel(tool, kind, { native });
        })
      ));
      return groups.flat();
    })();
    return catalog;
  }
  const panelTool = toolWithPanel({
    name: "openubmc_panel", title: "openUBMC",
    icons: [{ src: PANEL_ICON, mimeType: "image/svg+xml", sizes: ["any"] }],
    description: "Show the interactive openUBMC workflow and knowledge-base panel. Does not connect to any target or advance a Run.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false }
  }, "panel", { native });
  panelTool.annotations.openWorldHint = false;
  const readTool = toolWithPanel({
    name: PANEL_READ_TOOL,
    description: "Read workflow receipts, evidence, or knowledge from the openUBMC panel.",
    inputSchema: {
      type: "object", additionalProperties: false, required: ["name"],
      properties: {
        name: { type: "string", enum: [...PANEL_READ_ROUTES.keys()] },
        arguments: { type: "object", additionalProperties: true }
      }
    }
  }, "operator", { native: true });
  // Global app discovery must not wait for a backend to start or authenticate.
  // Validate the live read catalog only when the user requests a read.
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: native ? [panelTool, readTool] : [panelTool, ...await discover()] }));
  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    if (request.params.name === "openubmc_panel") {
      if (!native) await discover();
      return panelResult({
        content: [{ type: "text", text: "openUBMC 面板已打开。可查看工作流回执与知识库，并请求保持显示或展开。" }],
        structuredContent: { kind: "panel", services: backends.status(), available_tools: native ? [...PANEL_READ_ROUTES.keys()] : [...routes.keys()] }
      }, "openubmc_panel", backends.status());
    }
    let name = request.params.name, args = request.params.arguments ?? {}, kind;
    if (native) {
      if (name !== PANEL_READ_TOOL) throw new McpError(ErrorCode.InvalidParams, "Tool is not available in this connector.");
      name = args.name;
      kind = PANEL_READ_ROUTES.get(name);
      args = args.arguments ?? {};
      if (!args || typeof args !== "object" || Array.isArray(args)) throw new McpError(ErrorCode.InvalidParams, "Read arguments must be an object.");
    } else {
      await discover();
      kind = routes.get(name);
    }
    if (!kind) throw new McpError(ErrorCode.InvalidParams, "Tool is not available in this connector.");
    try {
      if (native && !(await backends.tools(kind)).some(tool => tool.name === name)) throw new Error("BACKEND_UNAVAILABLE");
      const result = await backends.call(kind, name, args, {
        signal: extra.signal, requestId: extra.requestId
      });
      return panelResult(result, name, backends.status());
    } catch {
      // No automatic replay: the Runtime may already have performed an Effect.
      return panelResult({
        isError: true,
        content: [{ type: "text", text: "后端调用未返回有效结果。请检查主机上的插件状态；已有 Run 应沿用原 run_id 恢复。" }],
        structuredContent: { error: "BACKEND_UNAVAILABLE", retry_performed: false }
      }, name, backends.status());
    }
  });
  server.setRequestHandler(ListResourcesRequestSchema, async () => ({ resources: [PANEL_URI, LEGACY_PANEL_URI].map(uri => ({
    uri, name: "openUBMC panel", mimeType: panelResource(uri, "").mimeType
  })) }));
  server.setRequestHandler(ReadResourceRequestSchema, async request => {
    if (![PANEL_URI, LEGACY_PANEL_URI].includes(request.params.uri)) throw new McpError(ErrorCode.InvalidParams, "Unknown resource.");
    return { contents: [panelResource(request.params.uri, html)] };
  });
  return server;
}

export async function createApp({ origin, key, html, backendFactory, preview = false }) {
  const publicUrl = new URL(origin);
  if ((publicUrl.protocol !== "https:" && !(publicUrl.protocol === "http:" && loopback(publicUrl.hostname))) ||
      publicUrl.pathname !== "/" || publicUrl.search || publicUrl.hash || publicUrl.username || publicUrl.password) {
    throw new Error("Public origin must be an HTTPS origin, or loopback HTTP for local development.");
  }
  const provider = new LocalOAuthProvider(origin, key);
  const app = express();
  app.disable("x-powered-by");
  const sessions = new Map();
  app.use((req, res, next) => {
    res.set({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" });
    let host;
    try { host = new URL(`http://${req.get("host")}`).hostname; } catch { return res.sendStatus(400); }
    if (host !== publicUrl.hostname && !loopback(host)) return res.sendStatus(403);
    const originHeader = req.get("origin");
    if (originHeader && ![publicUrl.origin, "https://chatgpt.com", "https://chat.openai.com"].includes(originHeader) &&
      !(loopback(publicUrl.hostname) && originHeader === `http://${req.get("host")}`)) return res.sendStatus(403);
    next();
  });
  app.use(express.json({ limit: "256kb" }));
  app.use(mcpAuthRouter({ provider, issuerUrl: publicUrl, resourceServerUrl: provider.resource,
    scopesSupported: ["openubmc"], resourceName: "openUBMC" }));
  app.get("/healthz", (_req, res) => res.json({ status: "ok", service: "openubmc-chatgpt" }));
  app.get("/login", (req, res) => {
    res.set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'");
    const page = provider.loginPage(String(req.query.request ?? ""));
    page ? res.type("html").send(page) : res.status(400).send("连接请求已过期，请重新连接。");
  });
  app.post("/login", express.urlencoded({ extended: false, limit: "4kb" }), (req, res) => {
    if (req.get("origin") && req.get("origin") !== publicUrl.origin) return res.sendStatus(403);
    const result = provider.approve(req.body.request, req.body.key, req.socket.remoteAddress);
    result.redirect ? res.redirect(result.redirect) : res.status(result.status).send("密钥无效或请求已过期。连续失败后请等待 15 分钟。");
  });
  const local = preview ? createLocalPreview(html, backendFactory) : undefined;
  if (local) app.use("/preview", local.router);
  const auth = requireBearerAuth({ verifier: provider, requiredScopes: ["openubmc"],
    expectedResource: provider.resource, resourceMetadataUrl: getOAuthProtectedResourceMetadataUrl(provider.resource) });
  app.all("/mcp", auth, async (req, res) => {
    let session = sessions.get(req.get("mcp-session-id"));
    if (req.get("mcp-session-id") && !session) return res.sendStatus(404);
    if (!session) {
      if (req.method !== "POST" || !isInitializeRequest(req.body)) return res.status(400).json({ error: "Initialize an MCP session first." });
      if (sessions.size >= 16) return res.sendStatus(503);
      const backends = backendFactory();
      let created;
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: randomUUID, enableJsonResponse: true,
        onsessioninitialized: id => { sessions.set(id, created); } });
      const server = createMcpServer(backends, html);
      session = { transport, server, backends, clientId: req.auth.clientId, lastUsed: Date.now(), active: 0 };
      await server.connect(transport);
      created = session;
      server.onclose = () => {
        sessions.delete(transport.sessionId);
        void backends.close();
      };
    }
    if (session.clientId !== req.auth.clientId) return res.sendStatus(403);
    session.active++;
    try {
      await session.transport.handleRequest(req, res, req.body);
    } catch {
      if (!res.headersSent) res.status(500).json({ error: "MCP transport failed." });
    } finally {
      session.lastUsed = Date.now();
      session.active--;
    }
  });
  const timer = setInterval(() => {
    for (const session of sessions.values()) if (!session.active && Date.now() - session.lastUsed > 3_600_000) void session.server.close();
  }, 60_000);
  timer.unref();
  app.use((error, _req, res, _next) => res.status(error.status === 413 ? 413 : 400).json({ error: "Invalid request." }));
  return { app, provider, async close() {
    clearInterval(timer);
    await local?.close();
    await Promise.allSettled([...sessions.values()].map(async session => {
      await session.server.close();
      await session.backends.close();
    }));
    sessions.clear();
  } };
}

async function main() {
  const { values } = parseArgs({ options: {
    port: { type: "string", default: process.env.OPENUBMC_CHATGPT_PORT || "8765" },
    host: { type: "string", default: "127.0.0.1" },
    "public-origin": { type: "string", default: process.env.OPENUBMC_CHATGPT_PUBLIC_ORIGIN },
    "plugin-root": { type: "string", default: process.env.OPENUBMC_PLUGIN_ROOT },
    preview: { type: "boolean", default: false }
  } });
  const port = Number(values.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid port.");
  const commands = backendCommands(values["plugin-root"]);
  const directory = path.join(process.env.XDG_DATA_HOME || path.join(homedir(), ".local/share"), "openubmc/chatgpt-app");
  const login = await loginSecret(directory);
  const html = await readFile(path.join(ROOT, "dist/panel.html"), "utf8");
  const service = await createApp({ origin: values["public-origin"] || `http://127.0.0.1:${port}`,
    key: login.value, html, preview: values.preview, backendFactory: () => new Backends(commands) });
  const listener = service.app.listen(port, values.host, () => {
    process.stdout.write(`openUBMC MCP: ${values["public-origin"] || `http://127.0.0.1:${port}`}/mcp\n`);
    process.stdout.write(`Login key file (private): ${login.file}\n`);
    if (values.preview) process.stdout.write(`Local panel preview: http://127.0.0.1:${port}/preview\n`);
  });
  listener.on("error", () => { process.stderr.write("Could not bind the local connector port.\n"); process.exitCode = 1; });
  async function shutdown() {
    listener.close();
    await service.close();
    listener.closeAllConnections();
  }
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(() => {
  process.stderr.write("Connector startup failed. Check the plugin root, public origin, port, and built panel.\n");
  process.exitCode = 1;
});
