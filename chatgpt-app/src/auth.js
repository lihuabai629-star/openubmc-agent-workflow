import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { InvalidGrantError, InvalidRequestError, InvalidScopeError, InvalidTokenError } from "@modelcontextprotocol/sdk/server/auth/errors.js";

const secret = () => randomBytes(32).toString("base64url");
const digest = value => createHash("sha256").update(value).digest();
const hash = value => digest(value).toString("hex");
const escape = value => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

export async function loginSecret(directory) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const file = path.join(directory, "chatgpt-login-key");
  try { await writeFile(file, secret(), { flag: "wx", mode: 0o600 }); }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  const value = (await readFile(file, "utf8")).trim();
  if (value.length < 32) throw new Error("The local ChatGPT login key is invalid.");
  return { file, value };
}

// Dedicated single-user connector. Credentials and OAuth tokens never enter model context.
// OAuth grants intentionally expire on process restart; reconnecting requires a fresh login.
export class LocalOAuthProvider {
  constructor(origin, key) {
    this.origin = new URL(origin).origin;
    this.resource = new URL("/mcp", this.origin);
    this.keyHash = digest(key);
    this.clients = new Map();
    this.pending = new Map();
    this.codes = new Map();
    this.tokens = new Map();
    this.refresh = new Map();
    this.failures = new Map();
    this.clientsStore = {
      getClient: async id => this.clients.get(id),
      registerClient: async client => {
        this.prune();
        for (const uri of client.redirect_uris) {
          const url = new URL(uri);
          const local = this.origin.startsWith("http://") && url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
          if (!local && !["https://chatgpt.com", "https://chat.openai.com"].includes(url.origin)) {
            throw new InvalidRequestError("Only ChatGPT redirect origins are accepted.");
          }
        }
        if (this.clients.size >= 128) throw new InvalidRequestError("Client limit reached; restart the connector.");
        this.clients.set(client.client_id, client);
        return client;
      }
    };
  }

  prune() {
    for (const map of [this.pending, this.codes, this.tokens, this.refresh, this.failures]) {
      for (const [key, value] of map) if (value.expiresAt <= Date.now()) map.delete(key);
      if (map.size >= 1024) throw new InvalidRequestError("Connector capacity reached.");
    }
  }

  async authorize(client, params, res) {
    this.prune();
    if ((params.scopes ?? []).some(scope => scope !== "openubmc")) throw new InvalidScopeError("Unsupported scope.");
    if (params.resource && params.resource.href !== this.resource.href) throw new InvalidRequestError("Wrong resource.");
    const request = secret();
    this.pending.set(request, { client, params, expiresAt: Date.now() + 300_000 });
    res.redirect(`/login?request=${request}`);
  }

  loginPage(request) {
    this.prune();
    const pending = this.pending.get(request);
    if (!pending) return null;
    return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>连接 openUBMC</title><style>body{font:16px system-ui;color:#243247;max-width:420px;margin:12vh auto;padding:24px}input,button{box-sizing:border-box;width:100%;padding:12px;margin:12px 0;font:inherit}button{background:#1f609f;color:white;border:0;border-radius:6px}p{line-height:1.7;color:#637386}</style><h1>连接 openUBMC</h1><p>${escape(pending.client.client_name || "ChatGPT")} 将能查询知识库、读取设备信息并调用工作流。请输入连接器主机上的登录密钥。</p><form method="post" action="/login"><input type="hidden" name="request" value="${escape(request)}"><label for="key">登录密钥</label><input id="key" name="key" type="password" required autocomplete="current-password" autofocus><button>允许连接</button></form></html>`;
  }

  approve(request, key, address) {
    this.prune();
    const failures = this.failures.get(address) ?? { count: 0, expiresAt: Date.now() + 900_000 };
    if (failures.count >= 5) return { status: 429 };
    const pending = this.pending.get(request);
    if (!pending) return { status: 400 };
    if (!timingSafeEqual(digest(String(key ?? "")), this.keyHash)) {
      failures.count++;
      this.failures.set(address, failures);
      return { status: 403 };
    }
    this.pending.delete(request);
    const code = secret();
    this.codes.set(hash(code), { ...pending, expiresAt: Date.now() + 60_000 });
    const url = new URL(pending.params.redirectUri);
    url.searchParams.set("code", code);
    if (pending.params.state !== undefined) url.searchParams.set("state", pending.params.state);
    return { status: 302, redirect: url.href };
  }

  codeFor(client, code) {
    this.prune();
    const record = this.codes.get(hash(code));
    if (!record || record.client.client_id !== client.client_id) throw new InvalidGrantError("Invalid authorization code.");
    return record;
  }

  async challengeForAuthorizationCode(client, code) {
    return this.codeFor(client, code).params.codeChallenge;
  }

  issue(clientId) {
    this.prune();
    const access = secret(), refresh = secret();
    const info = { clientId, scopes: ["openubmc"], resource: this.resource, expiresAt: Date.now() + 3_600_000 };
    this.tokens.set(hash(access), info);
    this.refresh.set(hash(refresh), { ...info, expiresAt: Date.now() + 86_400_000 });
    return { access_token: access, refresh_token: refresh, token_type: "Bearer", expires_in: 3600, scope: "openubmc" };
  }

  async exchangeAuthorizationCode(client, code, _verifier, redirectUri, resource) {
    const record = this.codeFor(client, code);
    if (redirectUri !== record.params.redirectUri || (resource && resource.href !== this.resource.href)) {
      throw new InvalidGrantError("Authorization binding mismatch.");
    }
    this.codes.delete(hash(code));
    return this.issue(client.client_id);
  }

  async exchangeRefreshToken(client, token, scopes, resource) {
    this.prune();
    const record = this.refresh.get(hash(token));
    if (!record || record.clientId !== client.client_id || scopes?.some(scope => scope !== "openubmc") ||
      (resource && resource.href !== this.resource.href)) throw new InvalidGrantError("Invalid refresh token.");
    this.refresh.delete(hash(token));
    return this.issue(client.client_id);
  }

  async verifyAccessToken(token) {
    this.prune();
    const record = this.tokens.get(hash(token));
    if (!record) throw new InvalidTokenError("Invalid or expired token.");
    return { ...record, token, expiresAt: Math.floor(record.expiresAt / 1000) };
  }

  async revokeToken(client, request) {
    for (const map of [this.tokens, this.refresh]) {
      const record = map.get(hash(request.token));
      if (record?.clientId === client.client_id) map.delete(hash(request.token));
    }
  }
}
