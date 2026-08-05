param(
    [string]$StartDate = ((Get-Date).AddDays(-1).ToString("yyyy-MM-dd")),
    [string]$EndDate = "",
    [int]$MinuteIntervalMs = 150
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $appDir
$runAll = Join-Path $appDir "run_all.ps1"
$culture = [Globalization.CultureInfo]::InvariantCulture
$style = [Globalization.DateTimeStyles]::None
$firstDate = [datetime]::MinValue
$lastDate = [datetime]::MinValue

if (-not [datetime]::TryParseExact($StartDate.Trim(), "yyyy-MM-dd", $culture, $style, [ref]$firstDate)) {
    Write-Host "Invalid start date '$StartDate'. Use YYYY-MM-DD." -ForegroundColor Red
    exit 1
}
if ([string]::IsNullOrWhiteSpace($EndDate)) {
    $lastDate = $firstDate
} elseif (-not [datetime]::TryParseExact($EndDate.Trim(), "yyyy-MM-dd", $culture, $style, [ref]$lastDate)) {
    Write-Host "Invalid end date '$EndDate'. Use YYYY-MM-DD." -ForegroundColor Red
    exit 1
}
if ($lastDate.Date -lt $firstDate.Date) {
    Write-Host "Invalid date range: start $($firstDate.ToString('yyyy-MM-dd')), end $($lastDate.ToString('yyyy-MM-dd'))." -ForegroundColor Red
    Write-Host "The end date must be the same as or later than the start date." -ForegroundColor Red
    exit 1
}

$firstDate = $firstDate.Date
$lastDate = $lastDate.Date

$dates = @()
for ($current = $firstDate; $current -le $lastDate; $current = $current.AddDays(1)) {
    $dates += $current
}

if ($dates.Count -eq 1) {
    & $runAll -Date $firstDate -MinuteIntervalMs $MinuteIntervalMs
    exit
}

$failures = @()
foreach ($current in $dates) {
    $dateText = $current.ToString("yyyy-MM-dd")
    $Host.UI.RawUI.WindowTitle = "Live Data Export - $dateText"
    Write-Host ""
    Write-Host "################################################################" -ForegroundColor Magenta
    Write-Host "  Export day $dateText ($($dates.IndexOf($current) + 1)/$($dates.Count))" -ForegroundColor Magenta
    Write-Host "################################################################" -ForegroundColor Magenta
    try {
        & $runAll -Date $current -MinuteIntervalMs $MinuteIntervalMs -SkipMonthlySummary
    } catch {
        $failures += "$dateText`: $($_.Exception.Message)"
        Write-Host "Day $dateText failed; saved data will be reused on retry." -ForegroundColor Red
    }
}

$months = $dates | ForEach-Object { $_.ToString("yyyy-MM") } | Select-Object -Unique
foreach ($month in $months) {
    Write-Host ""
    Write-Host "===== Refresh monthly summary / $month =====" -ForegroundColor Cyan
    $arguments = @("-u", "-m", "app.combine_exports", "--month", $month)
    $process = Start-Process -FilePath "python" -ArgumentList $arguments `
        -WorkingDirectory $projectDir -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        $failures += "$month monthly summary (exit $($process.ExitCode))"
    }
}

if ($failures.Count -gt 0) {
    throw "Some dates failed: $($failures -join '; ')"
}

$Host.UI.RawUI.WindowTitle = "Live Data Export - Complete"
Write-Host ""
Write-Host "Date range completed: $($firstDate.ToString('yyyy-MM-dd')) to $($lastDate.ToString('yyyy-MM-dd'))." -ForegroundColor Green
