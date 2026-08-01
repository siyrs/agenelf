import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { OwnerChatSecretController, type SecretChatTransport } from "../packages/core/src/chat-secret-env.ts";
import { fingerprintSecret, maskSecret, rawSha256, type SecretStage } from "../packages/core/src/secret-env.ts";
import { SecretTargetCatalog } from "../packages/core/src/secret-targets.ts";
import { ServerCatalog, type ManagedServer } from "../packages/core/src/server-catalog.ts";
import type { RemoteCommandResult } from "../packages/core/src/open-ssh.ts";

function success(command: string, stdout = ""): RemoteCommandResult {
  return { command, exit_code: 0, stdout, stderr: "" };
}

class FakeSecretTransport implements SecretChatTransport {
  readonly values = new Map<string, string>([
    ["ZHIPU_SEAT_A_API_KEY", "alpha-plaintext-A1"],
    ["ZHIPU_SEAT_B_API_KEY", "bravo-plaintext-B2"],
    ["ZHIPU_SEAT_C_API_KEY", "charlie-plaintext-C3"],
    ["ZHIPU_SEAT_D_API_KEY", "delta-plaintext-D4"]
  ]);
  readonly commands: string[] = [];
  readonly writes: Array<{ path: string; content: string }> = [];
  stage: SecretStage | null = null;
  backupCreated = false;

  async writeText(_server: ManagedServer, path: string, content: string): Promise<RemoteCommandResult> {
    this.writes.push({ path, content });
    if (path.endsWith("/stage.json")) this.stage = JSON.parse(content) as SecretStage;
    return success(`write:${path}`);
  }

  async run(_server: ManagedServer, command: string): Promise<RemoteCommandResult> {
    this.commands.push(command);
    if (command.includes("inventory.py")) {
      const seats = [
        ["zhipu-a", "ZHIPU_SEAT_A_API_KEY"],
        ["zhipu-b", "ZHIPU_SEAT_B_API_KEY"],
        ["zhipu-c", "ZHIPU_SEAT_C_API_KEY"],
        ["zhipu-d", "ZHIPU_SEAT_D_API_KEY"]
      ] as const;
      const rows = seats.map(([seatId, envName]) => {
        const value = this.values.get(envName);
        const full = value ? fingerprintSecret(value) : "";
        return {
          seat_id: seatId,
          env_name: envName,
          present: value !== undefined,
          masked: value ? maskSecret(value) : "",
          fingerprint: full ? full.slice(0, 12).toUpperCase() : "",
          fingerprint_sha256: full
        };
      });
      const digest = rows.map((row) => ({
        seat_id: row.seat_id,
        env_name: row.env_name,
        present: row.present,
        fingerprint_sha256: row.fingerprint_sha256
      }));
      return success(command, JSON.stringify({
        schema_version: 1,
        inventory_hash: rawSha256(JSON.stringify(digest)),
        seats: rows
      }));
    }
    if (command.includes("reveal.py")) {
      const envName = [...this.values.keys()].find((name) => command.includes(name));
      if (!envName) return { command, exit_code: 1, stdout: "", stderr: "missing" };
      const value = this.values.get(envName)!;
      return success(command, JSON.stringify({ schema_version: 1, value_b64: Buffer.from(value).toString("base64") }));
    }
    if (command.includes("sudo -n python3") && command.includes("patch.py")) {
      assert.ok(this.stage, "stage must be uploaded before patch");
      const changes = this.stage.mutations.map((mutation) => {
        const envName = new Map([
          ["zhipu-a", "ZHIPU_SEAT_A_API_KEY"],
          ["zhipu-b", "ZHIPU_SEAT_B_API_KEY"],
          ["zhipu-c", "ZHIPU_SEAT_C_API_KEY"],
          ["zhipu-d", "ZHIPU_SEAT_D_API_KEY"]
        ]).get(mutation.seat_id)!;
        const oldValue = this.values.get(envName);
        if (mutation.action === "delete") this.values.delete(envName);
        if (mutation.action === "set") this.values.set(envName, String(mutation.value));
        const newValue = this.values.get(envName);
        return {
          seat_id: mutation.seat_id,
          action: mutation.action,
          old_fingerprint: oldValue ? fingerprintSecret(oldValue).slice(0, 12).toUpperCase() : "",
          new_fingerprint: newValue ? fingerprintSecret(newValue).slice(0, 12).toUpperCase() : "",
          present: newValue !== undefined
        };
      });
      this.backupCreated = true;
      return success(command, JSON.stringify({
        schema_version: 1,
        inventory_hash_after: "f".repeat(64),
        changes
      }));
    }
    if (command.includes("sudo -n test -f")) {
      return this.backupCreated ? success(command) : { command, exit_code: 1, stdout: "", stderr: "" };
    }
    if (command.includes("sudo -n rm -f")) this.backupCreated = false;
    return success(command);
  }
}

async function fixture(): Promise<{ controller: OwnerChatSecretController; transport: FakeSecretTransport }> {
  const root = await mkdtemp(join(tmpdir(), "agenelf-chat-secret-"));
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
    "    server: relay-prod",
    "    env_file: /srv/new-api/.env.secrets",
    "    seats:",
    "      zhipu-a: ZHIPU_SEAT_A_API_KEY",
    "      zhipu-b: ZHIPU_SEAT_B_API_KEY",
    "      zhipu-c: ZHIPU_SEAT_C_API_KEY",
    "      zhipu-d: ZHIPU_SEAT_D_API_KEY",
    "    reload:",
    "      type: none"
  ].join("\n") + "\n");
  const servers = new ServerCatalog(root, serversFile, join(root, "local", "secrets"));
  const targets = new SecretTargetCatalog(root, servers, targetsFile);
  const transport = new FakeSecretTransport();
  const controller = new OwnerChatSecretController(root, { servers, targets, transport });
  await controller.initialize();
  return { controller, transport };
}

test("owner chat snapshot returns exact plaintext for all configured stable seats", async () => {
  const { controller } = await fixture();
  const snapshot = await controller.snapshot("relay-zhipu");
  assert.equal(snapshot.plaintext, true);
  assert.deepEqual((snapshot.seats as Array<Record<string, unknown>>).map((row) => row.value), [
    "alpha-plaintext-A1",
    "bravo-plaintext-B2",
    "charlie-plaintext-C3",
    "delta-plaintext-D4"
  ]);

  const single = await controller.snapshot("relay-zhipu", "zhipu-b");
  assert.deepEqual((single.seats as Array<Record<string, unknown>>).map((row) => row.value), ["bravo-plaintext-B2"]);
});

test("owner chat direct patch deletes B, replaces C, and automatically keeps A and D", async () => {
  const { controller, transport } = await fixture();
  const replacement = "charlie-owner-updated-C9";
  const result = await controller.apply("relay-zhipu", [
    { seat_id: "zhipu-b", action: "delete" },
    { seat_id: "zhipu-c", action: "set", value: replacement }
  ], "relay-zhipu");

  assert.equal(result.status, "succeeded");
  assert.equal(transport.values.get("ZHIPU_SEAT_A_API_KEY"), "alpha-plaintext-A1");
  assert.equal(transport.values.has("ZHIPU_SEAT_B_API_KEY"), false);
  assert.equal(transport.values.get("ZHIPU_SEAT_C_API_KEY"), replacement);
  assert.equal(transport.values.get("ZHIPU_SEAT_D_API_KEY"), "delta-plaintext-D4");
  assert.ok(transport.stage);
  assert.deepEqual(transport.stage!.mutations.map((row) => [row.seat_id, row.action]), [
    ["zhipu-a", "keep"],
    ["zhipu-b", "delete"],
    ["zhipu-c", "set"],
    ["zhipu-d", "keep"]
  ]);
  assert.equal(transport.commands.some((command) => command.includes(replacement)), false, "plaintext must not enter SSH argv");
  assert.equal(transport.writes.some((item) => item.path.endsWith("stage.json") && item.content.includes(replacement)), true);
  assert.equal(transport.backupCreated, false, "successful patch removes rollback backup");
});

test("owner chat direct patch requires exact target confirmation", async () => {
  const { controller } = await fixture();
  await assert.rejects(
    () => controller.apply("relay-zhipu", [{ seat_id: "zhipu-b", action: "delete" }], "another-target"),
    /confirm_target/
  );
});
