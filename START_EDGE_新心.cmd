@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\app\start_edge.ps1" -Backend "xinxin"
if errorlevel 1 pause
