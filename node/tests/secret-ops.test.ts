import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  INVENTORY_SCRIPT,
  PATCH_SCRIPT,
  maskSecret,
  parseSecretInventory,
  rawSha256,
  validateSecretStage,
  type SecretStage
} from "../packages/core/src/secret-env.ts";
import { SecretTargetCatalog, type ManagedSecretTarget } from "../packages/core/src/secret-targets.ts";
import { ServerCatalog } from "../packages/core/src/server-catalog.ts";

function targetFixture(): ManagedSecretTarget {
  return {
    alias: "relay-zhipu",
    serverAlias: "relay-prod",
    envFile: "/srv/new-api/.env.secrets",
    seats: new Map([
      ["zhipu-a", { id: "zhipu-a", envName: "ZHIPU_SEAT_A_API_KEY", label: "A" }],
      ["zhipu-b", { id: "zhipu-b", envName: "ZHIPU_SEAT_B_API_KEY", label: "B" }],
      ["zhipu-c", { id: "zhipu-c", envName: "ZHIPU_SEAT_C_API_KEY", label: "C" }],
      ["zhipu-d", { id: "zhipu-d", envName: "ZHIPU_SEAT_D_API_KEY", label: "D" }]
    ]),
    reload: { type: "none" }
  };
}

function seatsPayload(target: ManagedSecretTarget): string {
  return JSON.stringify([...target.seats.values()].map((seat) => ({ seat_id: seat.id, env_name: seat.envName })));
}

async function writePython(root: string, name: string, content: string): Promise<string> {
  const path = join(root, name);
  await writeFile(path, content, { mode: 0o700 });
  await chmod(path, 0o700);
  return path;
}

test("maskSecret exposes only bounded identifying fragments", () => {
  assert.equal(maskSecret(""), "");
  assert.equal(maskSecret("ab"), "••");
  assert.equal(maskSecret("abcdefgh"), "a••••••h");
  assert.equal(maskSecret("sk-1234567890-END"), "sk-1••••-END");
});

test("SecretTargetCatalog accepts stable seat IDs and rejects paths outside managed_root", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-secret-catalog-"));
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
    "      private_key: id_ed25519",
    "    allowed_containers: [new-api]"
  ].join("\n") + "\n");
  await writeFile(targetsFile, [
    "schema_version: 1",
    "targets:",
    "  relay-zhipu:",
    "    server: relay-prod",
    "    env_file: /srv/new-api/.env.secrets",
    "    seats:",
    "      zhipu-a: ZHIPU_SEAT_A_API_KEY",
    "      zhipu-c:",
    "        env: ZHIPU_SEAT_C_API_KEY",
    "        label: stable C",
    "    reload:",
    "      type: compose",
    "      project: new-api",
    "      health_container: new-api"
  ].join("\n") + "\n");
  const servers = new ServerCatalog(root, serversFile, join(root, "local", "secrets"));
  const catalog = new SecretTargetCatalog(root, servers, targetsFile);
  await catalog.initialize();
  const target = catalog.get("relay-zhipu");
  assert.deepEqual([...target.seats.keys()], ["zhipu-a", "zhipu-c"]);
  assert.equal(target.reload.type, "compose");

  await writeFile(targetsFile, (await readFile(targetsFile, "utf8")).replace("/srv/new-api/.env.secrets", "/etc/shadow"));
  await assert.rejects(() => catalog.initialize(), /managed_root/);
});

test("stage validation requires an explicit keep/delete/set decision for every stable seat", () => {
  const target = targetFixture();
  const stage: SecretStage = {
    schema_version: 1,
    env_target: target.alias,
    expected_inventory_hash: "a".repeat(64),
    created_at: new Date().toISOString(),
    mutations: [...target.seats.keys()].map((seatId) => ({
      seat_id: seatId,
      action: "keep" as const,
      expected_fingerprint: "b".repeat(64)
    }))
  };
  assert.equal(validateSecretStage(stage, target).mutations.length, 4);
  assert.throws(() => validateSecretStage({ ...stage, mutations: stage.mutations.slice(1) }, target), /全部席位/);
  assert.throws(() => validateSecretStage({
    ...stage,
    mutations: stage.mutations.map((row, index) => index === 0 ? { ...row, value: "must-not-exist" } : row)
  }, target), /不得包含 value/);
});

test("remote scripts inventory, atomically patch selected seats, and never print plaintext secrets", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-secret-script-"));
  const envFile = join(root, ".env.secrets");
  const inventoryPath = await writePython(root, "inventory.py", INVENTORY_SCRIPT);
  const patchPath = await writePython(root, "patch.py", PATCH_SCRIPT);
  const target = targetFixture();
  const old = {
    a: "alpha-very-secret-A1",
    b: "bravo-very-secret-B2",
    c: "charlie-very-secret-C3",
    d: "delta-very-secret-D4"
  };
  const replacement = "charlie-replaced-secret-C9";
  await writeFile(envFile, [
    "LOG_LEVEL=info",
    `ZHIPU_SEAT_A_API_KEY=${old.a}`,
    `ZHIPU_SEAT_B_API_KEY=${old.b}`,
    `ZHIPU_SEAT_C_API_KEY=${old.c}`,
    `ZHIPU_SEAT_D_API_KEY=${old.d}`,
    "UNRELATED_TOKEN=leave-this-alone"
  ].join("\n") + "\n", { mode: 0o600 });

  const seats = seatsPayload(target);
  const inventoryRun = spawnSync("python3", [inventoryPath, envFile, seats], { encoding: "utf8" });
  assert.equal(inventoryRun.status, 0, inventoryRun.stderr);
  for (const value of Object.values(old)) assert.equal(inventoryRun.stdout.includes(value), false);
  const inventory = parseSecretInventory(inventoryRun.stdout, target);
  assert.equal(inventory.seats.length, 4);
  assert.equal(inventory.seats.find((row) => row.seat_id === "zhipu-b")?.masked, "brav••••t-B2");

  const byId = new Map(inventory.seats.map((row) => [row.seat_id, row]));
  const stage: SecretStage = {
    schema_version: 1,
    env_target: target.alias,
    expected_inventory_hash: inventory.inventory_hash,
    created_at: new Date().toISOString(),
    mutations: [
      { seat_id: "zhipu-a", action: "keep", expected_fingerprint: byId.get("zhipu-a")!.fingerprint_sha256 },
      { seat_id: "zhipu-b", action: "delete", expected_fingerprint: byId.get("zhipu-b")!.fingerprint_sha256 },
      { seat_id: "zhipu-c", action: "set", expected_fingerprint: byId.get("zhipu-c")!.fingerprint_sha256, value: replacement },
      { seat_id: "zhipu-d", action: "keep", expected_fingerprint: byId.get("zhipu-d")!.fingerprint_sha256 }
    ]
  };
  const stagePath = join(root, "stage.json");
  const backupPath = join(root, "backups", "operation.env");
  await mkdir(join(root, "backups"));
  await writeFile(stagePath, `${JSON.stringify(stage)}\n`, { mode: 0o600 });
  const patchRun = spawnSync("python3", [patchPath, envFile, seats, stagePath, backupPath], { encoding: "utf8" });
  assert.equal(patchRun.status, 0, patchRun.stderr);
  for (const value of [...Object.values(old), replacement]) assert.equal(patchRun.stdout.includes(value), false);
  const updated = await readFile(envFile, "utf8");
  assert.match(updated, new RegExp(`ZHIPU_SEAT_A_API_KEY=${old.a}`));
  assert.doesNotMatch(updated, /ZHIPU_SEAT_B_API_KEY=/);
  assert.match(updated, new RegExp(`ZHIPU_SEAT_C_API_KEY=${replacement}`));
  assert.match(updated, new RegExp(`ZHIPU_SEAT_D_API_KEY=${old.d}`));
  assert.match(updated, /UNRELATED_TOKEN=leave-this-alone/);
  assert.equal((await readFile(backupPath, "utf8")).includes(old.b), true);

  await t.test("stale managed-seat inventory is rejected before another mutation", async () => {
    const freshInventoryRun = spawnSync("python3", [inventoryPath, envFile, seats], { encoding: "utf8" });
    const fresh = parseSecretInventory(freshInventoryRun.stdout, target);
    const staleStage: SecretStage = {
      schema_version: 1,
      env_target: target.alias,
      expected_inventory_hash: fresh.inventory_hash,
      created_at: new Date().toISOString(),
      mutations: fresh.seats.map((row) => ({ seat_id: row.seat_id, action: "keep", expected_fingerprint: row.fingerprint_sha256 }))
    };
    const stalePath = join(root, "stale.json");
    await writeFile(stalePath, JSON.stringify(staleStage), { mode: 0o600 });
    const beforeOutOfBandChange = await readFile(envFile, "utf8");
    await writeFile(
      envFile,
      beforeOutOfBandChange.replace(
        `ZHIPU_SEAT_A_API_KEY=${old.a}`,
        "ZHIPU_SEAT_A_API_KEY=out-of-band-managed-seat-change"
      )
    );
    const staleRun = spawnSync("python3", [patchPath, envFile, seats, stalePath, join(root, "backups", "stale.env")], { encoding: "utf8" });
    assert.notEqual(staleRun.status, 0);
    assert.match(staleRun.stderr, /inventory changed since owner review/);
  });

  assert.equal(rawSha256(await readFile(stagePath, "utf8")).length, 64);
});
