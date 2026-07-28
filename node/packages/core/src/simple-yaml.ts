import type { JsonObject, JsonValue } from "./types.ts";

interface SourceLine {
  indent: number;
  text: string;
  line: number;
}

const MAX_BYTES = 256 * 1024;
const MAX_LINES = 4_000;
const MAX_DEPTH = 32;

function stripComment(raw: string): string {
  let single = false;
  let double = false;
  let bracketDepth = 0;
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    const previous = index > 0 ? raw[index - 1] : "";
    if (char === "'" && !double && previous !== "\\") single = !single;
    else if (char === '"' && !single && previous !== "\\") double = !double;
    else if (!single && !double && (char === "[" || char === "{")) bracketDepth += 1;
    else if (!single && !double && (char === "]" || char === "}")) bracketDepth = Math.max(0, bracketDepth - 1);
    else if (char === "#" && !single && !double && bracketDepth === 0 && (index === 0 || /\s/.test(previous))) {
      return raw.slice(0, index).trimEnd();
    }
  }
  if (single || double || bracketDepth !== 0) throw new Error("YAML 引号或内联容器未闭合");
  return raw.trimEnd();
}

function splitMapping(text: string, line: number): [string, string] {
  let single = false;
  let double = false;
  let bracketDepth = 0;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const previous = index > 0 ? text[index - 1] : "";
    if (char === "'" && !double && previous !== "\\") single = !single;
    else if (char === '"' && !single && previous !== "\\") double = !double;
    else if (!single && !double && (char === "[" || char === "{")) bracketDepth += 1;
    else if (!single && !double && (char === "]" || char === "}")) bracketDepth = Math.max(0, bracketDepth - 1);
    else if (char === ":" && !single && !double && bracketDepth === 0) {
      const key = text.slice(0, index).trim();
      if (!key || key === "<<" || /^[&*!]/.test(key)) throw new Error(`YAML 第 ${line} 行包含非法 key`);
      return [key, text.slice(index + 1).trim()];
    }
  }
  throw new Error(`YAML 第 ${line} 行缺少 ':'`);
}

function splitInline(value: string): string[] {
  const items: string[] = [];
  let current = "";
  let single = false;
  let double = false;
  let depth = 0;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    const previous = index > 0 ? value[index - 1] : "";
    if (char === "'" && !double && previous !== "\\") single = !single;
    else if (char === '"' && !single && previous !== "\\") double = !double;
    else if (!single && !double && (char === "[" || char === "{")) depth += 1;
    else if (!single && !double && (char === "]" || char === "}")) depth -= 1;
    if (char === "," && !single && !double && depth === 0) {
      items.push(current.trim());
      current = "";
    } else current += char;
  }
  if (single || double || depth !== 0) throw new Error("YAML 内联容器未闭合");
  if (current.trim() || value.trim()) items.push(current.trim());
  return items;
}

function parseInlineObject(value: string, line: number): JsonObject {
  const result: JsonObject = {};
  const inner = value.slice(1, -1).trim();
  if (!inner) return result;
  for (const item of splitInline(inner)) {
    const [key, rawValue] = splitMapping(item, line);
    if (Object.hasOwn(result, key)) throw new Error(`YAML 第 ${line} 行内联 object key 重复：${key}`);
    result[key] = parseScalar(rawValue, line);
  }
  return result;
}

function parseScalar(raw: string, line: number): JsonValue {
  const value = raw.trim();
  if (!value) return "";
  if (value === "null" || value === "~") return null;
  if (value === "true") return true;
  if (value === "false") return false;
  if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) return Number(value);
  if (value.startsWith('"')) {
    try { return JSON.parse(value) as JsonValue; }
    catch { throw new Error(`YAML 第 ${line} 行双引号字符串非法`); }
  }
  if (value.startsWith("'")) {
    if (!value.endsWith("'") || value.length < 2) throw new Error(`YAML 第 ${line} 行单引号字符串非法`);
    return value.slice(1, -1).replace(/''/g, "'");
  }
  if (value.startsWith("[") && value.endsWith("]")) {
    const inner = value.slice(1, -1).trim();
    return inner ? splitInline(inner).map((item) => parseScalar(item, line)) : [];
  }
  if (value.startsWith("{") && value.endsWith("}")) return parseInlineObject(value, line);
  if (/^(?:[&*!]|<<:|---$|\.\.\.$)/.test(value) || value === "|" || value === ">") {
    throw new Error(`YAML 第 ${line} 行使用了不支持的 anchor/tag/merge/multiline 语法`);
  }
  return value;
}

function parseBlock(lines: SourceLine[], start: number, indent: number, depth: number): { value: JsonValue; next: number } {
  if (depth > MAX_DEPTH) throw new Error(`YAML 嵌套超过 ${MAX_DEPTH} 层`);
  const first = lines[start];
  if (!first || first.indent !== indent) throw new Error("YAML 缩进结构非法");
  const arrayMode = first.text === "-" || first.text.startsWith("- ");

  if (arrayMode) {
    const result: JsonValue[] = [];
    let index = start;
    while (index < lines.length && lines[index].indent === indent) {
      const source = lines[index];
      if (!(source.text === "-" || source.text.startsWith("- "))) break;
      const rest = source.text.slice(1).trim();
      if (!rest) {
        if (index + 1 >= lines.length || lines[index + 1].indent <= indent) throw new Error(`YAML 第 ${source.line} 行空数组项没有子内容`);
        const child = parseBlock(lines, index + 1, lines[index + 1].indent, depth + 1);
        result.push(child.value);
        index = child.next;
        continue;
      }
      if (rest.includes(":")) {
        const [key, rawValue] = splitMapping(rest, source.line);
        const object: JsonObject = {};
        if (rawValue) object[key] = parseScalar(rawValue, source.line);
        else if (index + 1 < lines.length && lines[index + 1].indent > indent) {
          const child = parseBlock(lines, index + 1, lines[index + 1].indent, depth + 1);
          object[key] = child.value;
          index = child.next - 1;
        } else object[key] = {};
        if (index + 1 < lines.length && lines[index + 1].indent > indent) {
          const continuation = parseBlock(lines, index + 1, lines[index + 1].indent, depth + 1);
          if (!continuation.value || Array.isArray(continuation.value) || typeof continuation.value !== "object") {
            throw new Error(`YAML 第 ${source.line} 行数组 object 的续行必须是 mapping`);
          }
          for (const [childKey, childValue] of Object.entries(continuation.value)) {
            if (Object.hasOwn(object, childKey)) throw new Error(`YAML 第 ${source.line} 行数组 object key 重复：${childKey}`);
            object[childKey] = childValue;
          }
          index = continuation.next - 1;
        }
        result.push(object);
      } else result.push(parseScalar(rest, source.line));
      index += 1;
    }
    return { value: result, next: index };
  }

  const result: JsonObject = {};
  let index = start;
  while (index < lines.length && lines[index].indent === indent) {
    const source = lines[index];
    if (source.text === "-" || source.text.startsWith("- ")) break;
    const [key, rawValue] = splitMapping(source.text, source.line);
    if (Object.hasOwn(result, key)) throw new Error(`YAML 第 ${source.line} 行 key 重复：${key}`);
    if (rawValue) {
      result[key] = parseScalar(rawValue, source.line);
      index += 1;
      continue;
    }
    if (index + 1 >= lines.length || lines[index + 1].indent <= indent) {
      result[key] = {};
      index += 1;
      continue;
    }
    const child = parseBlock(lines, index + 1, lines[index + 1].indent, depth + 1);
    result[key] = child.value;
    index = child.next;
  }
  return { value: result, next: index };
}

export function parseSimpleYaml(text: string): JsonObject {
  if (Buffer.byteLength(text, "utf8") > MAX_BYTES) throw new Error(`YAML 超过 ${MAX_BYTES} 字节上限`);
  const rawLines = text.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").split("\n");
  if (rawLines.length > MAX_LINES) throw new Error(`YAML 超过 ${MAX_LINES} 行上限`);
  const lines: SourceLine[] = [];
  for (let index = 0; index < rawLines.length; index += 1) {
    const raw = rawLines[index];
    if (raw.includes("\t")) throw new Error(`YAML 第 ${index + 1} 行包含 tab`);
    const cleaned = stripComment(raw);
    if (!cleaned.trim()) continue;
    const indent = cleaned.length - cleaned.trimStart().length;
    if (indent % 2 !== 0) throw new Error(`YAML 第 ${index + 1} 行缩进必须是 2 的倍数`);
    const textValue = cleaned.trimStart();
    if (textValue === "---" || textValue === "...") continue;
    lines.push({ indent, text: textValue, line: index + 1 });
  }
  if (!lines.length) return {};
  if (lines[0].indent !== 0) throw new Error("YAML 顶层必须从 0 缩进开始");
  const parsed = parseBlock(lines, 0, 0, 0);
  if (parsed.next !== lines.length || !parsed.value || Array.isArray(parsed.value) || typeof parsed.value !== "object") {
    throw new Error("YAML 顶层必须是 mapping");
  }
  return parsed.value;
}
