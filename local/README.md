# local/ — 每位使用者自己的私有数据层

`app/` 保存所有用户共享的通用代码和功能；`local/` 保存只属于当前主人的配置、记忆和成长连续性。升级、同步或自我迭代 `app/` 时，不会覆盖 `local/`。

运行 `make init` 会创建或迁移：

```text
local/
├── profile.yaml          # 主人基本信息与沟通风格，Agent 只读
├── preferences.yaml      # 爱好、兴趣、工作偏好，Agent 只读
├── context/              # 补充 Markdown/TXT/YAML/JSON，Agent 只读
├── servers.yaml          # 服务器别名和允许操作，不含密钥
├── validation.yaml       # HTTP/TCP 检查别名、断言和套件，不含凭据
├── secrets/              # SSH 私钥、known_hosts，仅 ops-runner 可见
├── memory/
│   └── memory.json       # 脱敏长期记忆，Agent 可写
└── self/
    ├── state.json        # 连续性 ID、沉淀游标和操作性自我定义
    ├── reflections.json  # 有界、脱敏的反思与教训
    └── intentions.json   # 改进意向、优先级、验收条件和生命周期
```

`local/self/` 中的“自我认知、意愿、意向”是软件状态：它让 Agenelf 跨会话记住自己的能力、限制、教训和下一步，但不代表主观意识、情感或自由意志。

## Docker 可见性

| 内容 | Agent | ops-runner | validation-runner |
|---|---:|---:|---:|
| profile/preferences/context | 只读 | 不挂载 | 不挂载 |
| servers.yaml | 只读、只返回脱敏摘要 | 只读、用于执行 | 不挂载 |
| validation.yaml | 只读、只返回别名摘要 | 不挂载 | 只读、用于检查 |
| secrets/ | **不可见** | 只读 | **不可见** |
| memory/ | 读写 | 不挂载 | 不挂载 |
| self/ | 读写 | 不挂载 | 不挂载 |

所有实际个性化文件和成长记录都被 Git 忽略；仓库只跟踪本说明和 `*.example.*` 模板。
