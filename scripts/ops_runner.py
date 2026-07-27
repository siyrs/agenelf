#!/usr/bin/env python3
"""Deterministic SSH operation runner for Agenelf.

This process is intentionally separate from the LLM-facing Agent.  It owns the
SSH credentials, validates every queued request again, enforces approval
fingerprints, and writes trusted result files that are mounted read-only into
the Agent container.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("AGENELF_ROOT", Path(__file__).resolve().parents[1])).resolve()
APP_DIR = ROOT / ("app-fork" if (ROOT / "app-fork").is_dir() else "app")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import operations  # noqa: E402
from skills.server_ops import validate_compose  # noqa: E402

try:
    import paramiko
except ImportError:  # pragma: no cover - reported clearly at runtime
    paramiko = None

_PROJECT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
_SERVICE_RE = re.compile(r"[a-zA-Z0-9@_.-]{1,128}")
_OPERATION_RISKS = {
    "inspect": operations.RISK_READ,
    "docker_ps": operations.RISK_READ,
    "service_status": operations.RISK_READ,
    "apt_update": operations.RISK_CHANGE,
    "compose_deploy": operations.RISK_CHANGE,
    "service_restart": operations.RISK_CHANGE,
    "docker_install": operations.RISK_PRIVILEGED,
}
_SUPPORTED = set(_OPERATION_RISKS)
_MAX_OUTPUT = 100_000


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, data: dict[str, Any], exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _expired(value: Any) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc).astimezone()
        return datetime.now().astimezone() > timestamp
    except ValueError:
        return True


def _truncate(value: str) -> str:
    if len(value) <= _MAX_OUTPUT:
        return value
    return value[:_MAX_OUTPUT] + "\n...（输出已截断）"


class RunnerError(RuntimeError):
    pass


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": _truncate(self.stdout),
            "stderr": _truncate(self.stderr),
        }


class SSHSession:
    """Small Paramiko adapter kept replaceable for unit tests."""

    def __init__(self, profile: dict[str, Any], secrets_root: Path):
        if paramiko is None:
            raise RunnerError("缺少 paramiko；请安装 app/requirements.txt")
        self.profile = profile
        self.secrets_root = secrets_root
        self.client = paramiko.SSHClient()

    def __enter__(self) -> "SSHSession":
        known_hosts = self.profile.get("known_hosts", "known_hosts")
        known_hosts_path = Path(str(known_hosts))
        if not known_hosts_path.is_absolute():
            known_hosts_path = self.secrets_root / known_hosts_path
        allow_unknown = bool(self.profile.get("allow_unknown_host_key", False))
        if known_hosts_path.is_file():
            self.client.load_host_keys(str(known_hosts_path))
        elif not allow_unknown:
            raise RunnerError(f"known_hosts 不存在：{known_hosts_path}")
        self.client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if allow_unknown else paramiko.RejectPolicy()
        )

        auth = self.profile.get("auth", {})
        if not isinstance(auth, dict):
            raise RunnerError("server.auth 必须是对象")
        auth_type = str(auth.get("type", "private_key"))
        kwargs: dict[str, Any] = {
            "hostname": str(self.profile.get("host", "")),
            "port": int(self.profile.get("port", 22)),
            "username": str(self.profile.get("username", "")),
            "timeout": int(self.profile.get("connect_timeout", 10)),
            "banner_timeout": int(self.profile.get("connect_timeout", 10)),
            "auth_timeout": int(self.profile.get("connect_timeout", 10)),
            "look_for_keys": False,
            "allow_agent": False,
        }
        if not kwargs["hostname"] or not kwargs["username"]:
            raise RunnerError("server.host 与 server.username 不能为空")
        if auth_type == "private_key":
            key_path = Path(str(auth.get("private_key", "id_ed25519")))
            if not key_path.is_absolute():
                key_path = self.secrets_root / key_path
            if not key_path.is_file():
                raise RunnerError(f"SSH 私钥不存在：{key_path}")
            kwargs["key_filename"] = str(key_path)
            passphrase_env = str(auth.get("passphrase_env", "")).strip()
            if passphrase_env:
                kwargs["passphrase"] = os.environ.get(passphrase_env)
        elif auth_type == "password_env":
            env_name = str(auth.get("password_env", "")).strip()
            if not env_name or not os.environ.get(env_name):
                raise RunnerError(f"SSH 密码环境变量未设置：{env_name}")
            kwargs["password"] = os.environ[env_name]
        else:
            raise RunnerError(f"不支持的 SSH 认证类型：{auth_type}")
        self.client.connect(**kwargs)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.client.close()

    def run(self, command: str, timeout: int = 120) -> CommandResult:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        stdin.close()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return CommandResult(command=command, exit_code=exit_code, stdout=out, stderr=err)

    def write_text(self, remote_path: str, content: str) -> None:
        sftp = self.client.open_sftp()
        try:
            with sftp.file(remote_path, "w") as handle:
                handle.write(content)
                handle.flush()
            sftp.chmod(remote_path, 0o600)
        finally:
            sftp.close()


class OpsRunner:
    def __init__(
        self,
        root: Path = ROOT,
        servers_file: Path | None = None,
        secrets_root: Path | None = None,
        session_factory=SSHSession,
    ):
        self.root = root.resolve()
        self.paths = operations.queue_paths(self.root)
        self.servers_file = servers_file or Path(
            os.environ.get("AGENELF_SERVERS_FILE", self.root / "config" / "servers.yaml")
        )
        self.secrets_root = secrets_root or Path(
            os.environ.get("AGENELF_SECRETS_DIR", self.root / "secrets")
        )
        self.session_factory = session_factory
        self.profiles = self._load_profiles()

    def _load_profiles(self) -> dict[str, dict[str, Any]]:
        if not self.servers_file.is_file():
            raise RunnerError(f"服务器配置不存在：{self.servers_file}")
        try:
            data = yaml.safe_load(self.servers_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RunnerError(f"服务器配置读取失败：{exc}") from exc
        profiles = data.get("servers", {}) if isinstance(data, dict) else {}
        if not isinstance(profiles, dict):
            raise RunnerError("servers.yaml 的 servers 必须是对象")
        return {str(name): value for name, value in profiles.items() if isinstance(value, dict)}

    def audit(self, event: str, detail: str) -> None:
        path = self.root / "logs" / "ops-runner.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] [{event}] {detail}\n")

    def _profile(self, target: str) -> dict[str, Any]:
        profile = self.profiles.get(target)
        if profile is None:
            raise RunnerError(f"未知服务器别名：{target}")
        return profile

    def _validate_request(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if request.get("schema_version") != 1:
            raise RunnerError("不支持的操作请求版本")
        operation = str(request.get("operation", ""))
        target = str(request.get("target", ""))
        capability = str(request.get("capability", ""))
        parameters = request.get("parameters", {})
        if capability != "server.operations" or operation not in _SUPPORTED:
            raise RunnerError("请求能力或操作不受支持")
        if not isinstance(parameters, dict):
            raise RunnerError("parameters 必须是对象")
        payload = operations.canonical_payload(capability, operation, target, parameters)
        fingerprint = operations.payload_fingerprint(payload)
        if fingerprint != request.get("fingerprint"):
            raise RunnerError("请求指纹校验失败，文件可能被篡改")
        expected_risk = _OPERATION_RISKS[operation]
        if request.get("risk") != expected_risk:
            raise RunnerError(
                f"风险级别不匹配：操作 {operation} 必须是 {expected_risk}"
            )

        profile = self._profile(target)
        allowed = profile.get("allowed_operations")
        if allowed is not None:
            if not isinstance(allowed, list) or operation not in {str(item) for item in allowed}:
                raise RunnerError(f"目标策略未允许操作：{operation}")
        if operation == "compose_deploy":
            project = str(parameters.get("project", ""))
            if not _PROJECT_RE.fullmatch(project):
                raise RunnerError("非法 Compose 项目名")
            validate_compose(str(parameters.get("compose_yaml", "")), profile)
        if operation in {"service_status", "service_restart"}:
            service = str(parameters.get("service", ""))
            if not _SERVICE_RE.fullmatch(service):
                raise RunnerError("非法 systemd 服务名")
            allowed_services = profile.get("allowed_services", [])
            if not isinstance(allowed_services, list) or service not in {
                str(item) for item in allowed_services
            }:
                raise RunnerError(f"服务不在允许清单：{service}")
        return payload, profile

    def _authorization_state(self, request: dict[str, Any], payload: dict[str, Any]) -> str:
        if _OPERATION_RISKS[str(request.get("operation", ""))] == operations.RISK_READ:
            return "approved"
        decision_path = self.paths["decisions"] / f"{request['id']}.json"
        decision = _read_json(decision_path)
        if decision is None:
            return "pending"
        if decision.get("request_id") != request.get("id"):
            return "invalid"
        if decision.get("decision") == "deny":
            return "denied"
        if decision.get("decision") != "approve" or _expired(decision.get("expires_at")):
            return "invalid"
        expected = operations.payload_fingerprint(payload)
        if decision.get("fingerprint") != expected:
            return "invalid"
        return "approved"

    def _docker(self, profile: dict[str, Any]) -> str:
        configured = str(profile.get("docker_command", "docker")).strip()
        if configured not in {"docker", "sudo -n docker"}:
            raise RunnerError("docker_command 只能是 docker 或 sudo -n docker")
        return configured

    def _execute(self, request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        params = request.get("parameters", {})
        started = now_iso()
        commands: list[dict[str, Any]] = []
        with self.session_factory(profile, self.secrets_root) as ssh:
            if operation == "inspect":
                command = (
                    "set -eu; "
                    "echo '=== identity ==='; hostname; id; uname -a; uptime; "
                    "echo '=== disk ==='; df -h; "
                    "echo '=== memory ==='; (free -h || true); "
                    "echo '=== docker ==='; "
                    "(command -v docker >/dev/null && docker version --format '{{.Server.Version}}' && "
                    "docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}') || true"
                )
                commands.append(ssh.run(command, timeout=60).as_dict())
            elif operation == "docker_ps":
                docker = self._docker(profile)
                command = f"{docker} ps -a --format 'table {{{{.Names}}}}\\t{{{{.Image}}}}\\t{{{{.Status}}}}\\t{{{{.Ports}}}}'"
                commands.append(ssh.run(command, timeout=60).as_dict())
            elif operation == "apt_update":
                commands.append(ssh.run("sudo -n apt-get update", timeout=600).as_dict())
            elif operation == "docker_install":
                command = (
                    "sudo -n apt-get update && "
                    "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 && "
                    "sudo -n systemctl enable --now docker && "
                    "sudo -n docker version"
                )
                commands.append(ssh.run(command, timeout=1200).as_dict())
            elif operation == "service_status":
                service = shlex.quote(str(params["service"]))
                commands.append(
                    ssh.run(f"systemctl status --no-pager --full {service}", timeout=60).as_dict()
                )
            elif operation == "service_restart":
                service = shlex.quote(str(params["service"]))
                command = (
                    f"sudo -n systemctl restart {service} && "
                    f"systemctl status --no-pager --full {service}"
                )
                commands.append(ssh.run(command, timeout=180).as_dict())
            elif operation == "compose_deploy":
                commands.extend(self._deploy_compose(ssh, profile, params))
            else:
                raise RunnerError(f"未实现操作：{operation}")

        ok = bool(commands) and all(item.get("exit_code") == 0 for item in commands)
        return {
            "schema_version": 1,
            "id": request["id"],
            "status": "succeeded" if ok else "failed",
            "target": request["target"],
            "operation": operation,
            "started_at": started,
            "finished_at": now_iso(),
            "commands": commands,
        }

    def _deploy_compose(
        self,
        ssh: SSHSession,
        profile: dict[str, Any],
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        project = str(params["project"])
        managed_root = str(profile.get("managed_root", "/srv/agenelf")).rstrip("/")
        project_dir = f"{managed_root}/{project}"
        compose_path = f"{project_dir}/compose.yaml"
        temp_path = f"{project_dir}/.compose.{uuid.uuid4().hex}.tmp.yaml"
        backup_dir = f"{project_dir}/.agenelf-backups"
        backup_path = f"{backup_dir}/{datetime.now().strftime('%Y%m%d-%H%M%S')}.yaml"
        docker = self._docker(profile)
        q_project_dir = shlex.quote(project_dir)
        q_backup_dir = shlex.quote(backup_dir)
        q_compose = shlex.quote(compose_path)
        q_temp = shlex.quote(temp_path)
        q_backup = shlex.quote(backup_path)
        results: list[dict[str, Any]] = []

        prepare = ssh.run(f"mkdir -p {q_project_dir} {q_backup_dir}", timeout=60)
        results.append(prepare.as_dict())
        if not prepare.ok:
            return results
        ssh.write_text(temp_path, str(params["compose_yaml"]))

        validate = ssh.run(f"{docker} compose -f {q_temp} config", timeout=120)
        results.append(validate.as_dict())
        if not validate.ok:
            ssh.run(f"rm -f {q_temp}", timeout=30)
            return results

        backup = ssh.run(
            f"if [ -f {q_compose} ]; then cp {q_compose} {q_backup}; fi && mv {q_temp} {q_compose}",
            timeout=60,
        )
        results.append(backup.as_dict())
        if not backup.ok:
            return results

        if bool(params.get("pull", True)):
            pull = ssh.run(f"cd {q_project_dir} && {docker} compose pull", timeout=1200)
            results.append(pull.as_dict())
            if not pull.ok:
                self._rollback(ssh, docker, project_dir, compose_path, backup_path, results)
                return results

        deploy = ssh.run(
            f"cd {q_project_dir} && {docker} compose up -d --remove-orphans",
            timeout=1200,
        )
        results.append(deploy.as_dict())
        if not deploy.ok:
            self._rollback(ssh, docker, project_dir, compose_path, backup_path, results)
            return results
        results.append(
            ssh.run(f"cd {q_project_dir} && {docker} compose ps", timeout=120).as_dict()
        )
        return results

    def _rollback(
        self,
        ssh: SSHSession,
        docker: str,
        project_dir: str,
        compose_path: str,
        backup_path: str,
        results: list[dict[str, Any]],
    ) -> None:
        command = (
            f"if [ -f {shlex.quote(backup_path)} ]; then "
            f"cp {shlex.quote(backup_path)} {shlex.quote(compose_path)} && "
            f"cd {shlex.quote(project_dir)} && {docker} compose up -d --remove-orphans; "
            "else exit 3; fi"
        )
        rollback = ssh.run(command, timeout=1200).as_dict()
        rollback["phase"] = "rollback"
        results.append(rollback)

    def process_request(self, request_path: Path) -> str:
        request = _read_json(request_path)
        if request is None:
            return "invalid"
        request_id = str(request.get("id", ""))
        result_path = self.paths["results"] / f"{request_id}.json"
        if result_path.exists():
            return "done"

        lock_path = self.paths["locks"] / f"{request_id}.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            return "locked"

        try:
            payload, profile = self._validate_request(request)
            auth_state = self._authorization_state(request, payload)
            if auth_state == "pending":
                return "pending"
            if auth_state in {"denied", "invalid"}:
                result = {
                    "schema_version": 1,
                    "id": request_id,
                    "status": "blocked",
                    "reason": "人类拒绝" if auth_state == "denied" else "授权无效或已过期",
                    "finished_at": now_iso(),
                }
                _atomic_json(result_path, result, exclusive=True)
                self.audit("blocked", f"{request_id} state={auth_state}")
                return "blocked"
            result = self._execute(request, profile)
            _atomic_json(result_path, result, exclusive=True)
            self.audit(result["status"], f"{request_id} {request['target']} {request['operation']}")
            return result["status"]
        except Exception as exc:
            result = {
                "schema_version": 1,
                "id": request_id,
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "finished_at": now_iso(),
            }
            try:
                _atomic_json(result_path, result, exclusive=True)
            except FileExistsError:
                pass
            self.audit("failed", f"{request_id} {type(exc).__name__}: {exc}")
            return "failed"
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    def run_once(self) -> dict[str, int]:
        self.paths["requests"].mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for path in sorted(self.paths["requests"].glob("op-*.json")):
            state = self.process_request(path)
            counts[state] = counts.get(state, 0) + 1
        return counts

    def watch(self, interval: float = 1.0) -> None:
        self.audit("runner_started", f"servers={','.join(sorted(self.profiles))}")
        while True:
            self.run_once()
            time.sleep(max(0.2, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf deterministic SSH ops runner")
    parser.add_argument("--once", action="store_true", help="process queue once and exit")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        runner = OpsRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"ops-runner 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
