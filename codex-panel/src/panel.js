export const PANEL_URI = "ui://openubmc/panel-v1.html";
export const LEGACY_PANEL_URI = "ui://openubmc/panel-v1-legacy.html";
export const PANEL_META = "openubmc/panel";
export const PANEL_READ_TOOL = "openubmc_panel_read";
export const PANEL_ICON = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#1f609f" stroke-width="1.6"><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M6 6.5h2m3 0h7M6 17.5h2m3 0h7"/></svg>');

export function toolWithPanel(tool, kind) {
  const readOnly = kind !== "runtime" || tool.name === "observe";
  const appOnly = kind !== "panel";
  return {
    ...tool,
    annotations: {
      ...tool.annotations,
      readOnlyHint: readOnly,
      destructiveHint: !readOnly,
      idempotentHint: readOnly,
      openWorldHint: true
    },
    _meta: {
      ...tool._meta,
      ui: { resourceUri: PANEL_URI, visibility: appOnly ? ["app"] : ["app", "model"] },
      ...(kind === "panel" ? { "openai/ui": { entrypoints: [{ type: "global" }] } } : {}),
      "openai/outputTemplate": LEGACY_PANEL_URI,
      "openai/widgetAccessible": true,
      ...(appOnly ? { "openai/visibility": "private" } : {})
    }
  };
}

export function panelResult(result, tool, services) {
  return {
    ...result,
    _meta: { ...result._meta, [PANEL_META]: { tool, services } }
  };
}

export function panelResource(uri, html) {
  return {
    uri,
    mimeType: uri === LEGACY_PANEL_URI ? "text/html+skybridge" : "text/html;profile=mcp-app",
    text: html,
    _meta: {
      ui: { prefersBorder: false, csp: { connectDomains: [], resourceDomains: [] } },
      "openai/widgetDescription": "openUBMC workflow, observations, evidence, and knowledge-base panel.",
      "openai/widgetPrefersBorder": false,
      "openai/widgetCSP": { connect_domains: [], resource_domains: [] }
    }
  };
}

// A display projection, never an authority for execution or success.
export function presentResult(result = {}) {
  const data = result.structuredContent ?? {};
  const tool = result._meta?.[PANEL_META]?.tool ?? "";
  const receipt = data.turn ?? (data.current_turn?.state ? data.current_turn : data);
  const outcome = receipt.outcome ?? data.run_outcome;
  const gate = receipt.gate ?? data.current_gate;
  const incident = receipt.incident ?? data.current_incident;
  const text = (result.content ?? []).filter(item => item.type === "text").map(item => item.text).join("\n");
  const state = result.isError ? "failed" : receipt.state ?? receipt.status ?? "unknown";
  return {
    tool,
    state,
    runId: receipt.run_id ?? data.case_id ?? "",
    summary: outcome?.summary ?? receipt.summary ?? text,
    gate: gate?.name ?? "",
    incident: incident?.summary ?? incident?.code ?? "",
    evidence: receipt.diagnostic_receipt?.evidence ?? receipt.evidence ?? data.evidence_refs ?? [],
    knowledge: data.result?.response ?? "",
    references: data.result?.references ?? [],
    services: result._meta?.[PANEL_META]?.services ?? data.services,
    // A partial receipt or an unrecognized state never becomes a success badge.
    completed: !result.isError && (receipt.state === "completed" || data.status === "terminal") && outcome?.status === "completed",
    data
  };
}
