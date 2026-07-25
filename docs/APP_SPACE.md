# 能力快车道：app-space 技能锻造

Agenelf 的自我完善有两条车道，分工明确、互不越权：

| | 慢车道（改核心） | 快车道（扩能力） |
|---|---|---|
| 目标 | 修改 `app/` 核心代码 | 新增/移除可插拔技能 |
| 路径 | `app-tmp` → 测试 → `gate_check.sh` → 宿主机 `promote.sh` | `app-space/skills` 写入 + 协议校验 + 热加载 |
| 裁决 | 宿主机安全门 + 人类晋升 | 注册表即时校验，全程审计 |
| 生效 | 晋升后替换 `app/` | 注册成功即可调用 |

快车道的存在不削弱慢车道：核心模块、安全关键技能、主人数据路径的规则一条不变。

## 架构

```text
Agent / LLM
   │  forge_skill(name, description, source_code, test_code?)
   ▼
skills/skill_forge.py                # 名称/同名/保护清单校验
   │
   ▼
core/registry.py
   register_external_skill(dirname, filename, source_code, test_code=None)
   ├─ 越权检查：只允许写 extra 目录第一个（<runtime_root>/app-space/skills）
   ├─ ast 语法校验
   ├─ 规模约束：≤500 行 / ≤64000 字符
   ├─ 危险模式扫描：与 scripts/gate_check.sh 检查 a/6 同族
   ├─ 测试门禁（附 test_code 时）：测试代码校验 + 沙盒运行，见下节
   ├─ 临时导入 + 协议校验（SKILL_META / TOOLS / execute）
   ├─ 失败即删除文件，不留垃圾
   └─ 成功：热加载 + （附测试时）写 <name>.tested 标记
        + 追加 logs/audit.log
        [skill_forge] name=<技能名> origin=app-space tested=<true|false>
```

`docker-compose.yml` 已把 `./app-space` 以可写方式挂进 Agent 容器
（`/agenelf/app-space:rw`）；`app/`、`scripts/` 仍是只读或不可写。
`SkillRegistry` 构造时接收 `extra_skills_dirs=[<runtime_root>/app-space/skills]`，
`discover()` 先扫主目录再扫 extra 目录，能力目录（技能清单）中快车道技能
标注 `origin: "app-space"`，内置技能标注 `origin: "app"`。

## 安全边界

1. **只读协议校验**：快车道技能与慢车道技能遵守同一份
   `SKILL_META / TOOLS / execute` 协议（含可选 `CAPABILITY_META`），
   校验失败即拒绝且不留文件。
2. **禁止同名覆盖**：主目录优先——`app/` 中的同名技能始终覆盖
   `app-space/`；`register_external_skill` 拒绝与内置技能同名，
   `skill_forge` 进一步拒绝与任何现有技能同名、拒绝 `core/*` 模块名与
   gate 保护的安全关键技能名（`server_ops`、`evolution_ops` 等）。
3. **越权写入拒绝**：`register_external_skill` 只接受 extra 目录第一个
   （app-space/skills）作为目标，传入其他任何路径一律拒绝。
4. **规模与危险模式约束**：快车道注册时套用与 gate 同族的限制——
   单文件 ≤500 行 / ≤64K 字符，并扫描 `rm -rf /`、`docker.sock`、
   `curl | sh`、硬编码密钥等危险模式。若某个 app-space 技能日后要并入
   `app/` 主目录，仍必须完整走慢车道 gate（规模限值、危险模式、
   受保护路径、安全关键模块、全量测试、候选摘要），快车道的即时校验
   不替代宿主机安全门。
5. **全程审计**：注册与移除都 best-effort 追加 `logs/audit.log`
   （`[skill_forge] ... origin=app-space`），审计失败不影响主流程。
6. **卸载保护**：`remove_forged_skill` 只移除 `origin=app-space` 的技能；
   `app/` 内置技能一律拒绝。

## 测试门禁（可选但推荐）

快车道此前只有协议校验（结构对不对），没有行为验证（逻辑对不对）。
测试门禁补上这条质量底线：`forge_skill` 可附带 `test_code`，注册表
先把技能源码与测试源码写入临时目录沙盒（技能文件名与目标一致，测试里
`import <name>` 直接可用；测试文件为 `test_<name>.py`），再用
`python -m unittest` 子进程运行（PYTHONPATH 指向临时目录，默认 60s
超时，继承当前解释器）。

- **可选，不强制**：未附测试不阻断注册，但注册结果与
  `list_forged_skills` 输出都会明确标注 `tested: false`
  （“未附测试，建议补充”）；附测试且跑通则标注 `tested: true`，
  并在 `app-space/skills/<name>.tested` 写旁车标记（remove 时一并清理）。
- **测试代码自身也有门禁**：ast 语法校验 + 规模限制（≤500 行 /
  ≤64K 字符）+ 与技能源码同族的危险模式扫描（测试是真实执行的代码，
  不能成为绕过安全底线的通道）。
- **失败行为**：测试失败/超时/语法错误一律拒绝注册，返回失败摘要
  （尾部输出截断），技能文件与标记都不落盘，临时沙盒目录自动销毁，
  不留垃圾。
- **与 gate_check 的关系**：快车道测试门禁是*即时轻量验证*——只跑
  LLM 随技能附带的单技能测试，证明“这个技能按预期工作”；慢车道
  `gate_check.sh` 是*完整门禁*——规模限值、危险模式、受保护路径、
  安全关键模块、全量测试一项不少。app-space 技能要并入 `app/` 主目录
  时仍必须完整走慢车道 gate，快车道测试不替代宿主机安全门。

```text
Agent → forge_skill(
    name="text_reverser",
    description="把输入文本按字符倒序输出",
    source_code="<完整技能源码>",
    test_code="<unittest 测试：import text_reverser 并断言 execute 行为>",
)
← {"forged": true, "tested": true, "message": "...已注册（含测试验证）..."}
```

## 使用示例

```text
用户：给自己造一个能把文本倒序输出的小技能。
Agent → forge_skill(
    name="text_reverser",
    description="把输入文本按字符倒序输出",
    source_code="<完整技能源码，含 SKILL_META/TOOLS/execute>",
)
← {"forged": true, "origin": "app-space", "message": "...已热加载，可立即通过工具调用使用。"}

Agent → list_forged_skills()
← {"origin": "app-space", "count": 1, "skills": [{"name": "text_reverser", ...}]}

Agent → text_reverser 工具               # 同一轮对话即可调用
Agent → remove_forged_skill(name="text_reverser")   # 不需要时移除并卸载
```

## 与既有机制的关系

- `register_new_skill`（慢车道开发态）行为不变，仍只写主目录。
- `core/self_optimization.py` 的参数快车道只调运行期参数，不碰代码；
  本快车道只增删技能，不调参数。两者互补。
- 快车道技能同样出现在系统提示的能力域目录中（带来源标注），
  参与工具调用的唯一约束仍是工具名全局唯一。
