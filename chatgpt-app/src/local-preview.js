import { randomUUID } from "node:crypto";
import express from "express";
import { panelResult } from "./panel.js";

const TOOLS = new Map([
  ["openubmc_kb_query", "kb"], ["openubmc_kb_status", "kb"],
  ["case_read", "operator"], ["runtime_status", "operator"]
]);
const loopback = host => ["127.0.0.1", "localhost", "[::1]"].includes(host);

// Local browser access is separate from the OAuth-protected MCP endpoint.
export function createLocalPreview(html, backendFactory) {
  const router = express.Router();
  const token = randomUUID();
  const meta = `<meta name="openubmc-preview-token" content="${token}">`;
  const page = html.includes("</head>") ? html.replace("</head>", `${meta}</head>`) : meta + html;
  let backends, active = 0;
  router.use((req, res, next) => {
    const host = new URL(`http://${req.get("host")}`).hostname;
    if (!loopback(host) || !["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(req.socket.remoteAddress)) return res.sendStatus(403);
    next();
  });
  router.get("/", (_req, res) => res.type("html").send(page));
  router.post("/call", async (req, res) => {
    if (req.get("x-openubmc-preview-token") !== token) return res.sendStatus(403);
    if (req.get("origin") && req.get("origin") !== `http://${req.get("host")}`) return res.sendStatus(403);
    const { name, arguments: args = {} } = req.body ?? {};
    const kind = TOOLS.get(name);
    if (!kind) return res.status(403).json({ error: "This tool is not available in the local panel." });
    if (!args || typeof args !== "object" || Array.isArray(args)) return res.sendStatus(400);
    if (active >= 4) return res.sendStatus(429);
    active++;
    try {
      backends ??= backendFactory();
      const tools = await backends.tools(kind);
      if (!tools.some(tool => tool.name === name)) throw new Error("BACKEND_UNAVAILABLE");
      const result = await backends.call(kind, name, args, { requestId: randomUUID() });
      res.json(panelResult(result, name, backends.status()));
    } catch {
      res.json(panelResult({ isError: true,
        content: [{ type: "text", text: kind === "kb" ? "知识库服务暂不可用，请检查本机插件中的知识库账号和网络后重试。" : "无法读取本机工作流，请检查插件状态与 Run ID。" }],
        structuredContent: { error: "BACKEND_UNAVAILABLE", retry_performed: false }
      }, name, backends?.status()));
    } finally { active--; }
  });
  return { router, async close() { await backends?.close(); } };
}
