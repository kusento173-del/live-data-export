@echo off
setlocal
title Live Data Export
cd /d "%~dp0"

for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "(Get-Date).AddDays(-1).ToString('yyyy-MM-dd')"`) do set "DEFAULT_DATE=%%D"
echo.
set "START_DATE="
set /p "START_DATE=Start date, press Enter for yesterday [%DEFAULT_DATE%]: "
if not defined START_DATE set "START_DATE=%DEFAULT_DATE%"
set "END_DATE="
set /p "END_DATE=End date, press Enter for the same day [%START_DATE%]: "
if not defined END_DATE set "END_DATE=%START_DATE%"
set "REQUESTED_START_DATE=%START_DATE%"
set "REQUESTED_END_DATE=%END_DATE%"

powershell -NoProfile -Command "$start=[datetime]::MinValue; $end=[datetime]::MinValue; $culture=[Globalization.CultureInfo]::InvariantCulture; $style=[Globalization.DateTimeStyles]::None; if(-not [datetime]::TryParseExact($env:REQUESTED_START_DATE,'yyyy-MM-dd',$culture,$style,[ref]$start) -or -not [datetime]::TryParseExact($env:REQUESTED_END_DATE,'yyyy-MM-dd',$culture,$style,[ref]$end) -or $end -lt $start){exit 1}"
if errorlevel 1 (
    echo.
    echo Invalid date range. Use YYYY-MM-DD and make sure the end date is not earlier than the start date.
    pause
    exit /b 1
)

echo.
echo Export range: %REQUESTED_START_DATE% to %REQUESTED_END_DATE%
echo Stage 1 creates daily/session data and monthly summaries.
echo Stage 2 continues minute/hourly data and refreshes the monthly workbook.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\app\run_range.ps1" -StartDate "%REQUESTED_START_DATE%" -EndDate "%REQUESTED_END_DATE%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Export completed.
) else (
    echo Export was not fully successful. Review the errors above.
)
pause
exit /b %EXIT_CODE%
