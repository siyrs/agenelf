# local/ — 每位使用者自己的私有数据层

`app/` 保存所有用户共享的通用代码和功能；`local/` 保存只属于当前主人的配置与数据。升级、同步或自我迭代 `app/` 时，不会覆盖 `local/`。

运行 `make init` 会创建或迁移：

```text
local/
├── profile.yaml          # 主人基本信息与沟通风格，Agent 只读
├── preferences.yaml      # 爱好、兴趣、工作偏好，Agent 只读
├── context/              # 补充 Markdown/TXT/YAML/JSON，Agent 只读
├── servers.yaml          # 服务器别名和允许操作，不含密钥
├── secrets/              # SSH 私钥、known_hosts，仅 ops-runner 可见
└── memory/
    └── memory.json       # 脱敏长期记忆，Agent 可写
```

## Docker 可见性

| 内容 | Agent | ops-runner |
|---|---:|---:|
| profile/preferences/context | 只读 | 不挂载 |
| servers.yaml | 只读、只返回脱敏摘要 | 只读、用于执行 |
| secrets/ | **不可见** | 只读 |
| memory/ | 读写 | 不挂载 |

所有实际个性化文件都被 Git 忽略；仓库只跟踪本说明和 `*.example.*` 模板。
