@echo off
setlocal
set PORT=8110
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
  echo Stopping PID %%p on port %PORT%...
  taskkill /PID %%p /F >nul 2>&1
)
echo Done.
