# Node Owner Approval Broker Migration

## 1. Scope

This batch moves the owner approval key initializer, signed-command Broker and explicit owner CLI approval surface from Python to Node.js 24 / TypeScript.

It does **not** give the Agent or model access to the approval key. Approval remains a separate owner-controlled trust boundary.

## 2. Preserved protocol

The Node implementation preserves the existing file contract:

- pending requests: `data/ops-requests/op-*.json` or `data/auth-requests/auth-*.json`;
- signed commands: `data/approval-commands/apc-*.json`;
- decisions: `data/auth-decisions/<request-id>.json`;
- Broker evidence: `data/approval-results/<command-id>.json`;
- directory locks: `data/approval-locks/*.lock`;
- HMAC-SHA256 over canonical JSON;
- exact request fingerprint binding;
- short command expiry and clock-skew checks;
- deny/approve only;
- duplicate request convergence;
- immutable result evidence and retry-safe processing.

The canonical serializer intentionally matches Python `json.dumps(..., sort_keys=True, separators=(",", ":"))`, including escaped Unicode, so Python and Node commands remain interoperable during rollback.

## 3. Trust boundaries

### Agent/API

- does not mount `/agenelf/approval`;
- does not expose an approval Tool;
- cannot sign or apply owner decisions;
- can only submit governed operation requests through the existing queue bridge.

### Owner CLI

- mounts the approval key read-only;
- exposes `/approvals`, `/approve [request-id] [reason]`, `/deny [request-id] [reason]`;
- signs an exact, expiring command;
- waits for independent Broker evidence;
- converges same-binding duplicates;
- refuses implicit approval when multiple different bindings are pending.

### Approval Runner

- runs with `network_mode: none`;
- reads the HMAC key read-only;
- reads signed commands and pending request metadata;
- writes only decisions, command results, locks and heartbeat;
- does not receive server credentials, Docker Socket or model access.

## 4. Key initialization

The Node key initializer:

- creates 48 random bytes encoded as Base64URL;
- never prints the secret;
- sets ownership to `AGENELF_UID:AGENELF_GID`;
- sets mode `0440`;
- reuses a valid existing key rather than rotating it implicitly;
- runs as root only for ownership initialization, then exits.

## 5. Deployment and rollback

The canary topology is composed with:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.node-approval.yml \
  up -d --build
```

Node Approval uses the distinct `agenelf-node:local` image tag, so it cannot replace the retained Python `agenelf:local` image used by Ops, Repair and Self-upgrade during migration.

The explicit full Python rollback remains:

```bash
docker compose -f docker-compose.python.yml up -d --build
```

The Node overlay is promoted into the default topology only after Node tests, Python/Node interoperability, Compose least-privilege checks and end-to-end decision evidence are all green.

## 6. Verification gates

- Python-canonical Unicode serialization;
- request fingerprint and request-file ID tamper rejection;
- HMAC tamper rejection;
- expiry and future clock-skew rejection;
- missing and unknown command field rejection;
- one-binding duplicate convergence;
- multiple-binding ambiguity rejection;
- mismatched duplicate rejection before any decision write;
- binding-level lock and conflicting-decision rejection;
- crash recovery when decisions exist but Broker result is missing;
- command symlink and filename/document ID rejection;
- Node-signed command independently verified by Python;
- Python-signed command consumed by Node Runner;
- real Docker key initialization, owner signing, networkless Broker and evidence output;
- Agent/API absence of approval key;
- complete existing Node, Python, Validation, Prompt, security and CodeQL regressions.

## 7. Next migration batch

After the Approval Broker is stable, migrate read-only Ops execution first. SSH credentials remain isolated in the Ops Runner; the Agent continues to receive only alias catalogs and trusted results. Change/privileged Ops and Self-upgrade remain later batches.
