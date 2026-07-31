import { createHash } from "node:crypto";
import type { ManagedSecretTarget } from "./secret-targets.ts";
import type { JsonObject, JsonValue } from "./types.ts";

export const SECRET_STAGE_RE = /^secret-stage-[0-9a-f]{24}\.json$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const MAX_SECRET_CHARS = 32 * 1024;

export type SecretMutationAction = "keep" | "delete" | "set";

export interface SecretMutation extends JsonObject {
  seat_id: string;
  action: SecretMutationAction;
  expected_fingerprint: string;
  value?: string;
}

export interface SecretStage extends JsonObject {
  schema_version: 1;
  env_target: string;
  expected_inventory_hash: string;
  mutations: SecretMutation[];
  created_at: string;
}

export interface SecretInventorySeat extends JsonObject {
  seat_id: string;
  env_name: string;
  label: string;
  present: boolean;
  masked: string;
  fingerprint: string;
  fingerprint_sha256: string;
}

export interface SecretInventory extends JsonObject {
  schema_version: 1;
  env_target: string;
  inventory_hash: string;
  seats: SecretInventorySeat[];
}

export function rawSha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

export function fingerprintSecret(value: string): string {
  return rawSha256(value);
}

export function shortFingerprint(value: string): string {
  return fingerprintSecret(value).slice(0, 12).toUpperCase();
}

export function maskSecret(value: string): string {
  if (!value) return "";
  if (value.length <= 2) return "•".repeat(value.length);
  if (value.length <= 8) return `${value.slice(0, 1)}${"•".repeat(Math.min(6, value.length - 2))}${value.slice(-1)}`;
  return `${value.slice(0, 4)}••••${value.slice(-4)}`;
}

function object(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 object`);
  return value as JsonObject;
}

export function validateSecretStage(value: unknown, target: ManagedSecretTarget): SecretStage {
  const document = object(value, "secret stage");
  if (Number(document.schema_version) !== 1) throw new Error("secret stage schema_version 必须为 1");
  if (String(document.env_target ?? "") !== target.alias) throw new Error("secret stage env_target 与请求不一致");
  const expectedInventoryHash = String(document.expected_inventory_hash ?? "");
  if (!SHA256_RE.test(expectedInventoryHash)) throw new Error("secret stage expected_inventory_hash 非法");
  const createdAt = String(document.created_at ?? "");
  if (!Number.isFinite(Date.parse(createdAt))) throw new Error("secret stage created_at 非法");
  if (!Array.isArray(document.mutations)) throw new Error("secret stage mutations 必须是 array");
  const rows = document.mutations.map((item, index) => {
    const mutation = object(item, `mutations[${index}]`);
    const seatId = String(mutation.seat_id ?? "");
    const seat = target.seats.get(seatId);
    if (!seat) throw new Error(`secret stage 含未知席位：${seatId}`);
    const action = String(mutation.action ?? "") as SecretMutationAction;
    if (!(["keep", "delete", "set"] as string[]).includes(action)) throw new Error(`席位 ${seatId} action 非法`);
    const expectedFingerprint = String(mutation.expected_fingerprint ?? "");
    if (expectedFingerprint && !SHA256_RE.test(expectedFingerprint)) throw new Error(`席位 ${seatId} expected_fingerprint 非法`);
    const hasValue = Object.hasOwn(mutation, "value");
    const secret = hasValue ? String(mutation.value ?? "") : undefined;
    if (action === "set") {
      if (!hasValue || !secret) throw new Error(`席位 ${seatId} set 必须提供非空 value`);
      if (secret.length > MAX_SECRET_CHARS || /[\0\r\n]/.test(secret)) throw new Error(`席位 ${seatId} value 非法`);
    } else if (hasValue) throw new Error(`席位 ${seatId} 的 ${action} 操作不得包含 value`);
    return {
      seat_id: seatId,
      action,
      expected_fingerprint: expectedFingerprint,
      ...(action === "set" ? { value: secret as string } : {})
    } as SecretMutation;
  });
  const ids = rows.map((row) => row.seat_id);
  if (new Set(ids).size !== ids.length) throw new Error("secret stage 席位重复");
  const expectedIds = [...target.seats.keys()].sort();
  const actualIds = [...ids].sort();
  if (JSON.stringify(expectedIds) !== JSON.stringify(actualIds)) {
    throw new Error("secret stage 必须显式包含目标中的全部席位（keep/delete/set）");
  }
  return {
    schema_version: 1,
    env_target: target.alias,
    expected_inventory_hash: expectedInventoryHash,
    mutations: rows,
    created_at: createdAt
  };
}

export function parseSecretInventory(text: string, target: ManagedSecretTarget): SecretInventory {
  let parsed: JsonValue;
  try { parsed = JSON.parse(text) as JsonValue; }
  catch { throw new Error("远程密钥清单不是有效 JSON"); }
  const document = object(parsed, "inventory");
  if (Number(document.schema_version) !== 1) throw new Error("inventory schema_version 非法");
  if (!SHA256_RE.test(String(document.inventory_hash ?? ""))) throw new Error("inventory_hash 非法");
  if (!Array.isArray(document.seats)) throw new Error("inventory seats 非法");
  const seats = document.seats.map((raw, index) => {
    const row = object(raw, `inventory.seats[${index}]`);
    const seatId = String(row.seat_id ?? "");
    const configured = target.seats.get(seatId);
    if (!configured) throw new Error(`inventory 含未知席位：${seatId}`);
    const fingerprint = String(row.fingerprint_sha256 ?? "");
    const present = row.present === true;
    if (present && !SHA256_RE.test(fingerprint)) throw new Error(`inventory 席位 ${seatId} fingerprint 非法`);
    if (!present && fingerprint) throw new Error(`inventory 缺失席位 ${seatId} 不应有 fingerprint`);
    return {
      seat_id: seatId,
      env_name: configured.envName,
      label: configured.label,
      present,
      masked: present ? String(row.masked ?? "").slice(0, 64) : "",
      fingerprint: present ? String(row.fingerprint ?? "").slice(0, 16) : "",
      fingerprint_sha256: fingerprint
    } as SecretInventorySeat;
  });
  if (new Set(seats.map((row) => row.seat_id)).size !== target.seats.size) throw new Error("inventory 未返回全部配置席位");
  return {
    schema_version: 1,
    env_target: target.alias,
    inventory_hash: String(document.inventory_hash),
    seats
  };
}

export const INVENTORY_SCRIPT = String.raw`#!/usr/bin/env python3
import hashlib, json, os, re, stat, sys

LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')

def decode_value(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except Exception:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value

def load_values(path, names):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError('env file must be a regular non-symlink file')
    values, duplicates = {}, set()
    with open(path, 'r', encoding='utf-8') as handle:
        for line in handle:
            match = LINE.match(line.rstrip('\n'))
            if not match or match.group(1) not in names:
                continue
            name = match.group(1)
            if name in values:
                duplicates.add(name)
            values[name] = decode_value(match.group(2))
    if duplicates:
        raise RuntimeError('duplicate managed env keys: ' + ','.join(sorted(duplicates)))
    return values

def mask(value):
    if not value:
        return ''
    if len(value) <= 2:
        return '•' * len(value)
    if len(value) <= 8:
        return value[:1] + ('•' * min(6, len(value)-2)) + value[-1:]
    return value[:4] + '••••' + value[-4:]

def inventory(path, seats):
    values = load_values(path, {seat['env_name'] for seat in seats})
    safe, digest_rows = [], []
    for seat in sorted(seats, key=lambda item: item['seat_id']):
        value = values.get(seat['env_name'])
        full = hashlib.sha256(value.encode('utf-8')).hexdigest() if value is not None else ''
        digest_rows.append({'seat_id': seat['seat_id'], 'env_name': seat['env_name'], 'present': value is not None, 'fingerprint_sha256': full})
        safe.append({
            'seat_id': seat['seat_id'],
            'env_name': seat['env_name'],
            'present': value is not None,
            'masked': mask(value or ''),
            'fingerprint': full[:12].upper() if full else '',
            'fingerprint_sha256': full
        })
    canonical = json.dumps(digest_rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'schema_version': 1, 'inventory_hash': hashlib.sha256(canonical).hexdigest(), 'seats': safe}

if __name__ == '__main__':
    env_file = sys.argv[1]
    seats = json.loads(sys.argv[2])
    print(json.dumps(inventory(env_file, seats), ensure_ascii=False, separators=(',', ':')))
`;

export const REVEAL_SCRIPT = String.raw`#!/usr/bin/env python3
import base64, json, os, re, stat, sys

LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')

def decode_value(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except Exception:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value

path, env_name = sys.argv[1], sys.argv[2]
info = os.lstat(path)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
    raise RuntimeError('env file must be a regular non-symlink file')
found = []
with open(path, 'r', encoding='utf-8') as handle:
    for line in handle:
        match = LINE.match(line.rstrip('\n'))
        if match and match.group(1) == env_name:
            found.append(decode_value(match.group(2)))
if len(found) != 1:
    raise RuntimeError('managed env key is missing or duplicated')
print(json.dumps({'schema_version': 1, 'value_b64': base64.b64encode(found[0].encode('utf-8')).decode('ascii')}, separators=(',', ':')))
`;

export const PATCH_SCRIPT = String.raw`#!/usr/bin/env python3
import fcntl, hashlib, json, os, re, shutil, stat, sys, tempfile

LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')
SAFE = re.compile(r'^[A-Za-z0-9_./:+@%,-]+$')

def decode_value(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except Exception:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value

def encode_value(value):
    return value if SAFE.fullmatch(value) else json.dumps(value, ensure_ascii=False)

def parse_lines(lines, names):
    values, indexes, duplicates = {}, {}, set()
    for index, line in enumerate(lines):
        match = LINE.match(line.rstrip('\n'))
        if not match or match.group(1) not in names:
            continue
        name = match.group(1)
        if name in values:
            duplicates.add(name)
        values[name] = decode_value(match.group(2))
        indexes[name] = index
    if duplicates:
        raise RuntimeError('duplicate managed env keys: ' + ','.join(sorted(duplicates)))
    return values, indexes

def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest() if value is not None else ''

def inventory_hash(seats, values):
    rows = []
    for seat in sorted(seats, key=lambda item: item['seat_id']):
        value = values.get(seat['env_name'])
        rows.append({'seat_id': seat['seat_id'], 'env_name': seat['env_name'], 'present': value is not None, 'fingerprint_sha256': fingerprint(value)})
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()

def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

env_file, seats_json, stage_file, backup_file = sys.argv[1:5]
seats = json.loads(seats_json)
stage = json.load(open(stage_file, 'r', encoding='utf-8'))
seat_by_id = {seat['seat_id']: seat for seat in seats}
mutations = stage.get('mutations') or []
if set(item.get('seat_id') for item in mutations) != set(seat_by_id):
    raise RuntimeError('stage must include every configured seat exactly once')
if len(mutations) != len(seat_by_id):
    raise RuntimeError('duplicate stage seat')

lock_path = env_file + '.agenelf.lock'
os.makedirs(os.path.dirname(env_file), exist_ok=True)
with open(lock_path, 'a+', encoding='utf-8') as lock:
    os.chmod(lock_path, 0o600)
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    info = os.lstat(env_file)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError('env file must be a regular non-symlink file')
    with open(env_file, 'r', encoding='utf-8') as handle:
        lines = handle.readlines()
    names = {seat['env_name'] for seat in seats}
    before, indexes = parse_lines(lines, names)
    current_hash = inventory_hash(seats, before)
    if current_hash != stage.get('expected_inventory_hash'):
        raise RuntimeError('inventory changed since owner review')
    actions = {}
    for mutation in mutations:
        seat = seat_by_id[mutation['seat_id']]
        current = before.get(seat['env_name'])
        if fingerprint(current) != mutation.get('expected_fingerprint', ''):
            raise RuntimeError('seat fingerprint changed: ' + mutation['seat_id'])
        action = mutation.get('action')
        if action not in ('keep', 'delete', 'set'):
            raise RuntimeError('invalid mutation action')
        if action == 'set' and (not isinstance(mutation.get('value'), str) or not mutation['value'] or any(ch in mutation['value'] for ch in '\x00\r\n')):
            raise RuntimeError('invalid secret value')
        if action != 'set' and 'value' in mutation:
            raise RuntimeError('unexpected value for non-set action')
        actions[seat['env_name']] = mutation

    output, emitted = [], set()
    for line in lines:
        match = LINE.match(line.rstrip('\n'))
        if not match or match.group(1) not in actions:
            output.append(line)
            continue
        env_name = match.group(1)
        mutation = actions[env_name]
        if mutation['action'] == 'delete':
            emitted.add(env_name)
            continue
        if mutation['action'] == 'set':
            output.append(env_name + '=' + encode_value(mutation['value']) + '\n')
            emitted.add(env_name)
            continue
        output.append(line)
        emitted.add(env_name)
    for seat in seats:
        env_name = seat['env_name']
        mutation = actions[env_name]
        if mutation['action'] == 'set' and env_name not in emitted:
            if output and not output[-1].endswith('\n'):
                output[-1] += '\n'
            output.append(env_name + '=' + encode_value(mutation['value']) + '\n')

    os.makedirs(os.path.dirname(backup_file), exist_ok=True)
    with open(env_file, 'rb') as source, open(backup_file, 'xb') as backup:
        shutil.copyfileobj(source, backup)
        backup.flush()
        os.fsync(backup.fileno())
    os.chmod(backup_file, 0o600)
    directory = os.path.dirname(env_file)
    descriptor, temporary = tempfile.mkstemp(prefix='.agenelf-env-', dir=directory, text=True)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.writelines(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, env_file)
        fsync_dir(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    with open(env_file, 'r', encoding='utf-8') as handle:
        after_lines = handle.readlines()
    after, _ = parse_lines(after_lines, names)
    changes = []
    for seat in sorted(seats, key=lambda item: item['seat_id']):
        old = before.get(seat['env_name'])
        new = after.get(seat['env_name'])
        action = actions[seat['env_name']]['action']
        changes.append({
            'seat_id': seat['seat_id'],
            'action': action,
            'old_fingerprint': fingerprint(old)[:12].upper() if old is not None else '',
            'new_fingerprint': fingerprint(new)[:12].upper() if new is not None else '',
            'present': new is not None
        })
    print(json.dumps({'schema_version': 1, 'inventory_hash_after': inventory_hash(seats, after), 'changes': changes}, ensure_ascii=False, separators=(',', ':')))
`;
