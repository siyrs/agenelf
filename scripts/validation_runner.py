#!/usr/bin/env python3
"""Deterministic allowlisted software-validation runner for Agenelf.

The runner accepts only aliases from ``local/validation.yaml``.  Requests cannot
supply URLs, hosts, ports, headers or assertion rules.  It performs bounded HTTP
and TCP checks and writes trusted JSON evidence for the Agent.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

ROOT = Path(os.environ.get("AGENELF_ROOT", Path(__file__).resolve().parents[1])).resolve()
APP_DIR = ROOT / ("app-fork" if (ROOT / "app-fork").is_dir() else "app")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import validation  # noqa: E402

_MAX_BODY_BYTES = 1_000_000
_MAX_ASSERTIONS = 30


class ValidationRunnerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
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
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _json_path(value: Any, dotted_path: str) -> tuple[bool, Any]:
    current = value
    for part in str(dotted_path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        return False, None
    return True, current


class ValidationRunner:
    def __init__(
        self,
        *,
        root: str | Path = ROOT,
        validation_file: str | Path | None = None,
    ):
        self.root = Path(root).resolve()
        self.paths = validation.queue_paths(self.root)
        configured = validation_file or os.environ.get("AGENELF_VALIDATION_FILE")
        self.validation_file = Path(configured).resolve() if configured else self.root / "local" / "validation.yaml"
        self.config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        if not self.validation_file.is_file() or self.validation_file.is_symlink():
            raise ValidationRunnerError(f"验证配置不存在或不可用：{self.validation_file}")
        try:
            data = yaml.safe_load(self.validation_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValidationRunnerError(f"验证配置读取失败：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationRunnerError("validation.yaml 顶层必须是对象")
        checks = data.get("checks", {})
        suites = data.get("suites", {})
        if not isinstance(checks, dict) or not isinstance(suites, dict):
            raise ValidationRunnerError("checks 与 suites 必须是对象")
        return {"checks": checks, "suites": suites}

    def audit(self, event: str, detail: str) -> None:
        path = self.paths["audit"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{now_iso()}] [{event}] {detail}\n")
        except OSError:
            pass

    def _validate_request(self, request: dict[str, Any]) -> tuple[str, str]:
        if request.get("schema_version") != 1:
            raise ValidationRunnerError("不支持的验证请求版本")
        operation = str(request.get("operation", ""))
        target = str(request.get("target", ""))
        payload = validation.canonical_payload(operation, target)
        if request.get("capability") != "software.validation":
            raise ValidationRunnerError("请求能力不是 software.validation")
        if request.get("risk") != "read":
            raise ValidationRunnerError("软件验证必须是只读风险级别")
        if request.get("parameters") not in ({}, None):
            raise ValidationRunnerError("验证请求不得携带自由参数")
        if request.get("fingerprint") != validation.payload_fingerprint(payload):
            raise ValidationRunnerError("验证请求指纹不匹配，文件可能被篡改")
        if operation == "run_check" and target not in self.config["checks"]:
            raise ValidationRunnerError(f"未知验证检查：{target}")
        if operation == "run_suite" and target not in self.config["suites"]:
            raise ValidationRunnerError(f"未知验证套件：{target}")
        return operation, target

    @staticmethod
    def _assertion(name: str, passed: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "passed": bool(passed), "detail": str(detail)[:1000]}

    def _http_check(self, name: str, cfg: dict[str, Any]) -> dict[str, Any]:
        url = str(cfg.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValidationRunnerError(f"HTTP 检查 {name} 的 URL 非法")
        method = str(cfg.get("method", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            raise ValidationRunnerError(f"HTTP 检查 {name} 仅支持 GET/HEAD")
        timeout = _bounded_int(cfg.get("timeout_seconds"), 5, 1, 30)
        expected_raw = cfg.get("expected_status", [200])
        if isinstance(expected_raw, int):
            expected = [expected_raw]
        elif isinstance(expected_raw, list):
            expected = [int(item) for item in expected_raw[:20] if isinstance(item, int)]
        else:
            expected = [200]
        if not expected:
            expected = [200]

        started = time.monotonic()
        status_code: int | None = None
        body = b""
        network_error = ""
        try:
            request = Request(url, method=method, headers={"User-Agent": "Agenelf-Validation/1.0"})
            with urlopen(request, timeout=timeout) as response:
                status_code = int(response.getcode())
                body = response.read(_MAX_BODY_BYTES + 1)
        except HTTPError as exc:
            status_code = int(exc.code)
            body = exc.read(_MAX_BODY_BYTES + 1)
        except (URLError, TimeoutError, OSError) as exc:
            network_error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        truncated = len(body) > _MAX_BODY_BYTES
        body = body[:_MAX_BODY_BYTES]
        text = body.decode("utf-8", errors="replace")

        assertions: list[dict[str, Any]] = []
        assertions.append(
            self._assertion(
                "network",
                not network_error,
                network_error or "连接成功",
            )
        )
        assertions.append(
            self._assertion(
                "status",
                status_code in expected,
                f"实际={status_code}，期望={expected}",
            )
        )
        max_latency = cfg.get("max_latency_ms")
        if max_latency is not None:
            limit = _bounded_int(max_latency, 5000, 1, 300_000)
            assertions.append(
                self._assertion(
                    "latency",
                    latency_ms <= limit,
                    f"实际={latency_ms}ms，最大={limit}ms",
                )
            )
        contains = cfg.get("contains", [])
        if isinstance(contains, str):
            contains = [contains]
        if isinstance(contains, list):
            for index, needle in enumerate(contains[:10]):
                needle_text = str(needle)
                assertions.append(
                    self._assertion(
                        f"contains[{index}]",
                        needle_text in text,
                        f"响应正文{'包含' if needle_text in text else '不包含'}指定文本",
                    )
                )
        json_equals = cfg.get("json_equals", {})
        if isinstance(json_equals, dict) and json_equals:
            try:
                json_value = json.loads(text)
                json_error = ""
            except json.JSONDecodeError as exc:
                json_value = None
                json_error = str(exc)
            assertions.append(
                self._assertion("json_parse", json_value is not None, json_error or "JSON 解析成功")
            )
            if json_value is not None:
                for dotted, expected_value in list(json_equals.items())[:10]:
                    found, actual = _json_path(json_value, str(dotted))
                    assertions.append(
                        self._assertion(
                            f"json_equals:{dotted}",
                            found and actual == expected_value,
                            f"actual={actual!r}, expected={expected_value!r}",
                        )
                    )
        assertions = assertions[:_MAX_ASSERTIONS]
        passed = bool(assertions) and all(item["passed"] for item in assertions)
        return {
            "name": name,
            "type": "http",
            "passed": passed,
            "latency_ms": latency_ms,
            "observed": {
                "status_code": status_code,
                "body_bytes": len(body),
                "body_truncated": truncated,
            },
            "assertions": assertions,
        }

    def _tcp_check(self, name: str, cfg: dict[str, Any]) -> dict[str, Any]:
        host = str(cfg.get("host", "")).strip()
        port = _bounded_int(cfg.get("port"), 0, 1, 65535)
        timeout = _bounded_int(cfg.get("timeout_seconds"), 5, 1, 30)
        if not host or not port:
            raise ValidationRunnerError(f"TCP 检查 {name} 缺少 host/port")
        started = time.monotonic()
        error = ""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        assertions = [self._assertion("connect", not error, error or "TCP 连接成功")]
        max_latency = cfg.get("max_latency_ms")
        if max_latency is not None:
            limit = _bounded_int(max_latency, 5000, 1, 300_000)
            assertions.append(
                self._assertion(
                    "latency",
                    latency_ms <= limit,
                    f"实际={latency_ms}ms，最大={limit}ms",
                )
            )
        passed = all(item["passed"] for item in assertions)
        return {
            "name": name,
            "type": "tcp",
            "passed": passed,
            "latency_ms": latency_ms,
            "observed": {"connected": not error},
            "assertions": assertions,
        }

    def run_check(self, name: str) -> dict[str, Any]:
        cfg = self.config["checks"].get(name)
        if not isinstance(cfg, dict):
            raise ValidationRunnerError(f"检查配置不是对象：{name}")
        check_type = str(cfg.get("type", "")).lower()
        started = now_iso()
        try:
            if check_type == "http":
                result = self._http_check(name, cfg)
            elif check_type == "tcp":
                result = self._tcp_check(name, cfg)
            else:
                raise ValidationRunnerError(f"检查 {name} 使用不支持的类型：{check_type}")
        except Exception as exc:
            result = {
                "name": name,
                "type": check_type or "unknown",
                "passed": False,
                "latency_ms": None,
                "observed": {},
                "assertions": [
                    self._assertion("configuration_or_execution", False, f"{type(exc).__name__}: {exc}")
                ],
            }
        result["started_at"] = started
        result["finished_at"] = now_iso()
        return result

    def _suite_members(self, name: str) -> list[str]:
        raw = self.config["suites"].get(name)
        if isinstance(raw, list):
            members = raw
        elif isinstance(raw, dict):
            members = raw.get("checks", [])
        else:
            raise ValidationRunnerError(f"套件配置非法：{name}")
        if not isinstance(members, list) or not members:
            raise ValidationRunnerError(f"套件 {name} 没有检查项")
        result = [str(item) for item in members[:100]]
        unknown = [item for item in result if item not in self.config["checks"]]
        if unknown:
            raise ValidationRunnerError(f"套件 {name} 引用了未知检查：{', '.join(unknown)}")
        return result

    def execute_request(self, request: dict[str, Any]) -> dict[str, Any]:
        operation, target = self._validate_request(request)
        started = now_iso()
        if operation == "run_check":
            checks = [self.run_check(target)]
        else:
            checks = [self.run_check(name) for name in self._suite_members(target)]
        passed_count = sum(1 for item in checks if item.get("passed"))
        failed_count = len(checks) - passed_count
        status = "succeeded" if failed_count == 0 and checks else "failed"
        return {
            "schema_version": 1,
            "id": request["id"],
            "capability": "software.validation",
            "operation": operation,
            "target": target,
            "status": status,
            "started_at": started,
            "finished_at": now_iso(),
            "summary": f"{passed_count}/{len(checks)} 个检查通过，{failed_count} 个失败",
            "passed": passed_count,
            "failed": failed_count,
            "checks": checks,
        }

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
            result = self.execute_request(request)
            _atomic_json(result_path, result, exclusive=True)
            self.audit(result["status"], f"{request_id} {request.get('operation')} {request.get('target')}")
            return result["status"]
        except Exception as exc:
            result = {
                "schema_version": 1,
                "id": request_id,
                "capability": "software.validation",
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
        for path in sorted(self.paths["requests"].glob("val-*.json")):
            state = self.process_request(path)
            counts[state] = counts.get(state, 0) + 1
        return counts

    def watch(self, interval: float = 1.0) -> None:
        self.audit("runner_started", f"checks={len(self.config['checks'])} suites={len(self.config['suites'])}")
        while True:
            self.run_once()
            time.sleep(max(0.2, float(interval)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf deterministic validation runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        runner = ValidationRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"validation-runner 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
