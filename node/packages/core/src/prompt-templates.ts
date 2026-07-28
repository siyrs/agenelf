import { lstat, readFile, readdir } from "node:fs/promises";
import { basename, extname, join, resolve } from "node:path";
import { sha256 } from "./canonical.ts";
import type { JsonObject } from "./types.ts";

const NAME = /^[a-z][a-z0-9-]{0,47}$/;
const MAX_TEMPLATE_BYTES = 64 * 1024;
const MAX_TEMPLATES = 100;

export interface PromptTemplateRecord {
  name: string;
  description: string;
  source: "builtin" | "owner";
  path: string;
  body: string;
  hash: string;
}

function parseFrontmatter(content: string, fallbackName: string): { name: string; description: string; body: string } {
  const normalized = content.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  if (!normalized.startsWith("---\n")) return { name: fallbackName, description: "", body: normalized.trim() };
  const end = normalized.indexOf("\n---\n", 4);
  if (end < 0) throw new Error(`Prompt ${fallbackName} 的 frontmatter 未闭合`);
  const values: Record<string, string> = {};
  for (const raw of normalized.slice(4, end).split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf(":");
    if (separator <= 0) throw new Error(`Prompt ${fallbackName} 的 frontmatter 行非法：${line}`);
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim().replace(/^(["'])(.*)\1$/, "$2");
    if (key !== "name" && key !== "description") throw new Error(`Prompt ${fallbackName} 使用了不支持的 frontmatter key：${key}`);
    if (Object.hasOwn(values, key)) throw new Error(`Prompt ${fallbackName} 的 frontmatter key 重复：${key}`);
    values[key] = value;
  }
  return { name: values.name || fallbackName, description: values.description || "", body: normalized.slice(end + 5).trim() };
}

function splitArguments(input: string): string[] {
  const result: string[] = [];
  let current = "";
  let quote = "";
  let escaped = false;
  for (const char of input.trim()) {
    if (escaped) { current += char; escaped = false; continue; }
    if (char === "\\") { escaped = true; continue; }
    if (quote) { if (char === quote) quote = ""; else current += char; continue; }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (/\s/.test(char)) { if (current) { result.push(current); current = ""; } continue; }
    current += char;
  }
  if (escaped) current += "\\";
  if (quote) throw new Error("Prompt 参数包含未闭合引号");
  if (current) result.push(current);
  return result;
}

export class PromptTemplateLoader {
  readonly root: string;
  private templates = new Map<string, PromptTemplateRecord>();
  constructor(root: string) { this.root = resolve(root); }

  private async discoverDirectory(directory: string, source: "builtin" | "owner"): Promise<PromptTemplateRecord[]> {
    let entries;
    try { entries = await readdir(directory, { withFileTypes: true }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return []; throw error; }
    const result: PromptTemplateRecord[] = [];
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (result.length >= MAX_TEMPLATES) break;
      if (!entry.isFile() || extname(entry.name).toLowerCase() !== ".md") continue;
      const path = join(directory, entry.name);
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink()) continue;
      if (info.size > MAX_TEMPLATE_BYTES) throw new Error(`Prompt ${entry.name} 超过 ${MAX_TEMPLATE_BYTES} 字节上限`);
      const fallbackName = basename(entry.name, ".md").toLowerCase();
      const parsed = parseFrontmatter(await readFile(path, "utf8"), fallbackName);
      if (!NAME.test(parsed.name)) throw new Error(`Prompt 名称非法：${parsed.name}`);
      if (!parsed.body) throw new Error(`Prompt ${parsed.name} 内容为空`);
      result.push({ name: parsed.name, description: parsed.description.slice(0, 500), source, path, body: parsed.body, hash: sha256({ content: parsed.body } as JsonObject) });
    }
    return result;
  }

  async discover(): Promise<void> {
    const builtin = await this.discoverDirectory(join(this.root, "node", "prompts"), "builtin");
    const owner = await this.discoverDirectory(join(this.root, "local", "prompts"), "owner");
    const merged = new Map<string, PromptTemplateRecord>();
    for (const template of builtin) merged.set(template.name, template);
    for (const template of owner) merged.set(template.name, template);
    if (merged.size > MAX_TEMPLATES) throw new Error(`Prompt 数量超过 ${MAX_TEMPLATES}`);
    this.templates = merged;
  }

  catalog(): JsonObject[] {
    return [...this.templates.values()].sort((a, b) => a.name.localeCompare(b.name)).map((template) => ({
      name: template.name, command: `/${template.name}`, explicit_command: `/prompt:${template.name}`,
      description: template.description, source: template.source, hash: template.hash
    }));
  }
  commands(): string[] { return [...this.templates.keys()].sort().flatMap((name) => [`/${name}`, `/prompt:${name}`]); }
  has(name: string): boolean { return this.templates.has(name); }
  expand(name: string, input = ""): JsonObject {
    const template = this.templates.get(name);
    if (!template) throw new Error(`未知 Prompt Template：${name}`);
    const normalizedInput = String(input ?? "").trim();
    const args = splitArguments(normalizedInput);
    let expanded = template.body;
    let replaced = false;
    expanded = expanded.replace(/\{\{(?:input|args)\}\}/g, () => { replaced = true; return normalizedInput; });
    expanded = expanded.replace(/\{\{([1-9])\}\}/g, (_match, index: string) => { replaced = true; return args[Number(index) - 1] ?? ""; });
    if (!replaced && normalizedInput) expanded = `${expanded}\n\n用户输入：\n${normalizedInput}`;
    return { name: template.name, description: template.description, source: template.source, hash: template.hash, prompt: expanded.trim() };
  }
  expandCommand(line: string): JsonObject | null {
    const match = String(line ?? "").trim().match(/^\/(?:prompt:)?([a-z][a-z0-9-]{0,47})(?:\s+([\s\S]*))?$/);
    if (!match || !this.has(match[1])) return null;
    return this.expand(match[1], match[2] || "");
  }
}
