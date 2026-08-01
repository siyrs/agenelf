import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SecretTargetCatalog } from "../packages/core/src/secret-targets.ts";
import { ServerCatalog } from "../packages/core/src/server-catalog.ts";

test("SecretTargetCatalog exposes Chinese labels and aliases for deterministic chat routing", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-secret-target-label-"));
  await mkdir(join(root, "local", "secrets"), { recursive: true });
  const serversFile = join(root, "local", "servers.yaml");
  const targetsFile = join(root, "local", "env-secrets.yaml");
  await writeFile(serversFile, [
    "servers:",
    "  relay-prod:",
    "    host: 127.0.0.1",
    "    username: operator",
    "    managed_root: /srv/new-api",
    "    auth:",
    "      type: private_key",
    "      private_key: id_ed25519"
  ].join("\n") + "\n");
  await writeFile(targetsFile, [
    "schema_version: 1",
    "targets:",
    "  relay-zhipu:",
    "    label: 中天中转站",
    "    aliases: [中天, 中天中转, 智谱中转站]",
    "    server: relay-prod",
    "    env_file: /srv/new-api/.env.secrets",
    "    seats:",
    "      zhipu-a:",
    "        env: ZHIPU_SEAT_A_API_KEY",
    "        label: 智谱席位 A"
  ].join("\n") + "\n");

  const servers = new ServerCatalog(root, serversFile, join(root, "local", "secrets"));
  const catalog = new SecretTargetCatalog(root, servers, targetsFile);
  await catalog.initialize();
  const target = catalog.get("relay-zhipu");
  assert.equal(target.label, "中天中转站");
  assert.deepEqual(target.aliases, ["中天", "中天中转", "智谱中转站"]);
  assert.deepEqual(catalog.list()[0].aliases, ["中天", "中天中转", "智谱中转站"]);
});

test("target aliases reject control characters and excessive list sizes", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-secret-target-label-invalid-"));
  await mkdir(join(root, "local", "secrets"), { recursive: true });
  const serversFile = join(root, "local", "servers.yaml");
  const targetsFile = join(root, "local", "env-secrets.yaml");
  await writeFile(serversFile, [
    "servers:",
    "  relay-prod:",
    "    host: 127.0.0.1",
    "    username: operator",
    "    managed_root: /srv/new-api",
    "    auth:",
    "      type: private_key",
    "      private_key: id_ed25519"
  ].join("\n") + "\n");
  await writeFile(targetsFile, [
    "schema_version: 1",
    "targets:",
    "  relay-zhipu:",
    "    label: 中天中转站",
    `    aliases: [${Array.from({ length: 17 }, (_, index) => `别名${index}`).join(", ")}]`,
    "    server: relay-prod",
    "    env_file: /srv/new-api/.env.secrets",
    "    seats:",
    "      zhipu-a: ZHIPU_SEAT_A_API_KEY"
  ].join("\n") + "\n");
  const servers = new ServerCatalog(root, serversFile, join(root, "local", "secrets"));
  const catalog = new SecretTargetCatalog(root, servers, targetsFile);
  await assert.rejects(() => catalog.initialize(), /最多 16 项/);
});
