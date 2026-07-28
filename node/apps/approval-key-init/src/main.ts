import { randomBytes } from "node:crypto";
import { chmod, lstat, mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export async function initializeApprovalKey(path = process.env.AGENELF_APPROVAL_KEY_FILE || "/agenelf/approval/key"): Promise<{ created: boolean; path: string; bytes: number }> {
  const target = resolve(path);
  await mkdir(dirname(target), { recursive: true });
  try {
    const info = await lstat(target);
    if (info.isFile() && !info.isSymbolicLink()) {
      const content = Buffer.from((await readFile(target)).toString("utf8").trim(), "utf8");
      if (content.length >= 32) return { created: false, path: target, bytes: info.size };
    }
  } catch { /* create below */ }

  const temp = `${target}.approval-key-${process.pid}-${Date.now()}`;
  try {
    const handle = await open(temp, "wx", 0o600);
    try {
      await handle.writeFile(`${randomBytes(48).toString("base64url")}\n`, "ascii");
      await handle.sync();
    } finally { await handle.close(); }
    await chmod(temp, 0o444);
    await rename(temp, target);
  } catch (error) {
    await rm(temp, { force: true }).catch(() => undefined);
    throw error;
  }
  const info = await lstat(target);
  return { created: true, path: target, bytes: info.size };
}

async function main(): Promise<void> {
  const result = await initializeApprovalKey();
  console.log(`approval key ready: created=${String(result.created)} bytes=${result.bytes} path=${result.path}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => { console.error(error); process.exitCode = 1; });
}
