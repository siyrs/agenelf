# Node Self-upgrade Runner

## Purpose

The Node Self-upgrade Runner is the trusted application stage of Agenelf's two-step
owner-authorized upgrade workflow. It does not generate candidates or owner decisions.
It consumes an already generated candidate only after the owner approved both the intent
and the exact candidate binding.

The existing Python candidate generator and authorization state machine remain the
single workflow source during this migration batch. The production process that verifies,
backs up, applies and rolls back files is Node.js/TypeScript.

## Exact inputs

The Runner reads only:

- `data/authorized-upgrades/<session>.json` and session evidence;
- `data/self-upgrade-requests/self-upgrade-*.json`;
- `data/auth-requests` and `data/auth-decisions`;
- the read-only candidate repository under `app-tmp/repo`;
- the trusted `run_authorized_upgrade_tests.py` bridge;
- the exact owner-authorized upgrade target mounts.

It does not mount `local/`, SSH secrets, the approval HMAC key, Docker Socket or Git
metadata.

## Verification order

Before changing a target file, the Runner verifies:

1. request schema, ID and Python-compatible request fingerprint;
2. exact session, intent/candidate authorization IDs and candidate binding;
3. session state and proof that intent authorization was consumed by candidate generation;
4. exact changed-file records;
5. candidate repository path;
6. candidate tree manifest and digest;
7. baseline-manifest and test-report evidence SHA-256;
8. every path against the owner-approved allowlist and permanent forbidden roots;
9. candidate file hash and current target baseline hash;
10. diff-aware permanent redlines and required root-of-trust tokens;
11. candidate authorization before the lock;
12. request, session and authorization again after the lock;
13. the complete trusted Python and Node candidate test suite;
14. authorization again immediately before exclusive consumption.

A pending authorization does not create a result or mutate the target. Denial, expiry,
binding mismatch, replacement or prior consumption fails closed.

## Atomic application and rollback

After exclusive authorization consumption:

- an exact per-request backup directory is created;
- every existing target file is backed up with SHA-256 evidence;
- candidate files are written through a same-directory temporary file, synced and renamed;
- the post-write hash must match the approved `after_sha256`;
- any failure rolls back entries in reverse order;
- newly created files are removed during rollback;
- the result records changed files, backup directory, restart requirement and test report.

The Runner never commits, pushes or merges Git branches. Restart/recreation is handled by
the owner/host control plane after trusted evidence is persisted.

## Dual-runtime image

`Dockerfile.control-plane` contains Node 24 and Python 3.12. Node is the production Runner
process. Python remains only as a trusted test tool for the existing full candidate suite.
The container is networkless, read-only, capability-dropped and has no Docker Socket.

This distinction is important: the Self-upgrade Runner body is Node, while complete Python
regression remains intentionally available until the internal legacy API and Python runtime
are retired in later batches.

## Pi-style event timeline

Sanitized append-only events are written to
`data/self-upgrade-events/<request-id>.jsonl`, including:

- `upgrade.runner.claimed`;
- `upgrade.authorization.checked`;
- `upgrade.candidate.verified`;
- `upgrade.tests.started` / `upgrade.tests.completed`;
- `upgrade.authorization.consumed`;
- `upgrade.backup.created`;
- `upgrade.file.applied`;
- `upgrade.rollback.started` / `upgrade.rollback.completed`;
- `upgrade.result.persisted` / `upgrade.failed`.

Events support Web, CLI and audit replay. `data/self-upgrade-results` and backup evidence
remain the trusted terminal facts.

## Default and rollback topologies

Default Compose starts `node-self-upgrade-runner`. The former Python service is available
only through the `python-self-upgrade` profile for diagnostics.

The explicit rollback remains complete and profile-free:

```bash
docker compose -f docker-compose.python.yml up -d --build
```
