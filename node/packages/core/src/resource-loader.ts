import { lstat, readFile, readdir } from "node:fs/promises";
import { extname, join } from "node:path";
import { sha256 } from "./canonical.ts";
import type { JsonObject, JsonValue } from "./types.ts";

export interface ResourceManifest {
  id: string;
  name: string;
  description: string;
  version: string;
  kind: "skill" | "prompt" | "context" | "ui" | "policy";
  trust: "builtin" | "owner" | "third_party";
  path: string;
  capabilities: string[];
  hash?: string;
}

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export class ResourceLoader {
  readonly root: string;
  readonly resourceDir: string;
  private manifests = new Map<string, ResourceManifest>();

  constructor(root: string) {
    this.root = root;
    this.resourceDir = join(root, "node", "resources");
  }

  async discover(): Promise<ResourceManifest[]> {
    this.manifests.clear();
    let names: string[] = [];
    try { names = (await readdir(this.resourceDir)).filter((name) => name.endsWith(".json")); } catch { return []; }
    for (const name of names.sort()) {
      const path = join(this.resourceDir, name);
      const info = await lstat(path);
      if (info.isSymbolicLink() || !info.isFile()) continue;
      const raw = JSON.parse(await readFile(path, "utf8")) as Partial<ResourceManifest>;
      const id = String(raw.id ?? "");
      if (!ID_RE.test(id) || this.manifests.has(id)) continue;
      const trust = raw.trust === "owner" || raw.trust === "third_party" ? raw.trust : "builtin";
      const kind = ["skill", "prompt", "context", "ui", "policy"].includes(String(raw.kind)) ? raw.kind as ResourceManifest["kind"] : "context";
      const manifest: ResourceManifest = {
        id,
        name: String(raw.name ?? id).slice(0, 200),
        description: String(raw.description ?? "").slice(0, 1000),
        version: String(raw.version ?? "0.0.0").slice(0, 40),
        kind,
        trust,
        path: String(raw.path ?? "").replace(/^[/\\]+/, ""),
        capabilities: Array.isArray(raw.capabilities) ? raw.capabilities.map(String).slice(0, 50) : [],
        hash: raw.hash ? String(raw.hash) : undefined
      };
      this.manifests.set(id, manifest);
    }
    return this.catalog();
  }

  catalog(): ResourceManifest[] {
    return [...this.manifests.values()].map((manifest) => ({ ...manifest, path: manifest.path ? "available-on-demand" : "" }));
  }

  async loadContent(id: string): Promise<{ manifest: ResourceManifest; content: string; hash: string }> {
    const manifest = this.manifests.get(id);
    if (!manifest) throw new Error(`resource 不存在：${id}`);
    if (!manifest.path) throw new Error(`resource ${id} 没有内容路径`);
    if (manifest.trust === "third_party" && process.env.AGENELF_ENABLE_THIRD_PARTY_RESOURCES !== "1") {
      throw new Error("第三方 resource 默认禁用，必须由主人显式开启");
    }
    const path = join(this.root, manifest.path);
    const info = await lstat(path);
    if (info.isSymbolicLink() || !info.isFile()) throw new Error("resource path 必须是普通文件");
    if (![".md", ".txt", ".json", ".yaml", ".yml"].includes(extname(path).toLowerCase())) throw new Error("resource loader 不执行代码文件");
    const content = await readFile(path, "utf8");
    if (Buffer.byteLength(content, "utf8") > 128 * 1024) throw new Error("resource 内容超过 128 KiB");
    const hash = sha256({ content } as unknown as JsonValue);
    if (manifest.hash && manifest.hash !== hash) throw new Error("resource hash 不匹配");
    return { manifest, content, hash };
  }
}
