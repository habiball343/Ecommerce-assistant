param([switch]$SkipInstall)

$ErrorActionPreference = "Stop"
$processPath = $env:Path
Remove-Item Env:Path -ErrorAction SilentlyContinue
$env:Path = $processPath
$serviceRoot = (Resolve-Path $PSScriptRoot).Path
Set-Location $serviceRoot
if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}
if (-not $SkipInstall) { npm install }

$runtimeDir = Join-Path $serviceRoot ".runtime"
$logsDir = Join-Path $serviceRoot "logs"
New-Item -ItemType Directory -Force -Path $runtimeDir, $logsDir | Out-Null
$pidFile = Join-Path $runtimeDir "pid.json"
if (Test-Path -LiteralPath $pidFile) {
    $old = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
    if (Get-Process -Id $old.pid -ErrorAction SilentlyContinue) {
        Write-Host "User frontend is already running (PID $($old.pid))."
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force
}

$nodePath = (Get-Command node).Source
$vitePath = Join-Path $serviceRoot "node_modules\vite\bin\vite.js"
$process = Start-Process -WindowStyle Hidden -FilePath $nodePath `
    -ArgumentList @($vitePath, "--host", "0.0.0.0", "--port", "5173") `
    -WorkingDirectory $serviceRoot -RedirectStandardOutput (Join-Path $logsDir "frontend.out.log") `
    -RedirectStandardError (Join-Path $logsDir "frontend.err.log") -PassThru
@{ name = "user-frontend"; pid = $process.Id } | ConvertTo-Json | `
    Set-Content -Encoding UTF8 -LiteralPath $pidFile
Write-Host "User frontend started: http://127.0.0.1:5173"
