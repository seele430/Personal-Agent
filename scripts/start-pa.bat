@echo off
rem PA backend autostart: api(:8000) + scheduler + outbox, hidden window.
rem Skips when :8000 is already listening.

setlocal
set "PA_DIR=C:\Users\123\Personal Agent"
set "LOG=%PA_DIR%\data\start-pa.log"

echo %date% %time% start-pa begin>>"%LOG%"

netstat -ano | findstr "LISTENING" | findstr ":8000" >nul
if not errorlevel 1 goto already

start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PA_DIR%\scripts\run-dev.ps1" -NoFrontend
echo %date% %time% launched run-dev -NoFrontend>>"%LOG%"
exit /b 0

:already
echo %date% %time% API 8000 already listening, nothing to do>>"%LOG%"
exit /b 0
