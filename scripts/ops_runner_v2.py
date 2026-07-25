#!/usr/bin/env python3
"""Hot-reloading extension for Agenelf's deterministic SSH operation runner.

The original runner remains the compatibility base. This layer adds bounded Docker
logs/diagnostics/restart operations and reloads ``servers.yaml`` before every request,
so Agent-side and Runner-side server aliases cannot drift.
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

import ops_runner as legacy  # noqa: E402
from core import operations  # noqa: E402
from core.privacy import redact_sensitive_text  # noqa: E402

_EXTRA_RISKS = {
    "docker_logs": operations.RISK_READ,
    "docker_diagnose": operations.RISK_READ,
    "docker_restart": operations.RISK_CHANGE,
}
legacy._OPERATION_RISKS.update(_EXTRA_RISKS)

_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_TAIL = 2000


def _safe_result(result: legacy.CommandResult) -> dict[str, Any]:
    data = result.as_dict()
    data["stdout"] = redact_sensitive_text(str(data.get("stdout", "")))
    data["stderr"] = redact_sensitive_text(str(data.get("stderr", "")))
    return data


class EnhancedOpsRunner(legacy.OpsRunner):
    """Existing runner semantics plus hot profile reload and Docker recovery ops."""

    def _reload_profiles(self) -> bool:
        try:
            current = self._load_profiles()
        except legacy.RunnerError as exc:
            if not self.profiles:
                raise
            self.audit("profiles_reload_failed", str(exc))
            return False
        changed = current != self.profiles
        self.profiles = current
        if changed:
            aliases = ",".join(sorted(current)) or "none"
            self.audit("profiles_reloaded", f"aliases={aliases}")
        return changed

    def _profile(self, target: str) -> dict[str, Any]:
        self._reload_profiles()
        profile = self.profiles.get(str(target))
        if profile is None:
            raise legacy.RunnerError(f"未知服务器别名：{target}")
        return profile

    @staticmethod
    def _allowed(profile: dict[str, Any], operation: str) -> None:
        raw = profile.get("allowed_operations")
        if raw is None:
            return
        if not isinstance(raw, list):
            raise legacy.RunnerError(f"目标策略未允许操作：{operation}")
        names = {str(item) for item in raw}
        if operation in names:
            return
        if operation in {"docker_logs", "docker_diagnose"} and "docker_ps" in names:
            return
        if operation == "docker_restart" and {"docker_ps", "service_restart"} <= names:
            return
        raise legacy.RunnerError(f"目标策略未允许操作：{operation}")

    def _validate_request(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        capability = str(request.get("capability", ""))
        operation = str(request.get("operation", ""))
        if capability != "server.docker":
            return super()._validate_request(request)

        if request.get("schema_version") != 1:
            raise legacy.RunnerError("不支持的操作请求版本")
        if operation not in _EXTRA_RISKS:
            raise legacy.RunnerError("请求能力或操作不受支持")
        target = str(request.get("target", ""))
        parameters = request.get("parameters", {})
        if not isinstance(parameters, dict):
            raise legacy.RunnerError("parameters 必须是对象")

        payload = operations.canonical_payload(
            capability, operation, target, parameters
        )
        expected_fingerprint = operations.payload_fingerprint(payload)
        if request.get("fingerprint") != expected_fingerprint:
            raise legacy.RunnerError("请求指纹校验失败，文件可能被篡改")
        expected_risk = _EXTRA_RISKS[operation]
        if request.get("risk") != expected_risk:
            raise legacy.RunnerError(
                f"风险级别不匹配：操作 {operation} 必须是 {expected_risk}"
            )

        profile = self._profile(target)
        self._allowed(profile, operation)
        container = str(parameters.get("container", "")).strip()
        if not _CONTAINER_RE.fullmatch(container):
            raise legacy.RunnerError("非法 Docker 容器名称或 ID")
        if operation in {"docker_logs", "docker_diagnose"}:
            tail = parameters.get("tail", 200)
            if (
                isinstance(tail, bool)
                or not isinstance(tail, int)
                or tail < 1
                or tail > _MAX_TAIL
            ):
                raise legacy.RunnerError(
                    f"Docker 日志 tail 必须是 1 到 {_MAX_TAIL} 的整数"
                )
        return payload, profile

    def _execute(
        self, request: dict[str, Any], profile: dict[str, Any]
    ) -> dict[str, Any]:
        operation = str(request.get("operation", ""))
        if operation not in _EXTRA_RISKS:
            return super()._execute(request, profile)

        params = request.get("parameters", {})
        docker = self._docker(profile)
        container = shlex.quote(str(params["container"]))
        started = legacy.now_iso()
        commands: list[dict[str, Any]] = []

        with self.session_factory(profile, self.secrets_root) as ssh:
            if operation == "docker_logs":
                tail = int(params.get("tail", 200))
                command = f"{docker} logs --tail {tail} --timestamps {container}"
                commands.append(_safe_result(ssh.run(command, timeout=120)))
            elif operation == "docker_diagnose":
                tail = int(params.get("tail", 200))
                inspect_format = (
                    "Name={{.Name}}\nImage={{.Config.Image}}\n"
                    "Status={{.State.Status}}\nExitCode={{.State.ExitCode}}\n"
                    "Error={{.State.Error}}\nRestartCount={{.RestartCount}}\n"
                    "StartedAt={{.State.StartedAt}}\nFinishedAt={{.State.FinishedAt}}\n"
                    "Mounts={{json .Mounts}}\nEnvCount={{len .Config.Env}}"
                )
                stats_format = (
                    "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}"
                )
                command = (
                    "set -eu; "
                    "echo '=== container ==='; "
                    f"{docker} inspect --type container --format "
                    f"{shlex.quote(inspect_format)} {container}; "
                    "echo '=== recent logs ==='; "
                    f"{docker} logs --tail {tail} --timestamps {container} 2>&1; "
                    "echo '=== resource snapshot ==='; "
                    f"{docker} stats --no-stream --format "
                    f"{shlex.quote(stats_format)} {container}"
                )
                commands.append(_safe_result(ssh.run(command, timeout=180)))
            elif operation == "docker_restart":
                verify_format = (
                    "Name={{.Name}} Status={{.State.Status}} "
                    "ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}}"
                )
                command = (
                    f"{docker} restart --time 10 {container} && "
                    f"{docker} inspect --type container --format "
                    f"{shlex.quote(verify_format)} {container}"
                )
                commands.append(_safe_result(ssh.run(command, timeout=180)))

        ok = bool(commands) and all(item.get("exit_code") == 0 for item in commands)
        return {
            "schema_version": 1,
            "id": request["id"],
            "status": "succeeded" if ok else "failed",
            "target": request["target"],
            "operation": operation,
            "started_at": started,
            "finished_at": legacy.now_iso(),
            "commands": commands,
        }


OpsRunner = EnhancedOpsRunner


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agenelf hot-reloading SSH ops runner with bounded Docker recovery"
    )
    parser.add_argument("--once", action="store_true", help="process queue once and exit")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        runner = EnhancedOpsRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"ops-runner-v2 启动失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
