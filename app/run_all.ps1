param(
    [datetime]$Date = (Get-Date).AddDays(-1),
    [int]$MinuteIntervalMs = 150,
    [switch]$SkipMonthlySummary
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $appDir
$config = Get-Content -LiteralPath (Join-Path $appDir "browser_config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$dateText = $Date.ToString("yyyy-MM-dd")
$monthText = $Date.ToString("yyyy-MM")
$failures = @()
$summaryFailures = @()

function Invoke-Python {
    param([string[]]$Arguments)
    $pythonArguments = @("-u") + $Arguments
    $process = Start-Process -FilePath "python" -ArgumentList $pythonArguments `
        -WorkingDirectory $projectDir -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

foreach ($backend in @("xinxin", "zhongding")) {
    & (Join-Path $appDir "start_edge.ps1") -Backend $backend
}

$Host.UI.RawUI.WindowTitle = "Live Data Export - Stage 1"
Write-Host ""
Write-Host "========== Stage 1/2: daily and session data ==========" -ForegroundColor Yellow
foreach ($key in @("xinxin", "zhongding")) {
    $backend = [string]$config.backends.$key.display_name
    Write-Host ""
    Write-Host "===== $backend / $dateText / daily and sessions =====" -ForegroundColor Cyan
    $code = Invoke-Python -Arguments @(
        "-m", "app.export",
        "--backend-name", $backend,
        "--date", $dateText,
        "--phase", "summary"
    )
    if ($code -ne 0) {
        $summaryFailures += "$backend daily/session (exit $code)"
    }
}

if (-not $SkipMonthlySummary) {
    Write-Host ""
    Write-Host "===== Refresh monthly daily/session summary / $monthText =====" -ForegroundColor Cyan
    $summaryCode = Invoke-Python -Arguments @("-m", "app.combine_exports", "--month", $monthText)
    if ($summaryCode -ne 0) {
        $summaryFailures += "monthly daily/session summary (exit $summaryCode)"
    }
}
$failures += $summaryFailures

Write-Host ""
if ($summaryFailures.Count -eq 0) {
    Write-Host "************************************************************" -ForegroundColor Green
    if ($SkipMonthlySummary) {
        Write-Host "  Daily and session data are ready." -ForegroundColor Green
    } else {
        Write-Host "  Daily and session data are ready. Monthly summaries updated." -ForegroundColor Green
    }
    Write-Host "  Minute and hourly data will continue in the same window." -ForegroundColor Green
    Write-Host "************************************************************" -ForegroundColor Green
} else {
    Write-Host "************************************************************" -ForegroundColor Yellow
    Write-Host "  Daily and session summaries were generated with some failures." -ForegroundColor Yellow
    Write-Host "  Completed data is available; minute processing will continue." -ForegroundColor Yellow
    Write-Host "************************************************************" -ForegroundColor Yellow
}
try { [Console]::Beep(1000, 300) } catch {}

$Host.UI.RawUI.WindowTitle = "Live Data Export - Stage 2"
Write-Host ""
Write-Host "========== Stage 2/2: minute and hourly data ==========" -ForegroundColor Yellow
foreach ($key in @("xinxin", "zhongding")) {
    $backend = [string]$config.backends.$key.display_name
    Write-Host ""
    Write-Host "===== $backend / $dateText / minute and hourly =====" -ForegroundColor Cyan
    $code = Invoke-Python -Arguments @(
        "-m", "app.export",
        "--backend-name", $backend,
        "--date", $dateText,
        "--phase", "minutes",
        "--minute-interval-ms", [string]$MinuteIntervalMs
    )
    if ($code -ne 0) {
        $failures += "$backend minute/hourly (exit $code)"
    }
}

if (-not $SkipMonthlySummary) {
    Write-Host ""
    Write-Host "===== Refresh complete monthly summary / $monthText =====" -ForegroundColor Cyan
    $summaryCode = Invoke-Python -Arguments @("-m", "app.combine_exports", "--month", $monthText)
    if ($summaryCode -ne 0) {
        $failures += "complete monthly summary (exit $summaryCode)"
    }
}

if ($failures.Count -gt 0) {
    throw "Some steps failed: $($failures -join ', '). Completed data and summaries were preserved for retry."
}

$Host.UI.RawUI.WindowTitle = "Live Data Export - Complete"
try { [Console]::Beep(1200, 500) } catch {}
Write-Host ""
if ($SkipMonthlySummary) {
    Write-Host "Done: daily, sessions, minute and hourly data are updated. Monthly summary is deferred." -ForegroundColor Green
} else {
    Write-Host "Done: daily, sessions, minute, hourly and all monthly summaries are updated." -ForegroundColor Green
}
