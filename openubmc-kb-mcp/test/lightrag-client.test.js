import test from "node:test";
import assert from "node:assert/strict";

import { LightRagClient } from "../src/lightrag-client.js";
import { OneIdClient } from "../src/auth/oneid-client.js";

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
}

function memoryStore(initial) {
  let value = initial ? structuredClone(initial) : undefined;
  return {
    load: async () => value ? structuredClone(value) : undefined,
    save: async token => { value = token ? structuredClone(token) : undefined; },
    clear: async () => { value = undefined; }
  };
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
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com", knowledgeMcpVersion: "1.3.0" }, auth, async (url) => {
    urls.push(String(url));
    return json({ ok: true });
  });
  const status = await client.status();
  await client.list({ page: 2, page_size: 10, status_filter: "COMPLETED" });
  assert.deepEqual(status, {
    configured: true,
    version: "1.3.0",
    endpoint: "https://kb.example.com",
    pipeline: { ok: true },
    counts: { ok: true }
  });
  assert.deepEqual(urls, [
    "https://kb.example.com/api/v1/rag/documents/pipeline_status",
    "https://kb.example.com/api/v1/rag/documents/status_counts",
    "https://kb.example.com/api/v1/rag/documents/paginated"
  ]);
});

test("status and list retain upstream data for the MCP projection", async () => {
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

  assert.equal(status.pipeline.history_messages.length, 25);
  assert.equal(page.documents[0].content_summary.length, 1000);
  assert.equal(page.documents[0].error_msg.length, 500);
  assert.equal("metadata" in page.documents[0], true);
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

test("coalesces concurrent 401 invalidation by token identity", async () => {
  let current = "old-token";
  let invalidations = 0;
  let requests = 0;
  const auth = {
    getAccessToken: async () => current,
    invalidateAccessToken: async token => {
      if (token === current) {
        invalidations += 1;
        current = "new-token";
      }
    }
  };
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com" }, auth, async (_url, options) => {
    requests += 1;
    if (options.headers.authorization === "Bearer old-token") return json({ error: "expired" }, 401);
    return json({ answer: "shared retry" });
  });

  const results = await Promise.all([
    client.query({ query: "one" }),
    client.query({ query: "two" })
  ]);
  assert.deepEqual(results.map(result => result.answer), ["shared retry", "shared retry"]);
  assert.equal(invalidations, 1);
  assert.equal(requests, 4);
});

test("coalesces concurrent 401 retries through the real token refresh client", async () => {
  const now = 1_700_000_000_000;
  const tokenStore = memoryStore({
    accessToken: "expired-access",
    refreshToken: "refresh-token",
    expiresAt: now + 600_000
  });
  let refreshes = 0;
  const auth = new OneIdClient({
    userCenterUrl: "https://usercenter.example.com",
    oauthBaseUrl: "https://oauth.example.com",
    tokenEndpoint: "https://oauth.example.com/token",
    clientId: "client",
    clientSecret: "secret",
    username: "user",
    password: "password",
    knowledgeMcpVersion: "1.3.0",
    credentialsConfigured: true
  }, {
    now: () => now,
    tokenStore,
    fetch: async url => {
      assert.equal(String(url), "https://oauth.example.com/token");
      refreshes += 1;
      await new Promise(resolve => setImmediate(resolve));
      return json({ access_token: "refreshed-access", refresh_token: "refresh-token", expires_in: 3600 });
    }
  });
  let requests = 0;
  const client = new LightRagClient({ lightragUrl: "https://kb.example.com", knowledgeMcpVersion: "1.3.0" }, auth,
    async (_url, options) => {
      requests += 1;
      if (options.headers.authorization === "Bearer expired-access") return json({ error: "expired" }, 401);
      return json({ answer: "shared retry" });
    });

  const results = await Promise.all([
    client.query({ query: "one" }),
    client.query({ query: "two" })
  ]);
  assert.deepEqual(results.map(result => result.answer), ["shared retry", "shared retry"]);
  assert.equal(refreshes, 1);
  assert.equal(requests, 4);
});
