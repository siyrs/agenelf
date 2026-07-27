import { readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

async function files(directory: string): Promise<string[]> {
  const output: string[] = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) output.push(...await files(path));
    else if (entry.isFile() && path.endsWith(".ts")) output.push(path);
  }
  return output;
}

const root = resolve(process.cwd(), "node");
const candidates = await files(root);
let failed = false;
for (const path of candidates) {
  const [major, minor] = process.versions.node.split(".").map(Number);
  const runtimeArgs = major < 24 || (major === 24 && minor < 12)
    ? ["--experimental-strip-types", "--check", path]
    : ["--check", path];
  const result = spawnSync(process.execPath, runtimeArgs, { encoding: "utf8" });
  if (result.status !== 0) {
    failed = true;
    console.error(`\n[FAIL] ${path}\n${result.stdout}${result.stderr}`);
  }
}
console.log(`Checked ${candidates.length} TypeScript files with Node ${process.version}.`);
if (failed) process.exitCode = 1;
