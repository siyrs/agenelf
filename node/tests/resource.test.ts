import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ResourceLoader } from "../packages/core/src/resource-loader.ts";

test("resource loader uses progressive disclosure and does not execute code", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-resource-test-"));
  await mkdir(join(root, "node", "resources"), { recursive: true });
  await mkdir(join(root, "docs"), { recursive: true });
  await writeFile(join(root, "docs", "context.md"), "hello");
  await writeFile(join(root, "node", "resources", "context.json"), JSON.stringify({ id: "owner.context", name: "Context", description: "test", version: "1.0.0", kind: "context", trust: "owner", path: "docs/context.md", capabilities: ["owner.context"] }));
  const loader = new ResourceLoader(root);
  const catalog = await loader.discover();
  assert.equal(catalog[0].path, "available-on-demand");
  assert.equal((await loader.loadContent("owner.context")).content, "hello");
  await writeFile(join(root, "docs", "unsafe.ts"), "console.log('unsafe')");
  await writeFile(join(root, "node", "resources", "unsafe.json"), JSON.stringify({ id: "unsafe", kind: "context", trust: "owner", path: "docs/unsafe.ts" }));
  await loader.discover();
  await assert.rejects(() => loader.loadContent("unsafe"), /不执行代码文件/);
});
