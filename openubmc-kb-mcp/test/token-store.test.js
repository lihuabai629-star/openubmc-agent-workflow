import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createTokenOwner, FileTokenStore } from "../src/auth/token-store.js";

const ownerConfig = {
  userCenterUrl: "https://usercenter.example.com",
  clientId: "client",
  username: "user"
};

test("persists tokens only for the matching account and protects the cache on POSIX", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openubmc-token-store-"));
  const path = join(dir, "nested", "token-cache.json");
  const owner = createTokenOwner(ownerConfig);
  const store = new FileTokenStore(path, owner);
  const token = { accessToken: "access", refreshToken: "refresh", expiresAt: 12345 };

  assert.equal(await store.save(token), true);
  assert.deepEqual(await store.load(), token);
  assert.equal(await new FileTokenStore(path, `${owner}-other`).load(), undefined);
  if (process.platform !== "win32") assert.equal((await stat(path)).mode & 0o777, 0o600);

  await store.clear();
  assert.equal(await store.load(), undefined);
});

test("binds token ownership to the installed knowledge-base version", () => {
  assert.notEqual(
    createTokenOwner({ ...ownerConfig, knowledgeMcpVersion: "1.3.0" }),
    createTokenOwner({ ...ownerConfig, knowledgeMcpVersion: "1.4.0" })
  );
});
