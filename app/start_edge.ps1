param(
    [ValidateSet("xinxin", "zhongding")]
    [string]$Backend = "xinxin"
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $appDir
$config = Get-Content -LiteralPath (Join-Path $appDir "browser_config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$settings = $config.backends.PSObject.Properties[$Backend].Value
if (-not $settings) {
    throw "Browser configuration not found: $Backend"
}
$displayName = [string]$settings.display_name

$cdp = [uri]([string]$settings.cdp_url)
$userDataDir = [System.IO.Path]::GetFullPath((Join-Path $projectDir ([string]$settings.user_data_dir)))

function Test-DebugPort {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($cdp.AbsoluteUri.TrimEnd("/") + "/json/version") -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-DebugPort) {
    Write-Host "$displayName Edge is ready: $($cdp.AbsoluteUri)"
    exit 0
}

$edge = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $edge) {
    throw "Cannot find Microsoft Edge."
}

New-Item -ItemType Directory -Force -Path $userDataDir | Out-Null
Start-Process -FilePath $edge -ArgumentList @(
    "--remote-debugging-port=$($cdp.Port)",
    "--user-data-dir=$userDataDir",
    [string]$settings.page_url
)
Start-Sleep -Seconds 8

if (-not (Test-DebugPort)) {
    throw "$displayName Edge debug port is not ready: $($cdp.Port)"
}

Write-Host "$displayName Edge is ready: $($cdp.AbsoluteUri)"
