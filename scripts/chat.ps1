# chat.ps1 — PowerShell CLI 入口（等价于 bash scripts/chat.sh）
# 用法：.\scripts\chat.ps1 [透传给 cli.py 的参数...]
param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot\..

try {
    # CLI 运行在独立的 cli 服务（profile=cli）：唯一挂载审批密钥的模型侧进程。
    $SkipResume = if ($env:AGENELF_SKIP_AUTO_RESUME) { $env:AGENELF_SKIP_AUTO_RESUME } else { "0" }
    docker compose --profile cli run --rm -e "AGENELF_SKIP_AUTO_RESUME=$SkipResume" cli python /agenelf/app-fork/cli.py @RemainingArgs
} finally {
    Pop-Location
}
