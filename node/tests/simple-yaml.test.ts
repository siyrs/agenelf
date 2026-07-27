import test from "node:test";
import assert from "node:assert/strict";
import { parseSimpleYaml } from "../packages/core/src/simple-yaml.ts";

test("simple YAML parses validation-style mappings, lists and scalars", () => {
  const value = parseSimpleYaml(`
# comments are ignored
checks:
  health:
    type: http
    url: http://127.0.0.1:8000/health
    method: GET
    expected_status: [200, 204]
    enabled: true
    ratio: 1.5
    note: 'owner''s check'
    json_equals:
      status: ok
      nested.value: 7
    tags: [agenelf, smoke]
suites:
  smoke:
    description: 基础检查
    checks:
      - health
`);
  const checks = value.checks as Record<string, Record<string, unknown>>;
  assert.equal(checks.health.type, "http");
  assert.equal(checks.health.url, "http://127.0.0.1:8000/health");
  assert.deepEqual(checks.health.expected_status, [200, 204]);
  assert.equal(checks.health.enabled, true);
  assert.equal(checks.health.ratio, 1.5);
  assert.equal(checks.health.note, "owner's check");
  assert.deepEqual(checks.health.tags, ["agenelf", "smoke"]);
  assert.deepEqual((value.suites as Record<string, unknown>).smoke, {
    description: "基础检查",
    checks: ["health"]
  });
});

test("simple YAML supports bounded inline objects without code execution", () => {
  assert.deepEqual(parseSimpleYaml("value: {name: test, count: 2, enabled: true}\n"), {
    value: { name: "test", count: 2, enabled: true }
  });
});

test("simple YAML fails closed on dangerous or ambiguous syntax", () => {
  const cases = [
    "base: &base\n  value: 1\n",
    "value: *base\n",
    "value: !tag test\n",
    "value: |\n  multiline\n",
    "value: >\n  folded\n",
    "value:\n\tchild: bad\n",
    "same: 1\nsame: 2\n",
    " value: bad\n",
    "odd:\n   child: bad\n"
  ];
  for (const source of cases) assert.throws(() => parseSimpleYaml(source), /YAML/);
});

test("simple YAML enforces byte, line and depth bounds", () => {
  assert.throws(() => parseSimpleYaml(`value: ${"x".repeat(256 * 1024)}\n`), /字节上限/);
  assert.throws(() => parseSimpleYaml(Array.from({ length: 4_001 }, (_, index) => `k${index}: ${index}`).join("\n")), /行上限/);
  let deep = "";
  for (let index = 0; index < 35; index += 1) deep += `${"  ".repeat(index)}k${index}:\n`;
  deep += `${"  ".repeat(35)}value: 1\n`;
  assert.throws(() => parseSimpleYaml(deep), /嵌套超过/);
});
