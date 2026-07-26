param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Root "app"
$Destination = Join-Path $Root "app-fork"

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "[sync_fork] 源目录不存在：$Source"
}

New-Item -ItemType Directory -Path $Destination -Force | Out-Null
if (-not $Quiet) {
    Write-Host "[sync_fork] 同步 $Source -> $Destination（镜像复制，排除 __pycache__ 和 *.pyc）"
}

$Arguments = @(
    $Source,
    $Destination,
    "/MIR",
    "/XD", "__pycache__",
    "/XF", "*.pyc",
    "/R:2",
    "/W:1",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP"
)
& robocopy @Arguments | Out-Null
$Code = $LASTEXITCODE
# Robocopy uses 0-7 for success (including copied/extra/mismatch metadata) and
# 8+ for real failures.
if ($Code -ge 8) {
    throw "[sync_fork] robocopy 失败，退出码：$Code"
}

if (-not $Quiet) {
    Write-Host "[sync_fork] 同步完成（robocopy=$Code）"
}
exit 0
