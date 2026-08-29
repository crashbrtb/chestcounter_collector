@echo off
setlocal enabledelayedexpansion

REM **Get Current Directory**
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM **Ensure execution_logs directory exists**
if not exist "execution_logs" mkdir "execution_logs"

REM **Get Date and Time for log file name**
for /f "tokens=2 delims==" %%a in ('wmic OS Get LocalDateTime /VALUE ^| findstr LocalDateTime') do set "datahora=%%a"
set "data=%datahora:~0,4%-%datahora:~4,2%-%datahora:~6,2%"
set "hora=%datahora:~8,2%-%datahora:~10,2%-%datahora:~12,2%"
set "logfile=%SCRIPT_DIR%execution_logs\log_%data%_%hora%.txt"

REM **Check for Python in venv or system**
set "PYTHON_EXE=python"
if exist "%SCRIPT_DIR%venv\Scripts\python.exe" (
    set "PYTHON_EXE=%SCRIPT_DIR%venv\Scripts\python.exe"
) else if exist "C:\chestcounter\venv\Scripts\python.exe" (
    set "PYTHON_EXE=C:\chestcounter\venv\Scripts\python.exe"
)

echo [INFO] Starting Total Battle Automation with %PYTHON_EXE% >> "%logfile%"
echo [INFO] Working Directory: %SCRIPT_DIR% >> "%logfile%"

%PYTHON_EXE% "%SCRIPT_DIR%main.py" 1>>"%logfile%" 2>>&1

echo [INFO] Script execution finished. >> "%logfile%"
endlocal