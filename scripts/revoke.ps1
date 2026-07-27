param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$RequestId,
    [Parameter(Position = 1)]
    [string]$Reason = "",
    [string]$As = $env:USERNAME
)

$ScriptPath = Join-Path $PSScriptRoot "revoke.py"
$Py = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $Py) {
    & $Py.Source -3 $ScriptPath $RequestId $Reason --as $As
    exit $LASTEXITCODE
}
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $Python) {
    Write-Error "未找到 py 或 python，无法执行主人撤销命令。"
    exit 2
}
& $Python.Source $ScriptPath $RequestId $Reason --as $As
exit $LASTEXITCODE
