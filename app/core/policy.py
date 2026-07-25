"""统一运行时策略引擎：policy/*.yaml → evaluate API。

本模块是治理策略的唯一运行时查询入口（研究报告 M1）：静态 YAML 由
``scripts/validate_governance.py`` 校验，本引擎负责在运行时把
``policy/safety-constraints.v1.yaml`` 翻译成确定性的评估结果。

设计原则：
- 默认拒绝（default deny）：未匹配任何规则的操作一律拒绝；
- 降级不崩溃：策略文件缺失/损坏时进入 empty 模式，返回安全默认值并
  通过 ``degraded`` 标记与 reason 明确声明降级；
- 移动端/语音渠道不能充当批准人：change 及以上风险必须回到文本态确认。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# 主策略文件名：风险分级、授权模式、受保护路径等核心约束都从这里读取。
_MAIN_POLICY_FILE = "safety-constraints.v1.yaml"
# empty 模式下对外暴露的版本号，明确区别于任何真实策略版本。
_EMPTY_POLICY_VERSION = "0.0.0-empty"
# 风险级别的固定评估顺序（从低到高），forbidden 兜底。
_RISK_ORDER = ("read", "change", "privileged", "irreversible", "forbidden")
# 已开通渠道：interaction_channels.current 之外的运行时内置主体也视为已开通。
_BUILTIN_SUBJECTS = {"agent", "cli", "http", "api"}
# 受限渠道：策略中处于 planned 状态，允许只读直通，但不允许语音/移动端直接批准。
_RESTRICTED_SUBJECTS = {"mobile_device", "voice"}


def _default_policy_root() -> Path:
    """默认策略目录探测链：AGENELF_ROOT 环境变量 → 本文件上两级（仓库根）/policy。"""

    configured = os.environ.get("AGENELF_ROOT", "").strip()
    root = (
        Path(configured).resolve()
        if configured
        else Path(__file__).resolve().parents[2]
    )
    return root / "policy"


def _read_yaml(path: Path) -> dict[str, Any]:
    """容错读取 YAML：任何失败（缺失/损坏/非对象）都返回空字典。"""

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


class PolicyEngine:
    """加载 policy/*.yaml 并提供统一的运行时策略评估。"""

    def __init__(self, policy_dir: str | Path | None = None):
        """加载 policy/*.yaml；policy_dir 为 None 时按 <仓库根>/policy 探测
        （root 探测链：AGENELF_ROOT 环境变量 → 本文件上两级）。
        文件缺失/损坏时进入 empty 模式：安全默认值 + 明确降级标记，绝不崩溃。
        """

        self.policy_dir = (
            Path(policy_dir).resolve() if policy_dir else _default_policy_root()
        )
        self._load_error = ""
        self._policy: dict[str, Any] = {}
        self._degraded = True
        main_path = self.policy_dir / _MAIN_POLICY_FILE
        if not main_path.is_file():
            self._load_error = f"主策略文件缺失：{main_path}"
        else:
            data = _read_yaml(main_path)
            if not data.get("risk_levels"):
                self._load_error = f"主策略文件损坏或缺少 risk_levels：{main_path}"
            else:
                self._policy = data
                self._degraded = False

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------
    @property
    def policy_version(self) -> str:
        """主策略 policy_version；empty 模式返回 "0.0.0-empty" 以便审计识别降级。"""

        if self._degraded:
            return _EMPTY_POLICY_VERSION
        return str(self._policy.get("policy_version") or _EMPTY_POLICY_VERSION)

    @property
    def degraded(self) -> bool:
        """是否处于 empty/降级模式（策略文件缺失或损坏）。"""

        return self._degraded

    # ------------------------------------------------------------------
    # 统一策略评估
    # ------------------------------------------------------------------
    def evaluate(
        self, capability: str, operation: str, subject: str = "agent"
    ) -> dict[str, Any]:
        """统一策略评估。

        规则：
        1. 默认 deny：未匹配到任何规则时 allowed=False, risk=forbidden,
           approval=impossible；
        2. 按 policy risk_levels 的 examples 匹配 operation → 得到 risk 与
           对应 approval/auto_execute；
        3. forbidden_behaviors 中命名的行为 → 永远 forbidden/impossible；
        4. subject 为 "mobile_device" 或 "voice" 时：
           - approval 为 none 的 read 操作仍允许，但
             requires_textual_confirmation=False；
           - change/privileged/irreversible → requires_textual_confirmation=True
             （移动端必须先回到文本态确认，绝不能语音直接批准）；
           - 移动端不能充当批准人（由调用方/permissions enforce，engine 在
             reason 中明确声明）；
        5. interaction_channels 未列出的渠道 subject → allowed=False（渠道未开通）。
        """

        operation = str(operation or "").strip()
        subject = str(subject or "agent").strip()
        result: dict[str, Any] = {
            "allowed": False,
            "risk": "forbidden",
            "approval": "impossible",
            "auto_execute": False,
            "requires_textual_confirmation": False,
            "second_confirmation_required": False,
            "rollback_required": False,
            "reason": "",
            "policy_version": self.policy_version,
        }

        # 降级模式：策略不可用，一切操作默认拒绝并明确声明降级原因。
        if self._degraded:
            result["reason"] = (
                f"策略引擎处于降级模式（{self._load_error}），"
                "默认拒绝所有操作，请联系主人恢复策略文件"
            )
            return result

        # 规则 5：渠道开通检查。内置主体与 current 中列出的渠道视为已开通；
        # mobile_device/voice 属于受限渠道，走规则 4 的专用语义。
        channels = self._policy.get("interaction_channels", {})
        current = (
            set(channels.get("current", [])) if isinstance(channels, dict) else set()
        )
        opened = _BUILTIN_SUBJECTS | _RESTRICTED_SUBJECTS | current
        if subject not in opened:
            result["reason"] = (
                f"渠道未开通：{subject} 不在 interaction_channels.current"
                f"（{sorted(current)}）或内置主体中，拒绝 {capability}.{operation}"
            )
            return result

        # 规则 3：forbidden_behaviors 中命名的行为永远禁止，授权模式不可覆盖。
        if operation in set(self.forbidden_behaviors()):
            result["reason"] = (
                f"{operation} 属于永久禁止行为（forbidden_behaviors），"
                "任何授权模式都不得覆盖"
            )
            return result

        # 规则 2：按 risk_levels.examples 匹配操作所属风险级别。
        risk_levels = self._policy.get("risk_levels", {})
        matched_risk = ""
        matched_cfg: dict[str, Any] = {}
        if isinstance(risk_levels, dict):
            for risk in _RISK_ORDER:
                cfg = risk_levels.get(risk)
                if not isinstance(cfg, dict):
                    continue
                examples = cfg.get("examples", [])
                if isinstance(examples, list) and operation in examples:
                    matched_risk = risk
                    matched_cfg = cfg
                    break

        # 规则 1：默认 deny，未匹配任何规则时禁止执行。
        if not matched_risk:
            result["reason"] = (
                f"默认拒绝：{capability}.{operation} 未匹配任何 risk_levels 规则"
            )
            return result

        approval = str(matched_cfg.get("approval", "impossible"))
        result.update(
            {
                "allowed": approval != "impossible",
                "risk": matched_risk,
                "approval": approval,
                "auto_execute": bool(matched_cfg.get("auto_execute", False)),
                "second_confirmation_required": bool(
                    matched_cfg.get("second_confirmation_required", False)
                ),
                "rollback_required": bool(
                    matched_cfg.get("rollback_required", False)
                ),
                "reason": (
                    f"{capability}.{operation} 命中 {matched_risk} 级规则："
                    f"approval={approval}"
                ),
            }
        )
        if matched_risk == "forbidden":
            result["reason"] = (
                f"{capability}.{operation} 命中 forbidden 级规则，"
                "属于治理绕过或隐蔽行为，不可授权"
            )
            return result

        # 规则 4：移动端/语音渠道 —— read 直通；change 及以上必须回到文本态
        # 确认，且移动端/语音绝不能充当批准人。
        if subject in _RESTRICTED_SUBJECTS:
            if matched_risk == "read":
                result["requires_textual_confirmation"] = False
            else:
                result["requires_textual_confirmation"] = True
                # 受限渠道的 privileged 及以上风险追加二次确认，防止误触高权限操作。
                if matched_risk in ("privileged", "irreversible"):
                    result["second_confirmation_required"] = True
            channel_name = "移动端" if subject == "mobile_device" else "语音渠道"
            result["reason"] += (
                f"；{channel_name}仅可作为发起方，不能充当批准人，"
                "高风险操作必须由主人在文本态确认"
            )
        return result

    def evaluate_declared(
        self,
        capability: str,
        operation: str,
        declared_risk: str,
        subject: str = "agent",
    ) -> dict[str, Any]:
        """Evaluate an explicit capability contract without example-name guessing.

        ``evaluate`` remains compatible with legacy operation examples. Registry
        middleware uses this method because execution contracts already carry a
        reviewed risk and must not depend on matching a global examples list.
        """

        capability = str(capability or "").strip()
        operation = str(operation or "").strip()
        subject = str(subject or "agent").strip()
        risk = str(declared_risk or "").strip().lower()
        result: dict[str, Any] = {
            "allowed": False,
            "risk": "forbidden",
            "approval": "impossible",
            "auto_execute": False,
            "requires_textual_confirmation": False,
            "second_confirmation_required": False,
            "rollback_required": False,
            "reason": "",
            "policy_version": self.policy_version,
        }
        if self._degraded:
            result["reason"] = (
                f"策略引擎处于降级模式（{self._load_error}），默认拒绝所有操作"
            )
            return result
        channels = self._policy.get("interaction_channels", {})
        current = set(channels.get("current", [])) if isinstance(channels, dict) else set()
        opened = _BUILTIN_SUBJECTS | _RESTRICTED_SUBJECTS | current | {"host"}
        if subject not in opened:
            result["reason"] = f"渠道未开通：{subject}，拒绝 {capability}.{operation}"
            return result
        if operation in set(self.forbidden_behaviors()) or risk == "forbidden":
            result["reason"] = f"{capability}.{operation} 属于永久禁止行为，不可授权"
            return result
        if risk not in _RISK_ORDER:
            result["reason"] = f"未知声明风险：{risk!r}"
            return result
        risk_levels = self._policy.get("risk_levels", {})
        cfg = risk_levels.get(risk, {}) if isinstance(risk_levels, dict) else {}
        if not isinstance(cfg, dict) or not cfg:
            result["reason"] = f"策略缺少风险级别：{risk}"
            return result
        approval = str(cfg.get("approval", "impossible"))
        result.update(
            allowed=approval != "impossible",
            risk=risk,
            approval=approval,
            auto_execute=bool(cfg.get("auto_execute", False)),
            second_confirmation_required=bool(
                cfg.get("second_confirmation_required", False)
            ),
            rollback_required=bool(cfg.get("rollback_required", False)),
            reason=(
                f"{capability}.{operation} 使用显式 execution contract："
                f"risk={risk}, approval={approval}"
            ),
        )
        if subject in _RESTRICTED_SUBJECTS and risk != "read":
            result["requires_textual_confirmation"] = True
            if risk in {"privileged", "irreversible"}:
                result["second_confirmation_required"] = True
            result["reason"] += "；移动端/语音只能发起，不能充当批准人"
        return result

    # ------------------------------------------------------------------
    # 策略查询辅助
    # ------------------------------------------------------------------
    def is_protected_path(self, rel_path: str) -> bool:
        """protected_paths 前缀匹配：命中即视为治理受保护路径。"""

        normalized = str(rel_path or "").strip().lstrip("./").lstrip("/")
        if not normalized:
            return False
        protected = self._policy.get("protected_paths", [])
        if not isinstance(protected, list):
            return False
        for entry in protected:
            prefix = str(entry).strip()
            if prefix and (normalized == prefix or normalized.startswith(prefix)):
                return True
        return False

    def candidate_limits(self) -> dict[str, Any]:
        """self_evolution.candidate_limits（降级模式返回空字典）。"""

        evolution = self._policy.get("self_evolution", {})
        limits = evolution.get("candidate_limits", {}) if isinstance(evolution, dict) else {}
        return dict(limits) if isinstance(limits, dict) else {}

    def acceptance_gates(self) -> list[str]:
        """acceptance_gates 验收门列表。"""

        gates = self._policy.get("acceptance_gates", [])
        return [str(item) for item in gates] if isinstance(gates, list) else []

    def forbidden_behaviors(self) -> list[str]:
        """forbidden_behaviors 永久禁止行为列表。"""

        behaviors = self._policy.get("forbidden_behaviors", [])
        return [str(item) for item in behaviors] if isinstance(behaviors, list) else []

    def approval_requirements(self, approval_mode: str) -> list[str]:
        """owner_authorization.modes 中指定授权模式的要求列表。"""

        authorization = self._policy.get("owner_authorization", {})
        modes = (
            authorization.get("modes", {}) if isinstance(authorization, dict) else {}
        )
        mode = modes.get(str(approval_mode or "").strip(), {})
        requirements = mode.get("requirements", []) if isinstance(mode, dict) else []
        return [str(item) for item in requirements] if isinstance(requirements, list) else []
