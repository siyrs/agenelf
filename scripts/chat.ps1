# chat.ps1 — PowerShell CLI 入口（等价于 bash scripts/chat.sh）
# 用法：.\scripts\chat.ps1 [透传给 cli.py 的参数...]
param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot\..

try {
    docker compose exec agenelf python /agenelf/app-fork/cli.py @RemainingArgs
} finally {
    Pop-Location
}
