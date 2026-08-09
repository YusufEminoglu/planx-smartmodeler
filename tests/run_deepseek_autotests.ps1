[CmdletBinding()]
param(
    [int]$MatrixRuns = 2,
    [int]$MatrixLimit = 10,
    [int]$HardSeed = 303
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:SMARTMODELER_DEEPSEEK_API_KEY) -and
    [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    throw "A DeepSeek API key must already be present in this PowerShell session."
}
if ($MatrixRuns -lt 1 -or $MatrixRuns -gt 20) {
    throw "MatrixRuns must be between 1 and 20."
}
if ($MatrixLimit -lt 2 -or $MatrixLimit -gt 30) {
    throw "MatrixLimit must be between 2 and 30."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$qgisPython = "C:\OSGeo4W\bin\python-qgis-ltr.bat"
if (-not (Test-Path -LiteralPath $qgisPython)) {
    throw "QGIS LTR runtime was not found at $qgisPython."
}

$logDirectory = Join-Path $repoRoot "scratch"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDirectory "deepseek_autotests_$stamp.log"
$env:QGIS_PLUGINPATH = $repoRoot

$results = [System.Collections.Generic.List[object]]::new()

function Invoke-QgisLiveTest {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ScriptPath
    )

    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    Add-Content -LiteralPath $logPath -Value "`n=== $Name ==="
    & $qgisPython $ScriptPath 2>&1 | Tee-Object -FilePath $logPath -Append
    $exitCode = $LASTEXITCODE
    $passed = ($exitCode -eq 0)
    $results.Add([pscustomobject]@{ Name = $Name; Passed = $passed; ExitCode = $exitCode })
    if ($passed) {
        Write-Host "$($Name): PASS" -ForegroundColor Green
    } else {
        Write-Host "$($Name): FAIL (exit $exitCode)" -ForegroundColor Red
    }
}

try {
    $matrixPath = Join-Path $repoRoot "planx_smartmodeler\tests\qgis_deepseek_matrix.py"
    $hardPath = Join-Path $repoRoot "planx_smartmodeler\tests\qgis_deepseek_hard_live.py"
    $env:SMARTMODELER_DEEPSEEK_MATRIX_LIMIT = [string]$MatrixLimit

    for ($run = 1; $run -le $MatrixRuns; $run++) {
        $env:SMARTMODELER_DEEPSEEK_MATRIX_SEED = [string](1000 + $run * 7919)
        Invoke-QgisLiveTest "DeepSeek randomized matrix $run/$MatrixRuns" $matrixPath
    }

    $env:SMARTMODELER_DEEPSEEK_HARD_SEED = [string]$HardSeed
    Invoke-QgisLiveTest "DeepSeek hard OSM workflow" $hardPath
} finally {
    Remove-Item Env:SMARTMODELER_DEEPSEEK_MATRIX_LIMIT -ErrorAction SilentlyContinue
    Remove-Item Env:SMARTMODELER_DEEPSEEK_MATRIX_SEED -ErrorAction SilentlyContinue
    Remove-Item Env:SMARTMODELER_DEEPSEEK_HARD_SEED -ErrorAction SilentlyContinue
}

$failed = @($results | Where-Object { -not $_.Passed })
Write-Host "`n=== DeepSeek automatic test summary ===" -ForegroundColor Cyan
$results | Format-Table -AutoSize
Write-Host "Log: $logPath"
Write-Host "Passed: $($results.Count - $failed.Count) / $($results.Count)"

if ($failed.Count -gt 0) {
    exit 1
}
exit 0
