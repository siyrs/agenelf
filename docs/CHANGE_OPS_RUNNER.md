# Node change/privileged Ops Runner

## Purpose

The change/privileged Ops Runner executes only exact, owner-approved server operations.
It is separate from the automatic read-only Runner even though both reuse the same
`op-*` request, result, lock and event directories.

## Supported operations

| Capability | Operation | Risk |
| --- | --- | --- |
| `server.operations` | `apt_update` | change |
| `server.operations` | `compose_deploy` | change |
| `server.operations` | `compose_down` | change |
| `server.operations` | `service_restart` | change |
| `server.operations` | `docker_install` | privileged |
| `docker.operations` | `restart_docker_container` | change |

Unknown operations and parameters are rejected. The model cannot submit a free-form
remote command.

## Exact approval and race boundary

1. The Agent writes an immutable `op-*` request with canonical fingerprint and TTL.
2. The owner signs an exact approval command through the Node CLI.
3. The networkless Approval Broker writes a fingerprint-bound decision.
4. The change Runner checks the decision before attempting the shared lock.
5. After acquiring the lock it rereads the request and decision.
6. A denial, replacement, expiry or fingerprint mismatch before the lock wins and no SSH
   connection is opened.
7. Only then can a fixed operation template execute.

The Runner does not mount the approval HMAC key. It sees only final decisions.

## SSH trust boundary

`OpenSshTransport` is shared with the read-only Runner and enforces:

- exact local `ssh` argv with `shell:false`;
- owner-configured host, port and username;
- strict known-host verification by default;
- owner-configured private key or password environment variable;
- no SSH agent or implicit local key discovery;
- bounded connect/command timeout and output;
- redaction of likely credentials and proxy subscription URIs.

Compose YAML is transferred through SSH stdin to a fixed temporary path. The content is
never embedded in the local process argv, event payload or command evidence.

## Compose security and rollback

Before SSH, the Node Runner parses the controlled YAML subset and rejects:

- `privileged: true`;
- host network, PID, IPC or user namespace;
- `cap_add: ALL`;
- device mappings;
- Docker Socket mounts;
- host root mounts;
- absolute bind sources outside the owner's `allowed_bind_roots`.

On the target it then:

1. writes a mode-0600 temporary Compose file;
2. executes `docker compose config` against that file;
3. backs up the current managed Compose file;
4. atomically promotes the temporary file;
5. optionally pulls images;
6. deploys and records status;
7. restores the backup and redeploys it if pull or deploy fails.

`compose_down` deliberately does not pass `--volumes` or `--rmi`, so named volumes,
images, the Compose file and backup history remain.

## Pi-style operation timeline

The Runner appends sanitized events to `data/ops-events/<op-id>.jsonl`, including:

- `ops.runner.claimed`;
- `ops.approval.checked`;
- `ssh.started` / `ssh.completed`;
- `compose.backup.created`;
- `compose.rollback.started` / `compose.rollback.completed`;
- `ops.result.persisted` / `ops.failed`.

The event timeline supports Web, CLI and audit replay. `data/ops-results` remains the
trusted terminal fact source.

## Deployment and rollback

Default Compose starts:

- `read-ops-runner` for semantic read requests;
- `change-ops-runner` for the supported change/privileged set.

The former Python `ops-runner` is available only with the `python-ops` profile. The
explicit command below remains a complete all-Python rollback and does not partition
operations:

```bash
docker compose -f docker-compose.python.yml up -d --build
```
