"""permissions — 运维命令权限拦截核心（人类授权闸门）。

设计哲学：agent 提议，人类裁决。
- 三级分类：白名单命令直接放行；普通命令由技能层要求 confirm=True；
  高危命令一律拦截，必须人类在宿主机执行 scripts/approve.sh 批准后，
  agent 携带授权 ID 重试方可执行（一次性核销）。
- 授权请求落盘在 data/auth-requests/<id>.json，agent 只能创建与查询，
  裁决（approve/deny）只能由人类在宿主机完成。
- 全部拦截/批准/执行事件追加 logs/audit.log，可审计、可追溯。

本模块不依赖其他 core 模块，仅用标准库。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 授权请求默认有效期（秒）
DEFAULT_TTL_SECONDS = 300

# 防轰炸：待裁决请求达到该数量后拒绝新建
MAX_PENDING_REQUESTS = 10

# 授权请求状态
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_USED = "used"
STATUS_NOT_FOUND = "not_found"

# ----------------------------------------------------------------------
# 三级分类：whitelist / normal / dangerous
# ----------------------------------------------------------------------

# 白名单只读命令（按可执行文件名匹配；出现 shell 元字符时不适用）
_WHITELIST_PROGS = {
    "ls", "ps", "df", "free", "uptime", "cat", "grep", "tail", "ss",
    "uname", "whoami", "pwd", "echo", "date", "hostname", "id", "w",
    "last", "env", "printenv", "dig", "nslookup",
}

# 出现以下字符即视为复合命令，不适用白名单直放（交给 normal/dangerous 判定）
_SHELL_META_CHARS = (">", "<", "|", ";", "&", "`", "$(", "\n")

# 系统路径前缀（写重定向目标、mv/cp 目标命中即为高危）
_SYSTEM_PATH_RE = re.compile(r"^/(?:etc|usr|bin|sbin|boot)(?:/|$)")

# 高危模式（ERE，逐条注释含义；命中任意一条即 dangerous）
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 删除文件/目录
    (re.compile(r"\b(?:rm|rmdir)\b"), "rm/rmdir 删除操作"),
    # dd 裸写磁盘
    (re.compile(r"\bdd\b"), "dd 低层写操作"),
    # 格式化磁盘
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "mkfs 格式化磁盘"),
    # 关机/重启
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"), "关机/重启"),
    # 杀进程
    (re.compile(r"\b(?:kill|killall|pkill)\b"), "kill/killall/pkill 杀进程"),
    # 改权限/属主
    (re.compile(r"\b(?:chmod|chown)\b"), "chmod/chown 改权限属主"),
    # 账号管理
    (re.compile(r"\b(?:useradd|userdel|usermod|passwd|visudo)\b"), "账号/口令管理"),
    # 防火墙
    (re.compile(r"\biptables\b"), "iptables 防火墙变更"),
    # systemctl 的破坏性动作
    (re.compile(r"\bsystemctl\b[^|;&]*\b(?:stop|restart|disable|mask)\b"),
     "systemctl stop/restart/disable/mask"),
    # 写重定向（含 tee）到系统路径
    (re.compile(r"(?:>{1,2}|\btee\b)\s*/(?:etc|usr|bin|sbin|boot)(?:/|\s|$)"),
     "写入系统路径"),
    # curl/wget 远程脚本管道直执行
    (re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
     "远程脚本管道直执行"),
    # git 强制推送
    (re.compile(r"\bgit\b[^|;&]*\bpush\b[^|;&]*(?:--force\b|-f\b)"),
     "git push --force"),
    # fork 炸弹 :(){ :|:& };:
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"), "fork 炸弹"),
    # 安装软件包
    (re.compile(r"\b(?:pip3?|npm|apt(?:-get)?|yum)\b[^|;&]*\binstall\b"),
     "安装软件包"),
]


def _now() -> datetime:
    """当前本地时间（带时区，便于跨进程比较）。"""
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    """统一的时间序列化格式（秒级精度，与 approve.sh 内联 python 保持一致）。"""
    return dt.isoformat(timespec="seconds")


def _get_root() -> Path:
    """获取运行时根目录：AGENELF_ROOT 环境变量优先，否则取 app/ 的上一级。

    本文件固定位于 <根>/app/core/permissions.py，向上两级即根。
    """
    env_root = os.environ.get("AGENELF_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def _requests_dir(root: Path | None = None) -> Path:
    """授权请求目录 data/auth-requests/。"""
    return (root or _get_root()) / "data" / "auth-requests"


def _audit_log_path(root: Path | None = None) -> Path:
    """审计日志 logs/audit.log。"""
    return (root or _get_root()) / "logs" / "audit.log"


def _mv_cp_targets_system(command: str) -> bool:
    """检测 mv/cp 的目标（最后一个参数）是否为系统路径。

    按管道/分号切段后逐段 shlex 解析；解析失败时保守返回 False
    （其余正则规则仍会兜底，宁可少报也不在此抛异常）。
    """
    for segment in re.split(r"[|;&]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if len(tokens) >= 3 and tokens[0] in ("mv", "cp"):
            if _SYSTEM_PATH_RE.match(tokens[-1]):
                return True
    return False


def classify_command(command: str) -> str:
    """把 shell 命令分为 "whitelist" | "normal" | "dangerous" 三级。

    判定顺序：dangerous 优先（命中任意高危模式即 dangerous），
    其次 whitelist（无 shell 元字符的简单只读命令），其余为 normal。
    """
    if not isinstance(command, str) or not command.strip():
        return "normal"
    text = command.strip()

    # 1. 高危模式（全文扫描，优先于一切）
    for pattern, _desc in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            return "dangerous"
    if _mv_cp_targets_system(text):
        return "dangerous"

    # 2. 白名单：不允许 shell 元字符（重定向/管道/复合命令一律降级为 normal）
    if not any(ch in text for ch in _SHELL_META_CHARS):
        try:
            argv = shlex.split(text)
        except ValueError:
            argv = []
        if argv:
            prog = argv[0]
            if prog in _WHITELIST_PROGS:
                return "whitelist"
            # curl 仅允许 -I/--head（只取响应头）
            if prog == "curl" and ("-I" in argv[1:] or "--head" in argv[1:]):
                return "whitelist"
            # systemctl 仅允许 status 子命令
            if prog == "systemctl" and len(argv) >= 2 and argv[1] == "status":
                return "whitelist"
            # ping 必须带 -c（限定次数，避免长挂）
            if prog == "ping" and "-c" in argv[1:]:
                return "whitelist"

    # 3. 其余全部普通
    return "normal"


# ----------------------------------------------------------------------
# 授权请求生命周期
# ----------------------------------------------------------------------

def _request_path(request_id: str, root: Path | None = None) -> Path:
    """授权请求文件路径；request_id 含路径分隔符时视为非法（防穿越）。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", request_id or ""):
        raise ValueError(f"非法的授权请求 ID：{request_id!r}")
    return _requests_dir(root) / f"{request_id}.json"


def _read_request(request_id: str, root: Path | None = None) -> dict | None:
    """读取授权请求 JSON；不存在或损坏返回 None。"""
    try:
        path = _request_path(request_id, root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_request(data: dict, root: Path | None = None) -> None:
    """落盘授权请求 JSON。"""
    path = _request_path(data["id"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _count_pending(root: Path | None = None) -> int:
    """统计当前 pending 状态的请求数（防轰炸用）。"""
    req_dir = _requests_dir(root)
    if not req_dir.is_dir():
        return 0
    count = 0
    for file in req_dir.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == STATUS_PENDING:
            count += 1
    return count


def request_auth(
    skill: str,
    action: str,
    detail: str,
    reason: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[bool, str]:
    """创建一条授权请求，写 data/auth-requests/<id>.json。

    防轰炸：pending 数 >= MAX_PENDING_REQUESTS 时拒绝新建。
    成功返回 (True, request_id)，失败返回 (False, 提示信息)。
    """
    root = _get_root()
    pending = _count_pending(root)
    if pending >= MAX_PENDING_REQUESTS:
        return (
            False,
            f"待裁决授权请求已达 {pending} 条（上限 {MAX_PENDING_REQUESTS}），"
            "拒绝新建；请通知人类先处理存量请求。",
        )
    request_id = f"auth-{uuid.uuid4().hex[:12]}"
    now = _now()
    data = {
        "id": request_id,
        "skill": skill,
        "action": action,
        "detail": detail,
        "reason": reason,
        "status": STATUS_PENDING,
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=ttl_seconds)),
        "decided_at": None,
        "decided_by": None,
    }
    _write_request(data, root)
    audit("auth_request", f"{request_id} skill={skill} action={action} detail={detail!r}")
    return True, request_id


def check_auth(request_id: str) -> str:
    """查询授权请求状态。

    返回 "pending" | "approved" | "denied" | "expired" | "used" | "not_found"；
    pending/approved 超过 expires_at 一律视为 expired。
    """
    data = _read_request(request_id)
    if data is None:
        return STATUS_NOT_FOUND
    status = data.get("status", "")
    if status in (STATUS_PENDING, STATUS_APPROVED):
        try:
            expires_at = datetime.fromisoformat(str(data.get("expires_at", "")))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc).astimezone()
            if _now() > expires_at:
                return STATUS_EXPIRED
        except ValueError:
            # 时间戳损坏：安全起见按过期处理
            return STATUS_EXPIRED
    return status if status in (
        STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED, STATUS_USED,
    ) else STATUS_NOT_FOUND


def consume_auth(request_id: str) -> bool:
    """核销授权：approved 且未过期 → 标记 used（一次性）返回 True，否则 False。"""
    root = _get_root()
    data = _read_request(request_id, root)
    if data is None:
        return False
    if data.get("status") != STATUS_APPROVED:
        return False
    if check_auth(request_id) != STATUS_APPROVED:
        # 已过期（或状态异常）
        return False
    data["status"] = STATUS_USED
    _write_request(data, root)
    audit("auth_consumed", f"{request_id} 一次性授权已核销")
    return True


def audit(event: str, detail: str) -> None:
    """追加审计日志 logs/audit.log：[时间戳] [event] detail。"""
    log_path = _audit_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{_iso(_now())}] [{event}] {detail}\n")
    except OSError:
        # 审计日志写入失败不应阻断主流程，但也不静默吞掉——打到 stderr
        import sys

        print(f"[permissions] 审计日志写入失败：{log_path}", file=sys.stderr)
