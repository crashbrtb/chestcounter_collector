@echo off
REM ==============================================================================
REM  Diagnóstico rápido de ambiente (RapidOCR, Python, Tesseract)
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
