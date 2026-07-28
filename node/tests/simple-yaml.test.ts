import test from "node:test";
import assert from "node:assert/strict";
import { parseSimpleYaml } from "../packages/core/src/simple-yaml.ts";

test("strict YAML parser handles validation configuration subset", () => {
  const value = parseSimpleYaml([
    "checks:",
    "  health:",
    "    type: http",
    "    expected_status: [200, 204]",
    "    json_equals:",
    "      status: ok",
    "    tags: [smoke, node]",
    "suites:",
    "  smoke:",
    "    checks:",
    "      - health",
    ""
  ].join("\n"));
  assert.deepEqual(value.checks, {
    health: {
      type: "http",
      expected_status: [200, 204],
      json_equals: { status: "ok" },
      tags: ["smoke", "node"]
    }
  });
  assert.deepEqual(value.suites, { smoke: { checks: ["health"] } });
});

test("strict YAML parser rejects aliases, duplicate keys and tabs", () => {
  assert.throws(() => parseSimpleYaml("defaults: &base\n  type: http\n"), /anchor\/tag\/merge/);
  assert.throws(() => parseSimpleYaml("checks: {}\nchecks: {}\n"), /key 重复/);
  assert.throws(() => parseSimpleYaml("checks:\n\tbad: true\n"), /tab/);
});
