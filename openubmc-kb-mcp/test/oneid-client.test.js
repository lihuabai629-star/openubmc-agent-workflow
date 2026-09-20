import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";

import { CaptchaRequiredError, OneIdClient } from "../src/auth/oneid-client.js";

function json(value, init = {}) {
  return new Response(JSON.stringify(value), {
    status: init.status || 200,
    headers: { "content-type": "application/json", ...(init.headers || {}) }
  });
}

function memoryStore(initial) {
  let value = initial ? structuredClone(initial) : undefined;
  return {
    load: async () => value ? structuredClone(value) : undefined,
    save: async token => {
      value = token ? structuredClone(token) : undefined;
      return true;
    },
    clear: async () => {
      value = undefined;
      return true;
    },
    snapshot: () => value ? structuredClone(value) : undefined
  };
}

const config = {
  userCenterUrl: "https://usercenter.openubmc.cn",
  authorizationEndpoint: "https://omapi.openubmc.cn/oneid/oidc/authorize",
  tokenEndpoint: "https://omapi.openubmc.cn/oneid/oidc/token",
  clientId: "client-id",
  clientSecret: "client-secret",
  redirectUri: "openubmc://openubmc.openubmc-auth/callback",
  scopes: ["openid", "offline_access"],
  username: "alice",
  password: "correct horse battery staple"
};

test("refuses authentication before login when CAPTCHA is required", async () => {
  const calls = [];
  const client = new OneIdClient(config, {
    tokenStore: memoryStore(),
    fetch: async url => {
      calls.push(String(url));
      return json({ data: { need_captcha_verification: true } });
    }
  });

  await assert.rejects(() => client.getAccessToken(), CaptchaRequiredError);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /checkLogin/);
});

test("uses a persisted access token while it is still valid", async () => {
  const now = 1_700_000_000_000;
  const client = new OneIdClient(config, {
    now: () => now,
    tokenStore: memoryStore({ accessToken: "cached-token", refreshToken: "refresh-token", expiresAt: now + 600_000 }),
    fetch: async () => { throw new Error("network should not be used"); }
  });

  assert.equal(await client.getAccessToken(), "cached-token");
});

test("logs in once, persists the token, and exchanges the authorization code", async () => {
  const { publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const publicPem = publicKey.export({ type: "spki", format: "pem" });
  const calls = [];
  const store = memoryStore();
  const now = 1_700_000_000_000;
  let encryptedPassword;

  const fetch = async (url, options = {}) => {
    const href = String(url);
    calls.push({ href, options });
    if (href.endsWith("/oneid/captcha/checkLogin")) return json({ data: { need_captcha_verification: false } });
    if (href.includes("/oneid/public/key")) return json({ data: { rsa: { publicKey: publicPem } } });
    if (href.endsWith("/oneid/login")) {
      const loginBody = JSON.parse(options.body);
      encryptedPassword = loginBody.password;
      assert.equal(loginBody.redirect_uri, undefined);
      assert.equal(options.headers.get("origin"), config.userCenterUrl);
      assert.match(options.headers.get("referer"), /\/login\?/);
      return json({ data: { username: "alice" } }, {
        headers: { "set-cookie": "waf=session; Domain=.openubmc.cn; Path=/; Secure" }
      });
    }
    if (href.startsWith(`${config.userCenterUrl}/oneid/oidc/auth`)) {
      assert.match(options.headers.get("cookie"), /waf=session/);
      assert.equal(options.headers.get("token"), null);
      return json({ data: { body: `${config.redirectUri}?code=auth-code&state=fixed-state` } });
    }
    if (href === config.tokenEndpoint) {
      assert.match(options.body, /grant_type=authorization_code/);
      assert.match(options.body, /code=auth-code/);
      return json({ access_token: "access-token", refresh_token: "refresh-token", expires_in: 3600 });
    }
    throw new Error(`Unexpected request: ${href}`);
  };

  const client = new OneIdClient(config, {
    fetch,
    now: () => now,
    stateFactory: () => "fixed-state",
    tokenStore: store
  });
  assert.equal(await client.getAccessToken(), "access-token");
  assert.equal(await client.getAccessToken(), "access-token");
  assert.equal(calls.filter(call => call.href === config.tokenEndpoint).length, 1);
  assert.deepEqual(store.snapshot(), {
    accessToken: "access-token",
    refreshToken: "refresh-token",
    expiresAt: now + 3_600_000
  });
  assert.match(encryptedPassword, /^[0-9a-f]+$/);
  assert.equal(encryptedPassword.length, 512);
});

test("refreshes an expired persisted token without repeating the OneID password login", async () => {
  const now = 1_700_000_000_000;
  const store = memoryStore({ accessToken: "expired", refreshToken: "refresh-1", expiresAt: now - 1 });
  const calls = [];
  const client = new OneIdClient(config, {
    now: () => now,
    tokenStore: store,
    fetch: async (url, options = {}) => {
      calls.push(String(url));
      assert.equal(String(url), config.tokenEndpoint);
      const body = new URLSearchParams(options.body);
      assert.equal(body.get("grant_type"), "refresh_token");
      assert.equal(body.get("refresh_token"), "refresh-1");
      return json({ access_token: "access-2", expires_in: 7200 });
    }
  });

  assert.equal(await client.getAccessToken(), "access-2");
  assert.deepEqual(calls, [config.tokenEndpoint]);
  assert.deepEqual(store.snapshot(), {
    accessToken: "access-2",
    refreshToken: "refresh-1",
    expiresAt: now + 7_200_000
  });
});

test("coalesces concurrent token requests into one refresh", async () => {
  const now = 1_700_000_000_000;
  let requests = 0;
  const client = new OneIdClient(config, {
    now: () => now,
    tokenStore: memoryStore({ refreshToken: "refresh-token", expiresAt: 0 }),
    fetch: async () => {
      requests += 1;
      await new Promise(resolve => setImmediate(resolve));
      return json({ access_token: "shared-token", refresh_token: "refresh-token", expires_in: 3600 });
    }
  });

  assert.deepEqual(await Promise.all([client.getAccessToken(), client.getAccessToken()]), ["shared-token", "shared-token"]);
  assert.equal(requests, 1);
});

test("stops on an invalid refresh token and requires local re-login", async () => {
  const now = 1_700_000_000_000;
  const store = memoryStore({ accessToken: "expired", refreshToken: "invalid-refresh", expiresAt: now - 1 });
  const calls = [];
  const client = new OneIdClient(config, {
    now: () => now,
    tokenStore: store,
    fetch: async (url) => {
      calls.push(String(url));
      return json({ error: "invalid_grant" }, { status: 400 });
    }
  });

  await assert.rejects(
    () => client.getAccessToken(),
    error => error.code === "KB_RELOGIN_REQUIRED"
  );
  assert.deepEqual(calls, [config.tokenEndpoint]);
  assert.equal(store.snapshot(), undefined);
});
