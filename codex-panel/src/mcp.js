import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema, ListResourcesRequestSchema, ReadResourceRequestSchema, McpError, ErrorCode } from "@modelcontextprotocol/sdk/types.js";
import { PANEL_URI, LEGACY_PANEL_URI, PANEL_ICON, PANEL_READ_TOOL, toolWithPanel, panelResult, panelResource } from "./panel.js";

const PANEL_READ_ROUTES = new Map([
  ["runtime_status", "operator"], ["case_read", "operator"],
  ["evidence_query", "operator"], ["evidence_read", "operator"],
  ["openubmc_kb_query", "kb"], ["openubmc_kb_status", "kb"], ["openubmc_kb_list", "kb"]
]);

export function createMcpServer(backends, html) {
  const server = new Server({ name: "openubmc-panel", version: "0.1.0" }, {
    capabilities: { tools: {}, resources: {} },
    instructions: "Use openubmc_panel to show the openUBMC panel. The private read tool connects on demand. Display modes and placement are chosen by the host. Runtime state remains authoritative."
  });
  const panelTool = toolWithPanel({
    name: "openubmc_panel", title: "openUBMC",
    icons: [{ src: PANEL_ICON, mimeType: "image/svg+xml", sizes: ["any"] }],
    description: "Show the interactive openUBMC workflow and knowledge-base panel. Does not connect to any target or advance a Run.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false }
  }, "panel");
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
  }, "operator");
  // Global app discovery must not wait for a backend to start or authenticate.
  // Validate the live read catalog only when the user requests a read.
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [panelTool, readTool] }));
  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    if (request.params.name === "openubmc_panel") {
      return panelResult({
        content: [{ type: "text", text: "openUBMC 面板已打开。可查看工作流回执与知识库，并请求保持显示或展开。" }],
        structuredContent: { kind: "panel", services: backends.status(), available_tools: [...PANEL_READ_ROUTES.keys()] }
      }, "openubmc_panel", backends.status());
    }
    if (request.params.name !== PANEL_READ_TOOL) throw new McpError(ErrorCode.InvalidParams, "Tool is not available in this panel.");
    const input = request.params.arguments ?? {};
    if (Object.keys(input).some(key => !["name", "arguments"].includes(key))) throw new McpError(ErrorCode.InvalidParams, "Invalid read request.");
    const name = input.name, kind = PANEL_READ_ROUTES.get(name), args = input.arguments ?? {};
    if (!args || typeof args !== "object" || Array.isArray(args)) throw new McpError(ErrorCode.InvalidParams, "Read arguments must be an object.");
    if (!kind) throw new McpError(ErrorCode.InvalidParams, "Tool is not available in this connector.");
    try {
      if (!(await backends.tools(kind)).some(tool => tool.name === name)) throw new Error("BACKEND_UNAVAILABLE");
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
