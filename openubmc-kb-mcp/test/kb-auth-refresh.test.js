import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { OneIdClient } from "../src/auth/oneid-client.js";
import { LightRagClient } from "../src/lightrag-client.js";

const now = 1_700_000_000_000;
const { publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const publicPem = publicKey.export({ type: "spki", format: "pem" });
const config = {
  credentialsConfigured: true,
  userCenterUrl: "https://example.invalid",
  tokenEndpoint: "https://example.invalid/oneid/oidc/token",
  lightragUrl: "https://kb.example.invalid",
  clientId: "fixture-client",
  clientSecret: "fixture-secret",
  username: "fixture-account",
  password: "fixture-password",
  redirectUri: "openubmc://fixture/callback",
  scopes: ["openid", "offline_access"]
};
const expiredToken = { accessToken: "expired-access", refreshToken: "expired-refresh", expiresAt: now - 1 };
const renewedToken = { accessToken: "renewed-access", refreshToken: "renewed-refresh", expiresAt: now + 3_600_000 };
const answer = { response: "Fixture KB context", references: [{ reference_id: "1", file_path: "fixture.md" }] };
const question = { query: "What is MDB?", include_references: true };

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function fixture(options = {}) {
  const initial = options.initial ?? expiredToken;
  let stored = structuredClone(initial);
  let clears = 0;
  const calls = [];
  const queries = [];
  const tokenStore = {
    async load() { return structuredClone(stored); },
    async save(token) { stored = structuredClone(token); },
    async clear() { stored = undefined; clears++; }
  };
  const fetch = async (url, request) => {
    request.signal?.throwIfAborted();
    const href = String(url);
    if (href === config.tokenEndpoint) {
      const body = new URLSearchParams(request.body);
      const refresh = body.get("grant_type") === "refresh_token";
      calls.push(refresh ? "refresh" : "exchange");
      if (refresh) {
        assert.equal(body.get("refresh_token"), initial.refreshToken);
        if (options.onRefresh) return options.onRefresh(request);
        return json(options.refreshBody ?? { error: "Bad Request", message: "token invalid or expired" }, options.refreshStatus ?? 400);
      }
      assert.equal(body.get("grant_type"), "authorization_code");
      assert.equal(body.get("code"), "fixture-code");
      return json({ access_token: renewedToken.accessToken, refresh_token: renewedToken.refreshToken, expires_in: 3600 });
    }
    if (href.endsWith("/oneid/captcha/checkLogin")) {
      calls.push("captcha");
      if (options.onCaptcha) await options.onCaptcha(request);
      return json({ data: { need_captcha_verification: options.captcha ?? false } });
    }
    if (href.includes("/oneid/public/key?")) {
      calls.push("key");
      return json({ data: { rsa: { publicKey: publicPem } } });
    }
    if (href.endsWith("/oneid/login")) {
      calls.push("login");
      const body = JSON.parse(request.body);
      assert.equal(body.account, config.username);
      assert.match(body.password, /^[0-9a-f]{512}$/);
      return options.loginStatus
        ? json({ error: "invalid_credentials" }, options.loginStatus)
        : json({ data: { token: "fixture-sso" } });
    }
    if (href.startsWith(`${config.userCenterUrl}/oneid/oidc/auth?`)) {
      calls.push("authorize");
      const state = new URL(href).searchParams.get("state");
      return json({ data: { body: `${config.redirectUri}?code=fixture-code&state=${state}` } });
    }
    if (href === `${config.lightragUrl}/api/v1/rag/retrieve`) {
      calls.push("query");
      queries.push(JSON.parse(request.body));
      const token = new Headers(request.headers).get("authorization");
      if (options.queryUnauthorized && (options.queryUnauthorized === "always" || queries.length === 1)) {
        return json({ message: "Unauthorized" }, 401);
      }
      assert.equal(token, `Bearer ${renewedToken.accessToken}`);
      return json(answer);
    }
    throw new Error(`Unexpected fixture request: ${href}`);
  };
  const auth = new OneIdClient(config, { now: () => now, tokenStore, fetch, stateFactory: () => "fixture-state" });
  const kb = new LightRagClient({ ...config, requestTimeoutMs: options.timeoutMs ?? 1000 }, auth, fetch);
  return { auth, kb, calls, queries, snapshot: () => structuredClone(stored), clearCount: () => clears };
}

for (const [label, status, body] of [
  ["OneID Bad Request envelope", 400, { error: "Bad Request", message: "token invalid or expired", status: 400 }],
  ["OneID object error envelope", 400, { error: {}, message: "token invalid or expired", status: 400 }],
  ["case and whitespace", 400, { message: " Token Invalid Or Expired " }],
  ["OAuth invalid grant", 400, { error: "invalid_grant" }],
  ["OAuth invalid token", 400, { error: "invalid_token" }],
  ["OAuth invalid refresh token", 400, { error: "invalid_refresh_token" }],
  ["unauthorized refresh token", 401, { error: "invalid_token" }],
  ["unauthorized refresh without an explicit code", 401, {}]
]) {
  test(`automatically resumes the original query after ${label}`, async () => {
    const f = fixture({ refreshStatus: status, refreshBody: body });
    assert.deepEqual(await f.kb.query(question), answer);
    assert.deepEqual(f.calls, ["refresh", "captcha", "key", "login", "authorize", "exchange", "query"]);
    assert.equal(f.queries[0].query, question.query);
    assert.equal(f.clearCount(), 1);
    assert.deepEqual(f.snapshot(), renewedToken);
    assert.equal(await f.auth.getAccessToken(), renewedToken.accessToken);
    assert.equal(f.calls.length, 7, "A renewed session must be reused without another login");
  });
}

for (const [label, status, body] of [
  ["service error", 500, { message: "token invalid or expired" }],
  ["invalid client", 400, { error: "invalid_client" }],
  ["unauthorized client", 401, { error: "invalid_client" }],
  ["explicit OAuth error takes precedence", 400, { error: "invalid_client", message: "token invalid or expired" }],
  ["explicit alternate error code takes precedence", 400, { error_code: "invalid_scope", message: "token invalid or expired" }],
  ["invalid scope", 400, { error: "invalid_scope" }],
  ["unknown bad request", 400, { message: "Missing a required parameter" }]
]) {
  test(`does not attempt password login after ${label}`, async () => {
    const f = fixture({ refreshStatus: status, refreshBody: body });
    await assert.rejects(f.kb.query(question), error => error.status === status && error.operation === "OAuth token refresh");
    assert.deepEqual(f.calls, ["refresh"]);
    assert.equal(f.clearCount(), 0);
    assert.deepEqual(f.snapshot(), expiredToken);
  });
}

test("refreshes a near-expiry token before the original query without password login", async () => {
  const f = fixture({
    initial: { ...expiredToken, expiresAt: now + 179_000 },
    onRefresh: async () => json({ access_token: renewedToken.accessToken, expires_in: 3600 })
  });
  assert.deepEqual(await f.kb.query(question), answer);
  assert.deepEqual(f.calls, ["refresh", "query"]);
  assert.deepEqual(f.snapshot(), { ...renewedToken, refreshToken: expiredToken.refreshToken });
});

test("a token outside the refresh margin needs no authentication request", async () => {
  const f = fixture({ initial: renewedToken });
  assert.deepEqual(await f.kb.query(question), answer);
  assert.deepEqual(f.calls, ["query"]);
});

test("concurrent queries share one refresh rejection and one saved-account login", async () => {
  const f = fixture();
  assert.deepEqual(await Promise.all([f.kb.query(question), f.kb.query(question)]), [answer, answer]);
  assert.deepEqual(f.calls, ["refresh", "captcha", "key", "login", "authorize", "exchange", "query", "query"]);
});

test("an upstream 401 renews authentication and retries the same query once", async () => {
  const f = fixture({ initial: { ...expiredToken, expiresAt: now + 3_600_000 }, queryUnauthorized: true });
  assert.deepEqual(await f.kb.query(question), answer);
  assert.deepEqual(f.calls, ["query", "refresh", "captcha", "key", "login", "authorize", "exchange", "query"]);
  assert.deepEqual(f.queries[0], f.queries[1]);
});

test("a second upstream 401 stops without another refresh or password login", async () => {
  const f = fixture({ initial: { ...expiredToken, expiresAt: now + 3_600_000 }, queryUnauthorized: "always" });
  await assert.rejects(f.kb.query(question), error => error.status === 401);
  assert.equal(f.calls.filter(call => call === "query").length, 2);
  assert.equal(f.calls.filter(call => call === "refresh").length, 1);
  assert.equal(f.calls.filter(call => call === "login").length, 1);
});

test("a CAPTCHA challenge stops recovery before password login", async () => {
  const f = fixture({ captcha: true });
  await assert.rejects(f.kb.query(question), error => error.code === "KB_INTERACTION_REQUIRED");
  assert.deepEqual(f.calls, ["refresh", "captcha"]);
  assert.equal(f.snapshot(), undefined);
});

test("rejected saved credentials stop recovery after one password login", async () => {
  const f = fixture({ loginStatus: 401 });
  await assert.rejects(f.kb.query(question), error => error.status === 401 && error.operation === "OneID login");
  assert.deepEqual(f.calls, ["refresh", "captcha", "key", "login"]);
  assert.equal(f.snapshot(), undefined);
});

test("a network failure preserves the session and does not try password login", async () => {
  const failure = new TypeError("fetch failed", { cause: { code: "ECONNRESET" } });
  const f = fixture({ onRefresh: async () => { throw failure; } });
  await assert.rejects(f.kb.query(question), error => error === failure);
  assert.deepEqual(f.calls, ["refresh"]);
  assert.deepEqual(f.snapshot(), expiredToken);
});

test("cancelling one concurrent query leaves the other query's shared recovery running", async () => {
  const entered = deferred(), resume = deferred();
  let sharedSignal;
  const f = fixture({ onCaptcha: async ({ signal }) => {
    sharedSignal = signal;
    entered.resolve();
    await resume.promise;
  } });
  const controller = new AbortController();
  const first = f.kb.query(question, { signal: controller.signal });
  const cancelled = assert.rejects(first, error => error.code === "KB_CANCELLED");
  const second = f.kb.query(question);
  await entered.promise;
  controller.abort();
  await cancelled;
  assert.equal(sharedSignal.aborted, false);
  resume.resolve();
  assert.deepEqual(await second, answer);
  assert.equal(f.calls.filter(call => call === "login").length, 1);
  assert.equal(f.queries.length, 1);
});

for (const mode of ["cancellation", "deadline"]) {
  test(`${mode} aborts the last waiter's recovery without sending the query`, async () => {
    const entered = deferred(), stopped = deferred();
    const f = fixture({
      timeoutMs: mode === "deadline" ? 50 : 1000,
      onCaptcha: async ({ signal }) => {
        entered.resolve();
        await new Promise((resolve, reject) => signal.addEventListener("abort", () => {
          stopped.resolve();
          reject(signal.reason);
        }, { once: true }));
      }
    });
    const controller = new AbortController();
    const pending = f.kb.query(question, { signal: controller.signal });
    const rejected = assert.rejects(pending, error => error.code === (mode === "deadline" ? "KB_TIMEOUT" : "KB_CANCELLED"));
    await entered.promise;
    if (mode === "cancellation") controller.abort();
    await rejected;
    await stopped.promise;
    assert.deepEqual(f.calls, ["refresh", "captcha"]);
    assert.equal(f.snapshot(), undefined);
  });
}

test("the same message during a non-refresh authentication step is not an expired refresh token", async () => {
  const client = new OneIdClient(config, {
    tokenStore: {},
    async fetch() { return json({ message: "token invalid or expired" }, 400); }
  });
  await assert.rejects(client.authenticate(), error => error.operation === "CAPTCHA check"
    && error.upstreamCode !== "invalid_token");
});
