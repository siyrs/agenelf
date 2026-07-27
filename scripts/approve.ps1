param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$RequestId,
    [Parameter(Position = 1)]
    [ValidateSet("approve", "deny")]
    [string]$Action = "approve",
    [Parameter(Position = 2)]
    [string]$Reason = "",
    [string]$As = $env:USERNAME
)

$ScriptPath = Join-Path $PSScriptRoot "approve.py"
$Py = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $Py) {
    & $Py.Source -3 $ScriptPath $RequestId $Action $Reason --as $As
    exit $LASTEXITCODE
}
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $Python) {
    Write-Error "未找到 py 或 python。也可以直接在 Agenelf CLI 输入 /approve <op-id>。"
    exit 2
}
& $Python.Source $ScriptPath $RequestId $Action $Reason --as $As
exit $LASTEXITCODE
