import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { isSecretOperationRequest, SecretOpsRunner } from "../packages/core/src/secret-ops.ts";
import { SecretTargetCatalog } from "../packages/core/src/secret-targets.ts";
import { ServerCatalog } from "../packages/core/src/server-catalog.ts";

test("secret runner claims only its explicit capability and operations", () => {
  assert.equal(isSecretOperationRequest({ capability: "server.secrets", operation: "inventory_env" }), true);
  assert.equal(isSecretOperationRequest({ capability: "server.secrets", operation: "patch_env" }), true);
  assert.equal(isSecretOperationRequest({ capability: "server.secrets", operation: "reveal_env" }), false);
  assert.equal(isSecretOperationRequest({ capability: "server.files", operation: "patch_env" }), false);
});

test("empty secret target catalog is a valid migration-safe idle configuration", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-secret-empty-"));
  const local = join(root, "local");
  const secrets = join(local, "secrets");
  await mkdir(secrets, { recursive: true });
  const serversFile = join(local, "servers.yaml");
  const targetsFile = join(local, "env-secrets.yaml");
  await writeFile(serversFile, [
    "servers:",
    "  idle-server:",
    "    host: 127.0.0.1",
    "    username: operator",
    "    managed_root: /srv/idle",
    "    auth:",
    "      type: private_key",
    "      private_key: id_ed25519"
  ].join("\n") + "\n");
  await writeFile(targetsFile, "schema_version: 1\ntargets: {}\n");
  const servers = new ServerCatalog(root, serversFile, secrets);
  const targets = new SecretTargetCatalog(root, servers, targetsFile);
  await targets.initialize();
  assert.deepEqual(targets.list(), []);

  const runner = new SecretOpsRunner(root, { servers, targets, stagingDir: join(root, "staging") });
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), {});
});
