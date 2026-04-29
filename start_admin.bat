@echo off
cd /d "%~dp0"
set PYTHONPATH=%CD%
set "LEGACY_TEMP_DIR=%CD%\wellness_data\tmp"
if defined WELLNESS_TEMP_DIR (
    if /I "%WELLNESS_TEMP_DIR%"=="%LEGACY_TEMP_DIR%" (
        echo Ignoring legacy repo temp directory: %WELLNESS_TEMP_DIR%
        set "WELLNESS_TEMP_DIR="
    )
)
if not defined WELLNESS_TEMP_DIR (
    if defined LOCALAPPDATA (
        set WELLNESS_TEMP_DIR=%LOCALAPPDATA%\wellness-bot\tmp
    ) else (
        set WELLNESS_TEMP_DIR=%USERPROFILE%\.cache\wellness-bot\tmp
    )
)
:temp_ready
if not exist "%WELLNESS_TEMP_DIR%" mkdir "%WELLNESS_TEMP_DIR%"
set TEMP=%WELLNESS_TEMP_DIR%
set TMP=%WELLNESS_TEMP_DIR%
set TMPDIR=%WELLNESS_TEMP_DIR%
start "" http://127.0.0.1:8200/
python -m app.interfaces.admin.server --host 127.0.0.1 --port 8200
pause
