import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PromptTemplateLoader } from "../packages/core/src/prompt-templates.ts";

async function root(): Promise<string> {
  const value = await mkdtemp(join(tmpdir(), "agenelf-prompts-test-"));
  await mkdir(join(value, "node", "prompts"), { recursive: true });
  await mkdir(join(value, "local", "prompts"), { recursive: true });
  return value;
}

test("prompt templates discover slash commands and expand quoted arguments", async () => {
  const value = await root();
  await writeFile(join(value, "node", "prompts", "plan.md"), "---\nname: plan\ndescription: Builtin plan\n---\nGoal={{input}} First={{1}} Second={{2}}\n");
  const loader = new PromptTemplateLoader(value);
  await loader.discover();
  assert.deepEqual(loader.commands(), ["/plan", "/prompt:plan"]);
  const expanded = loader.expandCommand('/plan "Node migration" safe');
  assert.equal(expanded?.name, "plan");
  assert.match(String(expanded?.prompt), /Goal="Node migration" safe/);
  assert.match(String(expanded?.prompt), /First=Node migration/);
  assert.match(String(expanded?.prompt), /Second=safe/);
});

test("owner prompt overrides builtin without exposing paths or executing code", async () => {
  const value = await root();
  await writeFile(join(value, "node", "prompts", "review.md"), "---\nname: review\ndescription: builtin\n---\nBuiltin {{input}}\n");
  await writeFile(join(value, "local", "prompts", "review.md"), "---\nname: review\ndescription: owner\n---\nOwner {{input}}\n");
  await writeFile(join(value, "local", "prompts", "ignored.ts"), "throw new Error('must not execute')\n");
  const loader = new PromptTemplateLoader(value);
  await loader.discover();
  const catalog = loader.catalog();
  assert.equal(catalog.length, 1);
  assert.equal(catalog[0].source, "owner");
  assert.equal(Object.hasOwn(catalog[0], "path"), false);
  assert.equal(loader.expand("review", "change").prompt, "Owner change");
});

test("prompt frontmatter rejects executable or unknown metadata", async () => {
  const value = await root();
  await writeFile(join(value, "node", "prompts", "unsafe.md"), "---\nname: unsafe\ncommand: rm -rf\n---\ntext\n");
  const loader = new PromptTemplateLoader(value);
  await assert.rejects(() => loader.discover(), /不支持的 frontmatter key/);
});
