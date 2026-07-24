"""server_ops 技能：运维服务器常用只读操作，安全第一。

安全模型（三级）：
- 白名单只读命令（ls/ps/df/free/uptime/cat/grep/tail/ss/curl -I/
  systemctl status/uname/whoami/pwd）无需确认直接执行；
- 高危命令（rm/dd/chmod/systemctl restart/装包/远程脚本管道等，见
  core.permissions.classify_command）一律拦截：先创建授权请求，必须人类
  在宿主机执行 ``scripts/approve.sh <请求ID> approve`` 批准后，携带
  ``auth_id`` 重试方可执行（授权一次性核销，默认 300 秒有效）；
- 其余命令必须 ``confirm=True`` 才会执行，否则返回需要确认的提示；
- 所有命令经子进程运行，15 秒超时，stdout/stderr 全量返回；
- 高危命令的拦截/批准/执行全程写 logs/audit.log 审计日志。
"""

from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import sys
import os
from pathlib import Path

try:
    from core import permissions
except ImportError:  # 兼容 registry 以文件路径独立加载技能的场景
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import permissions

SKILL_META = {
    "name": "server_ops",
    "description": "运维服务器：白名单只读 shell 命令直接执行、高危命令需人类授权（auth_id）、其他命令需确认；服务端口连通检测；磁盘状态查看。",
    "version": "0.3.0",
}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "执行 shell 命令。白名单只读命令（ls, ps, df, free, uptime, cat, grep, tail, "
                "ss, curl -I, systemctl status, uname, whoami, pwd）直接执行；"
                "高危命令（rm、dd、chmod、chown、kill、shutdown、systemctl stop/restart、"
                "装包、curl|sh 等）会被拦截并生成授权请求 ID——必须通知人类在宿主机执行 "
                "scripts/approve.sh <请求ID> approve 批准后，携带 auth_id=<请求ID> 重试 "
                "（授权一次性、限时有效，不可重复使用）；"
                "其余命令必须 confirm=True 才会执行，否则仅返回需要确认的提示。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令行。",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "非白名单、非高危命令的执行确认，默认 false。",
                    },
                    "auth_id": {
                        "type": "string",
                        "description": (
                            "高危命令的人类授权请求 ID（auth- 开头）。首次调用高危命令时留空，"
                            "从拦截提示中获得请求 ID 并等待人类批准后，带此参数重试。"
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_service",
            "description": "用 socket 检测指定主机端口的 TCP 连通性，返回通/不通。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "目标主机名或 IP。",
                    },
                    "port": {
                        "type": "integer",
                        "description": "目标端口（1-65535）。",
                    },
                },
                "required": ["host", "port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disk_status",
            "description": "返回 df -h 的磁盘使用结果。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

# 直接放行的只读命令（按可执行文件名匹配）
_WHITELIST = {
    "ls", "ps", "df", "free", "uptime", "cat", "grep", "tail",
    "ss", "uname", "whoami", "pwd",
}


def _split_command(command: str) -> list[str]:
    """按当前平台拆分命令行，保留 Windows 路径中的反斜杠。"""
    if os.name != "nt":
        return shlex.split(command)
    lexer = shlex.shlex(command, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] in "\"'" else token
        for token in lexer
    ]


def _git_bash() -> str | None:
    """返回 Windows 上可执行 Linux 运维命令的 Git Bash，其他平台返回 bash。"""
    if os.name != "nt":
        return shutil.which("bash")
    for candidate in (
        Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(r"C:\\Program Files\\Git\\bin\\bash.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _to_bash_token(token: str) -> str:
    """把绝对 Windows 路径转换为 Git Bash 可识别的 /c/... 形式。"""
    drive, tail = os.path.splitdrive(token)
    if os.name == "nt" and drive and tail:
        normalized_tail = tail.lstrip("\\\\/").replace("\\\\", "/")
        return f"/{drive[0].lower()}/{normalized_tail}"
    return token


def _can_execute(argv: list[str]) -> bool:
    """判断命令能否在当前平台执行，不把 Windows 开发机误判为 Linux 服务器。"""
    return bool(argv and (shutil.which(argv[0]) or _git_bash()))


def _run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """以 argv 语义执行命令；Windows 缺少 Linux 工具时经 Git Bash 兼容运行。

    先将 argv 重新安全引用，再交给 ``bash -lc``，避免 shell 元字符被重新解释。
    """
    run_kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 15,
    }
    if shutil.which(argv[0]):
        return subprocess.run(argv, **run_kwargs)
    bash = _git_bash()
    if bash is None:
        raise FileNotFoundError(argv[0])
    bash_command = shlex.join([_to_bash_token(token) for token in argv])
    return subprocess.run([bash, "-lc", bash_command], **run_kwargs)


def _is_whitelisted(argv: list[str]) -> bool:
    """判断命令是否属于白名单只读命令。"""
    if not argv:
        return False
    prog = argv[0]
    if prog in _WHITELIST:
        return True
    # curl 仅允许 -I（HEAD 请求，只取响应头）
    if prog == "curl":
        return "-I" in argv[1:] or "--head" in argv[1:]
    # systemctl 仅允许 status 子命令
    if prog == "systemctl":
        return len(argv) >= 2 and argv[1] == "status"
    return False


def _handle_dangerous(command: str, auth_id: str) -> str | None:
    """高危命令授权流程。

    返回 None 表示授权通过且已核销，可继续执行；否则返回给调用者的提示文本。
    所有拦截/批准/拒绝事件均写审计日志。
    """
    if not auth_id:
        # 未带授权 ID：创建授权请求，等待人类在宿主机裁决
        ok, result = permissions.request_auth(
            skill=SKILL_META["name"],
            action="run_shell",
            detail=command,
            reason="高危命令需人类授权后方可执行",
        )
        if not ok:
            permissions.audit("dangerous_blocked", f"授权请求创建失败：{command!r}（{result}）")
            return f"⚠️ 高危命令已拦截：{result}"
        permissions.audit("dangerous_blocked", f"高危命令拦截：{command!r}，授权请求 {result}")
        return (
            f"⚠️ 高危命令已拦截，授权请求ID：{result}，"
            f"请通知人类在宿主机执行 scripts/approve.sh {result} approve，"
            "批准后带 auth_id 重试"
        )
    # 带了授权 ID：核验状态
    status = permissions.check_auth(auth_id)
    if status == permissions.STATUS_APPROVED:
        if not permissions.consume_auth(auth_id):
            permissions.audit(
                "dangerous_denied", f"授权核销失败：{command!r} auth_id={auth_id}"
            )
            return f"高危命令未获授权：授权 {auth_id} 核销失败（可能已过期或被使用）"
        permissions.audit("dangerous_approved", f"高危命令批准执行：{command!r} auth_id={auth_id}")
        return None  # 授权通过，放行
    # 其余状态一律拒绝执行
    status_text = {
        permissions.STATUS_PENDING: "仍在等待人类裁决，请稍候重试或通知人类处理",
        permissions.STATUS_DENIED: "已被人类拒绝，不得执行",
        permissions.STATUS_EXPIRED: "授权已过期，请重新发起（留空 auth_id 再调用一次）",
        permissions.STATUS_USED: "授权已被使用（一次性），如需再执行请重新申请",
        permissions.STATUS_NOT_FOUND: "授权请求不存在，请检查 auth_id 是否正确",
    }.get(status, f"授权状态异常：{status}")
    permissions.audit(
        "dangerous_denied", f"高危命令拒绝执行：{command!r} auth_id={auth_id} 状态={status}"
    )
    return f"高危命令未获授权（{status}）：{status_text}"


def run_shell(command: str, confirm: bool = False, auth_id: str = "") -> str:
    """执行 shell 命令；白名单直放，高危命令需人类授权，其余需 confirm=True。"""
    if not isinstance(command, str) or not command.strip():
        return "执行失败：command 不能为空"
    try:
        argv = _split_command(command)
    except ValueError as exc:
        return f"执行失败：命令解析错误：{exc}"
    if not argv:
        return "执行失败：command 不能为空"
    # 高危判定必须先于可执行性探测。否则在缺少 rm 等 Linux 工具的开发机上，
    # 高危指令会绕过授权审计，直接以“命令不存在”结束。
    is_dangerous = permissions.classify_command(command) == "dangerous"
    if is_dangerous:
        blocked = _handle_dangerous(command, (auth_id or "").strip())
        if blocked is not None:
            return blocked
        permissions.audit("dangerous_exec", f"高危命令已执行：{command!r}")

    if not _can_execute(argv):
        return f"执行失败：命令 {argv[0]!r} 不存在"
    if not is_dangerous and not _is_whitelisted(argv) and not confirm:
        return (
            f"命令 {command!r} 不在只读白名单内，存在风险。"
            "如确认执行，请以 confirm=True 重新调用。"
        )
    try:
        proc = _run_command(argv)
    except subprocess.TimeoutExpired:
        return "执行超时（15 秒限制），进程已被终止"
    except OSError as exc:
        return f"执行失败：{exc}"
    parts = [f"退出码：{proc.returncode}"]
    parts.append(f"stdout:\n{proc.stdout}" if proc.stdout else "stdout:（空）")
    parts.append(f"stderr:\n{proc.stderr}" if proc.stderr else "stderr:（空）")
    return "\n".join(parts)


def check_service(host: str, port: int) -> str:
    """socket 检测 TCP 连通性，返回通/不通。"""
    if not isinstance(host, str) or not host.strip():
        return "检测失败：host 不能为空"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return f"检测失败：port 无效：{port!r}"
    if not 1 <= port <= 65535:
        return f"检测失败：port 超出范围（1-65535）：{port}"
    try:
        with socket.create_connection((host, port), timeout=5):
            return f"{host}:{port} 连通（TCP 可达）"
    except (OSError, socket.timeout) as exc:
        return f"{host}:{port} 不通：{exc}"


def disk_status() -> str:
    """返回 df -h 结果。"""
    try:
        proc = _run_command(["df", "-h"])
    except subprocess.TimeoutExpired:
        return "执行超时（15 秒限制）"
    except OSError as exc:
        return f"执行失败：{exc}"
    if proc.returncode != 0:
        return f"df -h 执行失败（退出码 {proc.returncode}）：\n{proc.stderr}"
    return proc.stdout


_DISPATCH = {
    "run_shell": lambda a: run_shell(
        a.get("command", ""),
        bool(a.get("confirm", False)),
        str(a.get("auth_id", "") or ""),
    ),
    "check_service": lambda a: check_service(a.get("host", ""), a.get("port", 0)),
    "disk_status": lambda a: disk_status(),
}


def execute(tool_name: str, args: dict) -> str:
    """按协议路由工具调用；内部捕获所有异常并返回字符串。"""
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(_DISPATCH))}"
    try:
        return handler(args or {})
    except Exception as exc:  # 兜底：协议要求永不抛异常
        return f"执行失败：{type(exc).__name__}: {exc}"
