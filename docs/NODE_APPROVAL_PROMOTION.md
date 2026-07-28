# Node Approval Default Promotion

## Default entry

`compose.yaml` is now the default Docker Compose application model. It includes the stable base graph from `docker-compose.yml` and promotes the verified Node overlays:

- Node Agent/API/CLI;
- owner-private Markdown Prompt Templates;
- Node Validation Runner already present in the base graph;
- Node Approval key initializer and networkless Approval Broker.

Therefore the normal operator commands keep their existing shape:

```bash
make start
make chat
docker compose up -d --build
```

## Preserved rollback

Explicit file selection bypasses the default `compose.yaml`, so the complete Python rollback remains unchanged:

```bash
docker compose -f docker-compose.python.yml up -d --build
```

The old `docker-compose.yml` and `docker-compose.override.yml` remain available as migration building blocks. `docker-compose.node-approval.yml` remains a focused canary overlay and protocol test fixture.

## Safety invariants

The default topology must prove all of the following before merge:

- `approval-key-init` and `approval-runner` use `agenelf-node:local` and `Dockerfile.node`;
- retained Python Ops/Repair/Self-upgrade continue using `agenelf:local` and are not overwritten;
- Approval Runner remains `network_mode: none`;
- Approval key is read-only for Owner CLI and Approval Runner;
- Agent/API never receives the approval key;
- owner Prompt Templates remain read-only for Agent/CLI;
- a real signed command produces immutable decision/result evidence;
- explicit Python rollback still runs `scripts/approval_runner.py`.

## Remaining Node.js migration

After this promotion, the default Node trust domains are Agent/API/CLI, Validation and Owner Approval. Remaining Python domains are intentionally retained until their own isolated migrations pass complete gates:

1. read-only Ops execution;
2. Repair Runner;
3. change/privileged Ops;
4. Self-upgrade Runner;
5. remaining legacy API routes and final Python archival.
