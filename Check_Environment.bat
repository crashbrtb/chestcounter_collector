@echo off
REM ==============================================================================
REM  Quick environment diagnostics (Python and its libraries)
REM ==============================================================================
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
)

"%PYTHON_EXE%" "%~dp0utils\check_env.py"
echo.
pause
