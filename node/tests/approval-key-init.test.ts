import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { initializeApprovalKey } from "../apps/approval-key-init/src/main.ts";

test("approval key init creates a private idempotent owner-readable key", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-approval-key-"));
  const path = join(root, "approval", "key");
  const previousUid = process.env.AGENELF_UID;
  const previousGid = process.env.AGENELF_GID;
  process.env.AGENELF_UID = String(process.getuid?.() ?? 1000);
  process.env.AGENELF_GID = String(process.getgid?.() ?? 1000);
  try {
    const created = await initializeApprovalKey(path);
    assert.equal(created.created, true);
    const first = await readFile(path, "utf8");
    assert.ok(first.trim().length >= 32);
    const firstStat = await stat(path);
    assert.equal(firstStat.mode & 0o777, 0o440);

    const reused = await initializeApprovalKey(path);
    assert.equal(reused.created, false);
    assert.equal(await readFile(path, "utf8"), first);
    const reusedStat = await stat(path);
    assert.equal(reusedStat.mode & 0o777, 0o440);
  } finally {
    if (previousUid === undefined) delete process.env.AGENELF_UID; else process.env.AGENELF_UID = previousUid;
    if (previousGid === undefined) delete process.env.AGENELF_GID; else process.env.AGENELF_GID = previousGid;
  }
});
