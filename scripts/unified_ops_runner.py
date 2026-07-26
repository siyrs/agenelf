#!/usr/bin/env python3
"""Unified deterministic SSH runner with structured Docker and Compose lifecycle ops.

The model never receives SSH credentials or arbitrary shell access.  Every request is
fingerprint-bound and validated again by this runner.  Server profiles reload before
each queue scan; malformed edits retain the last known-good snapshot.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ops_runner import (  # noqa: E402
    ROOT,
    CommandResult,
    OpsRunner,
    RunnerError,
    _expired,
    _read_json,
    now_iso,
)
from core import operations  # noqa: E402
from core.privacy import redact_sensitive_text  # noqa: E402

_DOCKER_CAPABILITY = "docker.operations"
_SERVER_CAPABILITY = "server.operations"
_COMPOSE_DOWN = "compose_down"
_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CHECK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DOCKER_RISKS = {
    "get_docker_logs": operations.RISK_READ,
    "inspect_docker_container": operations.RISK_READ,
    "run_docker_check": operations.RISK_READ,
    "restart_docker_container": operations.RISK_CHANGE,
}
_PROXY_URI_RE = re.compile(
    r"(?i)\b(vmess|vless|trojan|ss|ssr|hysteria2?|tuic)://[^\s\"']+"
)
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|secret|password|passwd|api[_-]?key|key)=)[^&\s\"']+"
)
_INSPECT_FORMAT = (
    '{"Name":{{json .Name}},'
    '"Image":{{json .Config.Image}},'
    '"State":{{json .State}},'
    '"Mounts":{{json .Mounts}},'
    '"Labels":{{json .Config.Labels}},'
    '"RestartPolicy":{{json .HostConfig.RestartPolicy}},'
    '"NetworkMode":{{json .HostConfig.NetworkMode}},'
    '"Networks":{{json .NetworkSettings.Networks}}}'
)


def _sanitize_text(value: Any) -> str:
    text = redact_sensitive_text(value)
    text = _PROXY_URI_RE.sub(lambda match: f"{match.group(1)}://[REDACTED]", text)
    return _URL_SECRET_RE.sub(r"\1[REDACTED]", text)


def _safe_command_result(result: Any) -> dict[str, Any]:
    data = result.as_dict()
    for key in ("command", "stdout", "stderr"):
        data[key] = _sanitize_text(data.get(key, ""))
    return data


class UnifiedOpsRunner(OpsRunner):
    """Extend the existing server runner without weakening its queue/approval model."""

    def refresh_profiles(self) -> bool:
        try:
            loaded = self._load_profiles()
        except Exception as exc:
            self.audit(
                "profiles_reload_failed",
                f"{type(exc).__name__}: {_sanitize_text(exc)}; keeping={','.join(sorted(self.profiles))}",
            )
            return False
        if loaded == self.profiles:
            return False
        before = set(self.profiles)
        after = set(loaded)
        self.profiles = loaded
        self.audit(
            "profiles_reloaded",
            "added="
            + ",".join(sorted(after - before))
            + " removed="
            + ",".join(sorted(before - after))
            + " current="
            + ",".join(sorted(after)),
        )
        return True

    @staticmethod
    def _container(profile: dict[str, Any], raw: Any) -> str:
        value = str(raw or "").strip()
        if not _CONTAINER_RE.fullmatch(value):
            raise RunnerError("container 名称非法")
        allowed = profile.get("allowed_containers")
        if allowed is not None:
            if not isinstance(allowed, list) or value not in {str(item) for item in allowed}:
                raise RunnerError(f"容器不在允许清单：{value}")
        return value

    @staticmethod
    def _allowed_docker_operation(profile: dict[str, Any], operation: str) -> None:
        allowed = profile.get("allowed_docker_operations")
        if allowed is None:
            return
        if not isinstance(allowed, list) or operation not in {str(item) for item in allowed}:
            raise RunnerError(f"目标 Docker 策略未允许操作：{operation}")

    @staticmethod
    def _allowed_compose_down(profile: dict[str, Any]) -> None:
        allowed = profile.get("allowed_operations")
        if allowed is None:
            return
        values = {str(item) for item in allowed} if isinstance(allowed, list) else set()
        # Existing compose_deploy authorization permits lifecycle shutdown only after a
        # separate exact owner approval; volumes/images remain protected.
        if not ({_COMPOSE_DOWN, "compose_deploy"} & values):
            raise RunnerError("目标策略未允许 compose_down")

    def _check_definition(
        self, profile: dict[str, Any], alias_raw: Any
    ) -> tuple[str, str, list[str]]:
        alias = str(alias_raw or "").strip()
        if not _CHECK_RE.fullmatch(alias):
            raise RunnerError("check 别名非法")
        checks = profile.get("docker_checks", {})
        if not isinstance(checks, dict) or not isinstance(checks.get(alias), dict):
            raise RunnerError(f"未配置 Docker 诊断别名：{alias}")
        entry = checks[alias]
        container = self._container(profile, entry.get("container", ""))
        raw_argv = entry.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv or len(raw_argv) > 32:
            raise RunnerError(f"Docker 诊断 {alias!r} 的 argv 必须是 1-32 项列表")
        argv: list[str] = []
        for item in raw_argv:
            text = str(item)
            if not text or len(text) > 500 or "\n" in text or "\x00" in text:
                raise RunnerError(f"Docker 诊断 {alias!r} 含非法参数")
            argv.append(text)
        return alias, container, argv

    @staticmethod
    def _is_compose_down(request: dict[str, Any]) -> bool:
        return (
            request.get("capability") == _SERVER_CAPABILITY
            and request.get("operation") == _COMPOSE_DOWN
        )

    def _validate_compose_down(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if request.get("schema_version") != 1:
            raise RunnerError("不支持的操作请求版本")
        target = str(request.get("target", ""))
        parameters = request.get("parameters", {})
        if not isinstance(parameters, dict):
            raise RunnerError("parameters 必须是对象")
        allowed_parameters = {"project", "timeout_seconds", "remove_orphans"}
        unknown = set(parameters) - allowed_parameters
        if unknown:
            raise RunnerError(f"compose_down 含未知参数：{sorted(unknown)}")
        project = str(parameters.get("project", ""))
        if not _PROJECT_RE.fullmatch(project):
            raise RunnerError("非法 Compose 项目名")
        try:
            timeout = int(parameters.get("timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise RunnerError("timeout_seconds 必须是整数") from exc
        if timeout < 0 or timeout > 120:
            raise RunnerError("timeout_seconds 必须在 0-120 之间")
        if not isinstance(parameters.get("remove_orphans", True), bool):
            raise RunnerError("remove_orphans 必须是布尔值")
        payload = operations.canonical_payload(
            _SERVER_CAPABILITY, _COMPOSE_DOWN, target, parameters
        )
        if operations.payload_fingerprint(payload) != request.get("fingerprint"):
            raise RunnerError("请求指纹校验失败，文件可能被篡改")
        if request.get("risk") != operations.RISK_CHANGE:
            raise RunnerError("compose_down 必须声明 change 风险")
        profile = self._profile(target)
        self._allowed_compose_down(profile)
        return payload, profile

    def _validate_request(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._is_compose_down(request):
            return self._validate_compose_down(request)
        if request.get("capability") != _DOCKER_CAPABILITY:
            return super()._validate_request(request)
        if request.get("schema_version") != 1:
            raise RunnerError("不支持的操作请求版本")
        operation = str(request.get("operation", ""))
        target = str(request.get("target", ""))
        parameters = request.get("parameters", {})
        if operation not in _DOCKER_RISKS:
            raise RunnerError("Docker 操作不受支持")
        if not isinstance(parameters, dict):
            raise RunnerError("parameters 必须是对象")
        payload = operations.canonical_payload(
            _DOCKER_CAPABILITY, operation, target, parameters
        )
        if operations.payload_fingerprint(payload) != request.get("fingerprint"):
            raise RunnerError("请求指纹校验失败，文件可能被篡改")
        expected_risk = _DOCKER_RISKS[operation]
        if request.get("risk") != expected_risk:
            raise RunnerError(f"风险级别不匹配：操作 {operation} 必须是 {expected_risk}")
        profile = self._profile(target)
        self._allowed_docker_operation(profile, operation)
        if operation in {
            "get_docker_logs",
            "inspect_docker_container",
            "restart_docker_container",
        }:
            self._container(profile, parameters.get("container", ""))
        if operation == "get_docker_logs":
            try:
                tail = int(parameters.get("tail", 100))
            except (TypeError, ValueError) as exc:
                raise RunnerError("tail 必须是整数") from exc
            if tail < 1 or tail > 1000:
                raise RunnerError("tail 必须在 1-1000 之间")
        if operation == "restart_docker_container":
            try:
                timeout = int(parameters.get("timeout_seconds", 10))
            except (TypeError, ValueError) as exc:
                raise RunnerError("timeout_seconds 必须是整数") from exc
            if timeout < 0 or timeout > 60:
                raise RunnerError("timeout_seconds 必须在 0-60 之间")
        if operation == "run_docker_check":
            self._check_definition(profile, parameters.get("check", ""))
        return payload, profile

    @staticmethod
    def _exact_change_authorization(
        paths: dict[str, Path], request: dict[str, Any], payload: dict[str, Any]
    ) -> str:
        decision = _read_json(paths["decisions"] / f"{request['id']}.json")
        if decision is None:
            return "pending"
        if decision.get("request_id") != request.get("id"):
            return "invalid"
        if decision.get("decision") == "deny":
            return "denied"
        if decision.get("decision") != "approve" or _expired(decision.get("expires_at")):
            return "invalid"
        if decision.get("fingerprint") != operations.payload_fingerprint(payload):
            return "invalid"
        return "approved"

    def _authorization_state(
        self, request: dict[str, Any], payload: dict[str, Any]
    ) -> str:
        if self._is_compose_down(request):
            return self._exact_change_authorization(self.paths, request, payload)
        if request.get("capability") != _DOCKER_CAPABILITY:
            return super()._authorization_state(request, payload)
        operation = str(request.get("operation", ""))
        if _DOCKER_RISKS[operation] == operations.RISK_READ:
            return "approved"
        return self._exact_change_authorization(self.paths, request, payload)

    def _execute_compose_down(
        self, request: dict[str, Any], profile: dict[str, Any]
    ) -> dict[str, Any]:
        params = request["parameters"]
        project = str(params["project"])
        timeout = int(params.get("timeout_seconds", 30))
        remove_orphans = bool(params.get("remove_orphans", True))
        managed_root = str(profile.get("managed_root", "/srv/agenelf")).rstrip("/")
        project_dir = f"{managed_root}/{project}"
        compose_path = f"{project_dir}/compose.yaml"
        q_dir = shlex.quote(project_dir)
        q_compose = shlex.quote(compose_path)
        docker = self._docker(profile)
        commands: list[dict[str, Any]] = []
        started = now_iso()

        with self.session_factory(profile, self.secrets_root) as ssh:
            check = ssh.run(
                f"test -d {q_dir} && test -f {q_compose}", timeout=60
            )
            commands.append(_safe_command_result(check))
            if check.ok:
                validate = ssh.run(
                    f"cd {q_dir} && {docker} compose -f {q_compose} config --services",
                    timeout=120,
                )
                commands.append(_safe_command_result(validate))
            else:
                validate = check
            if validate.ok:
                suffix = " --remove-orphans" if remove_orphans else ""
                # Deliberately no --volumes or --rmi: persistent data and images survive.
                down = ssh.run(
                    f"cd {q_dir} && {docker} compose -f {q_compose} down "
                    f"--timeout {timeout}{suffix}",
                    timeout=max(180, timeout + 120),
                )
                commands.append(_safe_command_result(down))
            else:
                down = validate
            if down.ok:
                commands.append(
                    _safe_command_result(
                        ssh.run(
                            f"cd {q_dir} && {docker} compose -f {q_compose} ps -a",
                            timeout=120,
                        )
                    )
                )

        ok = bool(commands) and all(item.get("exit_code") == 0 for item in commands)
        return {
            "schema_version": 1,
            "id": request["id"],
            "status": "succeeded" if ok else "failed",
            "target": request["target"],
            "capability": _SERVER_CAPABILITY,
            "operation": _COMPOSE_DOWN,
            "project": project,
            "preserved": ["named_volumes", "images", "compose.yaml", ".agenelf-backups"],
            "started_at": started,
            "finished_at": now_iso(),
            "commands": commands,
        }

    def _execute(
        self, request: dict[str, Any], profile: dict[str, Any]
    ) -> dict[str, Any]:
        if self._is_compose_down(request):
            return self._execute_compose_down(request, profile)
        if request.get("capability") != _DOCKER_CAPABILITY:
            return super()._execute(request, profile)

        operation = str(request["operation"])
        params = request.get("parameters", {})
        started = now_iso()
        commands: list[dict[str, Any]] = []
        docker = self._docker(profile)
        with self.session_factory(profile, self.secrets_root) as ssh:
            if operation == "get_docker_logs":
                container = shlex.quote(self._container(profile, params["container"]))
                tail = int(params.get("tail", 100))
                commands.append(
                    _safe_command_result(
                        ssh.run(f"{docker} logs --tail {tail} {container}", timeout=120)
                    )
                )
            elif operation == "inspect_docker_container":
                container = shlex.quote(self._container(profile, params["container"]))
                command = (
                    f"{docker} inspect --type container "
                    f"--format {shlex.quote(_INSPECT_FORMAT)} {container}"
                )
                commands.append(_safe_command_result(ssh.run(command, timeout=120)))
            elif operation == "run_docker_check":
                _, container_raw, argv = self._check_definition(
                    profile, params.get("check", "")
                )
                container = shlex.quote(container_raw)
                quoted_argv = " ".join(shlex.quote(item) for item in argv)
                commands.append(
                    _safe_command_result(
                        ssh.run(f"{docker} exec {container} {quoted_argv}", timeout=300)
                    )
                )
            elif operation == "restart_docker_container":
                container_raw = self._container(profile, params["container"])
                container = shlex.quote(container_raw)
                timeout = int(params.get("timeout_seconds", 10))
                restart = ssh.run(
                    f"{docker} restart --time {timeout} {container}", timeout=180
                )
                commands.append(_safe_command_result(restart))
                if restart.ok:
                    commands.append(
                        _safe_command_result(
                            ssh.run(
                                f"{docker} ps -a --filter name=^/{container_raw}$ "
                                "--format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}'",
                                timeout=60,
                            )
                        )
                    )
            else:  # pragma: no cover - validated above
                raise RunnerError(f"未实现 Docker 操作：{operation}")

        ok = bool(commands) and all(item.get("exit_code") == 0 for item in commands)
        return {
            "schema_version": 1,
            "id": request["id"],
            "status": "succeeded" if ok else "failed",
            "target": request["target"],
            "capability": _DOCKER_CAPABILITY,
            "operation": operation,
            "started_at": started,
            "finished_at": now_iso(),
            "commands": commands,
        }

    def run_once(self) -> dict[str, int]:
        self.refresh_profiles()
        return super().run_once()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agenelf unified deterministic SSH operations runner"
    )
    parser.add_argument("--once", action="store_true", help="process queue once and exit")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        runner = UnifiedOpsRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"unified ops-runner 启动失败：{type(exc).__name__}: {_sanitize_text(exc)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
