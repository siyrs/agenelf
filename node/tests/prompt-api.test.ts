import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createAgenelfServer } from "../apps/api/src/main.ts";

test("API exposes prompt catalog and deterministic expansion", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = "prompt-api-token";
  const root = await mkdtemp(join(tmpdir(), "agenelf-prompt-api-test-"));
  await mkdir(join(root, "web"), { recursive: true });
  await mkdir(join(root, "node", "prompts"), { recursive: true });
  await writeFile(join(root, "web", "index.html"), "<html>prompt-api</html>");
  await writeFile(join(root, "node", "prompts", "plan.md"), "---\nname: plan\ndescription: plan api\n---\nPlan={{input}} First={{1}}\n");
  const server = await createAgenelfServer({ root });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing address");
  const base = `http://127.0.0.1:${address.port}`;
  const headers = { "x-agenelf-token": "prompt-api-token", "content-type": "application/json" };
  try {
    const catalogResponse = await fetch(`${base}/prompts`, { headers });
    assert.equal(catalogResponse.status, 200);
    const catalog = await catalogResponse.json();
    assert.equal(catalog.prompts[0].command, "/plan");
    assert.equal(Object.hasOwn(catalog.prompts[0], "path"), false);

    const expandedResponse = await fetch(`${base}/prompts/plan/expand`, {
      method: "POST",
      headers,
      body: JSON.stringify({ input: '"Node migration" safe' })
    });
    assert.equal(expandedResponse.status, 200);
    const expanded = await expandedResponse.json();
    assert.match(expanded.prompt, /Plan="Node migration" safe/);
    assert.match(expanded.prompt, /First=Node migration/);
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
  }
});
