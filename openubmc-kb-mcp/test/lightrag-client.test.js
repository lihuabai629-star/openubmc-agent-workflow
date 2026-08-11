import test from "node:test";
import assert from "node:assert/strict";

import { LightRagClient } from "../src/lightrag-client.js";

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
}

test("query sends the expected body and bearer token", async () => {
  const calls = [];
  const auth = { getAccessToken: async () => "token" };
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com" }, auth, async (url, options) => {
    calls.push({ url: String(url), options });
    return json({ answer: "ok" });
  });

  const result = await client.query({ query: "风扇调速", mode: "mix", top_k: 5, chunk_top_k: 3 });
  assert.equal(result.answer, "ok");
  assert.equal(calls[0].url, "https://kb.example.com/api/v1/rag/retrieve");
  assert.equal(calls[0].options.headers.authorization, "Bearer token");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    query: "风扇调速",
    mode: "mix",
    top_k: 5,
    chunk_top_k: 3,
    include_references: false,
    enable_rerank: true,
    only_need_context: true,
    only_need_prompt: false,
    stream: false
  });
});

test("status and list use the community API endpoint shapes", async () => {
  const urls = [];
  const auth = { getAccessToken: async () => "token" };
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com" }, auth, async (url) => {
    urls.push(String(url));
    return json({ ok: true });
  });
  const status = await client.status();
  await client.list({ page: 2, page_size: 10, status_filter: "COMPLETED" });
  assert.deepEqual(status, {
    configured: true,
    endpoint: "https://kb.example.com",
    pipeline: {
      ok: true,
      history_messages: [],
      history_total: 0,
      history_truncated: false
    },
    counts: { ok: true }
  });
  assert.deepEqual(urls, [
    "https://kb.example.com/api/v1/rag/documents/pipeline_status",
    "https://kb.example.com/api/v1/rag/documents/status_counts",
    "https://kb.example.com/api/v1/rag/documents/paginated"
  ]);
});

test("status and document pages are bounded for agent context", async () => {
  const auth = { getAccessToken: async () => "token" };
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com" }, auth, async url => {
    if (String(url).endsWith("pipeline_status")) {
      return json({ history_messages: Array.from({ length: 25 }, (_, index) => `event-${index}`) });
    }
    if (String(url).endsWith("status_counts")) return json({ processed: 1 });
    return json({
      documents: [{
        id: "doc-1",
        file_path: "topic.json",
        status: "processed",
        content_summary: "s".repeat(1000),
        error_msg: "e".repeat(500),
        metadata: { private: "omitted" }
      }],
      pagination: { page: 1, total_pages: 1 }
    });
  });

  const status = await client.status();
  const page = await client.list();

  assert.equal(status.pipeline.history_messages.length, 10);
  assert.equal(status.pipeline.history_total, 25);
  assert.equal(status.pipeline.history_truncated, true);
  assert.equal(page.documents[0].content_summary.length, 600);
  assert.equal(page.documents[0].error_msg.length, 300);
  assert.equal("metadata" in page.documents[0], false);
});

test("does not contact LightRAG when authentication fails", async () => {
  let requests = 0;
  const auth = { getAccessToken: async () => { throw new Error("captcha required"); } };
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com" }, auth, async () => {
    requests += 1;
    return json({});
  });
  await assert.rejects(() => client.query({ query: "x" }), /captcha required/);
  assert.equal(requests, 0);
});

test("clears the token and retries once after 401", async () => {
  let tokenCalls = 0;
  let clears = 0;
  let requests = 0;
  const auth = {
    getAccessToken: async () => `token-${++tokenCalls}`,
    clearToken: () => { clears += 1; }
  };
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com" }, auth, async () => {
    requests += 1;
    return requests === 1 ? json({ error: "expired" }, 401) : json({ answer: "retried" });
  });
  assert.equal((await client.query({ query: "x" })).answer, "retried");
  assert.equal(clears, 1);
  assert.equal(requests, 2);
});
