#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def replace_once(path: str, old: str, new: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[patched] {path}")


def insert_before(path: str, marker: str, block: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    if block.strip() in text:
        return
    if text.count(marker) != 1:
        raise RuntimeError(f"{path}: marker mismatch")
    file.write_text(text.replace(marker, block + marker, 1), encoding="utf-8")
    print(f"[inserted] {path}")


replace_once(
    "app/core/capabilities.py",
    '_VALID_RISKS = {"read", "change", "privileged", "forbidden"}',
    '_VALID_RISKS = {"read", "change", "privileged", "irreversible", "forbidden"}',
)

POLICY_METHOD = '''    def evaluate_declared(
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

'''
insert_before(
    "app/core/policy.py",
    "    # ------------------------------------------------------------------\n    # 策略查询辅助\n",
    POLICY_METHOD,
)

replace_once(
    "app/core/registry.py",
    "from .capabilities import normalize_capability_meta\n",
    "from .capabilities import normalize_capability_meta\nfrom .execution_policy import audit_dispatch, evaluate_contract, resolve_contract\n",
)
replace_once(
    "app/core/registry.py",
    "    def __init__(self, skills_dir: str, extra_skills_dirs: list[str] | None = None):\n",
    "    def __init__(\n        self,\n        skills_dir: str,\n        extra_skills_dirs: list[str] | None = None,\n        *,\n        policy_engine: Any | None = None,\n    ):\n",
)
replace_once(
    "app/core/registry.py",
    "        self._origins: dict[str, str] = {}\n",
    "        self._origins: dict[str, str] = {}\n        self.policy_engine = policy_engine\n        self._contracts: dict[str, Any | None] = {}\n",
)
replace_once(
    "app/core/registry.py",
    '''    def _rebuild_tool_index(self) -> None:\n        index: dict[str, str] = {}\n        for skill_name, module in self.skills.items():\n            for tool in getattr(module, "TOOLS", []):\n                function = tool.get("function", {}) if isinstance(tool, dict) else {}\n                tool_name = function.get("name")\n                if not tool_name:\n                    continue\n                previous = index.get(tool_name)\n                if previous is not None and previous != skill_name:\n                    raise ValueError(\n                        f"工具名冲突：{tool_name} 同时由 {previous} 与 {skill_name} 提供"\n                    )\n                index[tool_name] = skill_name\n        self._tool_index = index\n''',
    '''    def _rebuild_tool_index(self) -> None:\n        index: dict[str, str] = {}\n        contracts: dict[str, Any | None] = {}\n        for skill_name, module in self.skills.items():\n            for tool in getattr(module, "TOOLS", []):\n                function = tool.get("function", {}) if isinstance(tool, dict) else {}\n                tool_name = function.get("name")\n                if not tool_name:\n                    continue\n                previous = index.get(tool_name)\n                if previous is not None and previous != skill_name:\n                    raise ValueError(\n                        f"工具名冲突：{tool_name} 同时由 {previous} 与 {skill_name} 提供"\n                    )\n                index[tool_name] = skill_name\n                contracts[tool_name] = resolve_contract(str(tool_name), {}, module)\n        self._tool_index = index\n        self._contracts = contracts\n''',
)
replace_once(
    "app/core/registry.py",
    '''            descriptor = self._descriptor_for(name, module)\n            descriptor["origin"] = self._origins.get(name, ORIGIN_APP)\n            catalog.append(descriptor)\n''',
    '''            descriptor = self._descriptor_for(name, module)\n            descriptor["origin"] = self._origins.get(name, ORIGIN_APP)\n            tool_names = [\n                str(tool.get("function", {}).get("name"))\n                for tool in getattr(module, "TOOLS", [])\n                if tool.get("function", {}).get("name")\n            ]\n            descriptor["tool_contracts"] = [\n                self._contracts[tool_name].as_dict()\n                for tool_name in tool_names\n                if self._contracts.get(tool_name) is not None\n            ]\n            descriptor["unclassified_tools"] = [\n                tool_name for tool_name in tool_names if self._contracts.get(tool_name) is None\n            ]\n            catalog.append(descriptor)\n''',
)
replace_once(
    "app/core/registry.py",
    '''    def dispatch(self, tool_name: str, args: dict) -> str:\n        skill_name = self._tool_index.get(tool_name)\n        if skill_name is None:\n            return f"错误：未知工具 {tool_name}"\n        module = self.skills[skill_name]\n        try:\n            return str(module.execute(tool_name, args or {}))\n        except Exception:\n            return f"错误：工具 {tool_name} 执行异常\\n{traceback.format_exc(limit=3)}"\n\n''',
    '''    def contract_for(self, tool_name: str, args: dict | None = None):\n        skill_name = self._tool_index.get(tool_name)\n        if skill_name is None:\n            return None\n        return resolve_contract(tool_name, args or {}, self.skills[skill_name])\n\n    def unclassified_tools(self) -> list[str]:\n        return sorted(name for name, contract in self._contracts.items() if contract is None)\n\n    def dispatch(self, tool_name: str, args: dict, *, subject: str = "agent") -> str:\n        skill_name = self._tool_index.get(tool_name)\n        if skill_name is None:\n            return f"错误：未知工具 {tool_name}"\n        module = self.skills[skill_name]\n        contract = resolve_contract(tool_name, args or {}, module)\n        decision = evaluate_contract(self.policy_engine, contract, subject)\n        audit_dispatch(tool_name, contract, subject, decision)\n        if not decision.get("allowed", False):\n            return f"错误：策略拒绝工具 {tool_name}：{decision.get('reason', '未说明原因')}"\n        try:\n            return str(module.execute(tool_name, args or {}))\n        except Exception:\n            return f"错误：工具 {tool_name} 执行异常\\n{traceback.format_exc(limit=3)}"\n\n''',
)

replace_once(
    "app/core/agent.py",
    "from .memory import MemoryStore\nfrom .registry import SkillRegistry\n",
    "from .memory import MemoryStore\nfrom .policy import PolicyEngine\nfrom .registry import SkillRegistry\n",
)
replace_once(
    "app/core/agent.py",
    '''        self.registry = SkillRegistry(\n            config.get("skills_dir", "skills"),\n            extra_skills_dirs=extra_skills_dirs or None,\n        )\n''',
    '''        self.registry = SkillRegistry(\n            config.get("skills_dir", "skills"),\n            extra_skills_dirs=extra_skills_dirs or None,\n            policy_engine=PolicyEngine(config.get("policy_dir")),\n        )\n''',
)
replace_once(
    "app/core/agent.py",
    "    def chat(self, user_input: str) -> str:\n",
    "    def chat(self, user_input: str, *, subject: str = \"agent\") -> str:\n",
)
replace_once(
    "app/core/agent.py",
    '                result = self.registry.dispatch(call["name"], call["arguments"])\n',
    '                result = self.registry.dispatch(\n                    call["name"], call["arguments"], subject=subject\n                )\n',
)

replace_once(
    "app/core/configuration.py",
    '''    config["runtime_root"] = str(root)\n    config["local_dir"] = str(local_dir)\n''',
    '''    config["runtime_root"] = str(root)\n    config["policy_dir"] = str(root / "policy")\n    config["local_dir"] = str(local_dir)\n''',
)

replace_once(
    "app/api.py",
    '''class ChatRequest(BaseModel):\n    message: str\n''',
    '''class ChatRequest(BaseModel):\n    message: str\n    channel: str = "http"\n''',
)
replace_once(
    "app/api.py",
    '    text = get_agent().registry.dispatch(tool_name, args or {})\n',
    '    text = get_agent().registry.dispatch(tool_name, args or {}, subject="api")\n',
)
replace_once(
    "app/api.py",
    '''    try:\n        reply = get_agent().chat(request.message)\n    except Exception as exc:\n''',
    '''    channel = request.channel.strip().lower()\n    if channel not in {"http", "mobile_device", "voice"}:\n        raise HTTPException(status_code=400, detail="channel 只能是 http、mobile_device 或 voice")\n    try:\n        reply = get_agent().chat(request.message, subject=channel)\n    except Exception as exc:\n''',
)

replace_once(
    "app/cli.py",
    '    text = agent.registry.dispatch(tool_name, args or {})\n',
    '    text = agent.registry.dispatch(tool_name, args or {}, subject="cli")\n',
)
replace_once(
    "app/cli.py",
    "            reply = agent.chat(user_input)\n",
    "            reply = agent.chat(user_input, subject=\"cli\")\n",
)

EXEC_VALIDATOR = '''\n\ndef validate_execution_modes(policy: Any) -> list[str]:\n    errors: list[str] = []\n    root = _mapping(policy, "execution_modes_root", errors)\n    if root.get("schema_version") != 1 or root.get("status") != "active":\n        errors.append("execution-modes 策略必须 schema_version=1 且 status=active")\n    defaults = _mapping(root.get("defaults"), "execution_modes.defaults", errors)\n    if defaults.get("unclassified_tool") != "deny":\n        errors.append("未分类工具必须默认拒绝")\n    if defaults.get("arguments_in_audit") is not False:\n        errors.append("执行审计不得记录工具参数")\n    modes = _mapping(root.get("execution_modes"), "execution_modes", errors)\n    required = {"pure", "local_state", "queued_runner", "controlled_sandbox", "host_controlled", "forbidden"}\n    missing = required - set(modes)\n    if missing:\n        errors.append(f"execution_mode 缺失：{', '.join(sorted(missing))}")\n    forbidden = _mapping(modes.get("forbidden"), "execution_modes.forbidden", errors)\n    if forbidden.get("approval") != "impossible":\n        errors.append("forbidden execution_mode 必须不可授权")\n    return errors\n'''
insert_before(
    "scripts/validate_governance.py",
    "\ndef load_policy(path: Path) -> Any:\n",
    EXEC_VALIDATOR,
)
replace_once(
    "scripts/validate_governance.py",
    '''    errors = validate_policy(policy)\n    if errors:\n''',
    '''    errors = validate_policy(policy)\n    execution_path = path.parent / "execution-modes.v1.yaml"\n    try:\n        execution_policy = load_policy(execution_path)\n    except (OSError, yaml.YAMLError) as exc:\n        print(f"执行模式策略读取失败：{exc}", file=sys.stderr)\n        return 2\n    errors.extend(validate_execution_modes(execution_policy))\n    if errors:\n''',
)
replace_once(
    "scripts/validate_governance.py",
    '    print(f"治理策略校验通过：{path}")\n',
    '    print(f"治理策略校验通过：{path} + {execution_path}")\n',
)

replace_once(
    "scripts/gate_check.sh",
    '''    "core/registry.py"\n    "core/code_repair.py"\n''',
    '''    "core/registry.py"\n    "core/policy.py"\n    "core/execution_policy.py"\n    "core/capabilities.py"\n    "core/code_repair.py"\n''',
)

replace_once(
    "policy/safety-constraints.v1.yaml",
    '''  - app/core/registry.py\n  - app/core/code_repair.py\n''',
    '''  - app/core/registry.py\n  - app/core/policy.py\n  - app/core/execution_policy.py\n  - app/core/capabilities.py\n  - app/core/code_repair.py\n''',
)

print("registry execution policy middleware applied")
