$ErrorActionPreference = "SilentlyContinue"
$serviceRoot = (Resolve-Path $PSScriptRoot).Path
$pidFile = Join-Path $serviceRoot ".runtime\pid.json"
if (Test-Path -LiteralPath $pidFile) {
    $entry = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
    Stop-Process -Id $entry.pid -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host "Stopped user frontend ($($entry.pid))"
}
