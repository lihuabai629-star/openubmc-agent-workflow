import { App } from "@modelcontextprotocol/ext-apps";
import { presentResult } from "../src/panel.js";

const $ = id => document.getElementById(id);
const readTool = document.querySelector('meta[name="openubmc-read-tool"]')?.content;
const stateNames = { gate: "等待输入", running: "处理中", incident: "需要处理", completed: "已完成", failed: "未完成", cancelled: "已取消", unknown: "已收到回执" };
let bridge, lastRun = "", selectedTab = "workflow", busy = false, lastResultError = false;

function notice(text = "") { $("notice").textContent = text; $("notice").hidden = !text; }
function tab(name) {
  selectedTab = name;
  for (const id of ["workflow", "knowledge"]) { $(id).hidden = id !== name; $(`${id}-tab`).setAttribute("aria-selected", String(id === name)); }
  persist();
}
function persist() {
  if (bridge?.save) bridge.save({ tab: selectedTab, runId: $("run-id").value, query: $("query").value });
}
function list(id, items, text) {
  const nodes = items.slice(0, 50).map(item => { const li = document.createElement("li"); li.textContent = text(item); return li; });
  $(id).replaceChildren(...nodes);
}
function render(result) {
  const view = presentResult(result);
  if (view.tool.startsWith("openubmc_kb_") || view.knowledge) {
    tab("knowledge");
    $("kb-empty").hidden = true;
    $("kb-answer").hidden = false;
    $("kb-answer").textContent = view.knowledge || view.summary || "未返回匹配资料。";
    list("references", view.references, item => `${item.reference_id ?? "来源"}  ${item.file_path ?? item.title ?? ""}`);
  } else if (view.tool !== "openubmc_panel" && view.tool !== "runtime_status") {
    tab("workflow");
    lastRun = view.runId || lastRun;
    if (lastRun) $("run-id").value = lastRun;
    $("state").textContent = view.completed ? "已完成" : view.state === "completed" ? "待确认" : stateNames[view.state] ?? "已收到回执";
    $("state").classList.toggle("complete", view.completed);
    $("receipt-title").textContent = view.gate ? "等待当前步骤的输入" : view.incident ? "工作流需要处理" : view.completed ? "工作流已完成" : "最新回执";
    $("summary").textContent = view.summary || "请查看回执中的证据与当前步骤。";
    $("gate").hidden = !view.gate; $("gate").textContent = `当前步骤：${view.gate}`;
    $("incident").hidden = !view.incident; $("incident").textContent = view.incident;
    const evidence = Array.isArray(view.evidence) ? view.evidence : [];
    list("evidence", evidence, item => typeof item === "string" ? item : item.summary ?? item.evidence_id ?? item.handle ?? JSON.stringify(item));
    $("no-evidence").hidden = evidence.length > 0;
    $("evidence-count").textContent = evidence.length ? `${evidence.length} 条` : "";
    $("details").hidden = false;
    $("raw").textContent = JSON.stringify(view.data, null, 2).slice(0, 32_768);
    lastResultError = Boolean(result.isError);
    $("continue").disabled = lastResultError || !bridge?.message || !lastRun;
  }
  if (view.services) {
    const labels = { runtime: "工作流", operator: "证据", kb: "知识库" };
    const statuses = { connected: "已连接", not_started: "未启动", not_configured: "未配置", unavailable: "不可用", disconnected: "已断开", setup_required: "需配置", incompatible: "接口不匹配" };
    $("service-note").textContent = Object.entries(view.services).map(([key, value]) => `${labels[key] ?? key}：${statuses[value] ?? "未知"}`).join(" / ");
  }
  if (result.isError) notice(view.summary || "请求未完成，请检查连接后重试。");
  persist();
}
function hostContext(context = {}) {
  if (["light", "dark"].includes(context.theme)) document.documentElement.dataset.theme = context.theme;
  const modes = context.availableDisplayModes ?? ["inline", "pip", "fullscreen"];
  $("pin").hidden = !bridge || !modes.includes("pip");
  $("expand").hidden = !bridge || !modes.includes("fullscreen");
  $("mode").textContent = ({ pip: "保持显示", fullscreen: "已展开" })[context.displayMode] ?? "";
}
function connected(connection, context = {}, label = "已连接宿主") {
  bridge = readTool ? { ...connection, call: (name, args) => connection.call(readTool, { name, arguments: args }) } : connection;
  $("connection").textContent = label;
  $("connection-dot").classList.add("ready");
  for (const id of ["load-run", "search", "refresh"]) $(id).disabled = false;
  $("continue").disabled = lastResultError || !connection.message || !lastRun;
  hostContext(context);
}
async function call(name, args = {}) {
  if (!bridge || busy) return;
  busy = true; notice("正在读取…");
  for (const id of ["load-run", "search", "refresh"]) $(id).disabled = true;
  try { const result = await bridge.call(name, args); notice(); render(result); }
  catch { notice("读取未完成。请确认连接器和对应服务可用，再重新读取。"); }
  finally { busy = false; for (const id of ["load-run", "search", "refresh"]) $(id).disabled = !bridge; }
}
for (const name of ["workflow", "knowledge"]) $(`${name}-tab`).addEventListener("click", () => tab(name));
$("run-form").addEventListener("submit", event => { event.preventDefault(); const id = $("run-id").value.trim(); if (id) void call("case_read", { case_id: id }); });
$("search-form").addEventListener("submit", event => { event.preventDefault(); const query = $("query").value.trim(); if (query) void call("openubmc_kb_query", { query, only_need_context: false, include_references: true }); });
$("refresh").addEventListener("click", () => void call("runtime_status"));
$("continue").addEventListener("click", async () => {
  if (!bridge || !lastRun) return;
  try { await bridge.message(`请继续处理 openUBMC Run ${lastRun}，沿用已有授权，并以 Runtime 返回的下一步为准。`); }
  catch { notice("未能发送到对话，请在聊天中继续该 Run。"); }
});
for (const [id, mode] of [["pin", "pip"], ["expand", "fullscreen"]]) $(id).addEventListener("click", async () => {
  if (!bridge) return;
  try {
    const response = await bridge.display(mode);
    if (response.mode !== mode) return notice("当前窗口未切换到请求的显示模式。");
    $("mode").textContent = mode === "pip" ? "保持显示" : "已展开"; notice();
  } catch { notice("当前窗口不支持此显示模式，可继续在当前面板操作。"); }
});

async function initialize() {
  const localToken = document.querySelector('meta[name="openubmc-preview-token"]')?.content;
  if (window.openai?.callTool) {
    const api = window.openai;
    connected({ call: (name, args) => api.callTool(name, args), display: mode => api.requestDisplayMode({ mode }),
      message: prompt => api.sendFollowUpMessage({ prompt }), save: value => api.setWidgetState?.(value) }, api, "已连接 ChatGPT");
    const saved = api.widgetState;
    if (saved) { $("run-id").value = saved.runId ?? ""; $("query").value = saved.query ?? ""; tab(saved.tab === "knowledge" ? "knowledge" : "workflow"); }
    const update = () => { hostContext(api); if (api.toolOutput) render({ structuredContent: api.toolOutput, _meta: api.toolResponseMetadata }); };
    window.addEventListener("openai:set_globals", update); update();
  } else if (localToken) {
    connected({ call: async (name, args) => {
      const response = await fetch("/preview/call", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "x-openubmc-preview-token": localToken },
        body: JSON.stringify({ name, arguments: args }) });
      if (!response.ok) throw new Error("LOCAL_PANEL_UNAVAILABLE");
      return response.json();
    } }, { availableDisplayModes: [] }, "本地模式");
    $("local-note").hidden = false;
    $("service-note").textContent = "查询时连接本机插件";
  } else if (window.parent !== window) {
    const app = new App({ name: "openubmc-panel", version: "0.1.0" }, {}, { autoResize: true });
    app.ontoolresult = result => render(result);
    app.onhostcontextchanged = context => hostContext(context);
    app.ontoolcancelled = () => notice("本次请求已取消。已有 Run 的状态请以 Runtime 回执为准。");
    app.onteardown = async () => { bridge = undefined; return {}; };
    try {
      await app.connect(undefined, { timeout: 10_000 });
      connected({ call: (name, args) => app.callServerTool({ name, arguments: args }),
        display: mode => app.requestDisplayMode({ mode }),
        message: text => app.sendMessage({ role: "user", content: [{ type: "text", text }] }) }, app.getHostContext(), `已连接 ${app.getHostVersion()?.name || "宿主"}`);
    } catch { notice("未能连接宿主，请重新打开 openUBMC 面板。"); }
  } else {
    $("connection").textContent = "本地预览";
    $("service-note").textContent = "在 Codex 或 ChatGPT 中连接后可读取工作流和知识库";
  }
}
void initialize();
