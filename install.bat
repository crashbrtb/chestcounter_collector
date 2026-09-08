@echo off
setlocal enabledelayedexpansion

REM ==============================================================================
REM  Total Battle Chest Collector - environment setup
REM ==============================================================================

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_DIR=%SCRIPT_DIR%venv"
set "WINPY_DIR=%SCRIPT_DIR%python"
set "WINPY_ZIP=%SCRIPT_DIR%winpython.zip"
set "WINPY_URL=https://github.com/winpython/winpython/releases/download/19.1.20260805final/Winpython64-3.14.7.0dot.zip"

echo.
echo ==============================================================================
echo     Total Battle Chest Collector - Setup
echo ==============================================================================
echo.

if not exist "%SCRIPT_DIR%execution_logs" (
    mkdir "%SCRIPT_DIR%execution_logs"
    echo [OK] 'execution_logs' folder created.
)
if not exist "%SCRIPT_DIR%config" mkdir "%SCRIPT_DIR%config"

REM --- 1. Python ----------------------------------------------------------------
set "SYS_PYTHON="
where python >nul 2>nul
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "SYS_PYTHON=%%i"
)

if defined SYS_PYTHON (
    echo [OK] System Python: %SYS_PYTHON%
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
) else (
    echo [INFO] Python not found. Downloading portable WinPython...
    if not exist "%WINPY_DIR%\python.exe" (
        echo   [1/3] downloading...
        curl -L -o "%WINPY_ZIP%" "%WINPY_URL%"
        echo   [2/3] extracting...
        if not exist "%WINPY_DIR%" mkdir "%WINPY_DIR%"
        tar -xf "%WINPY_ZIP%" --strip-components=2 -C "%WINPY_DIR%"
        echo   [3/3] cleaning up...
        if exist "%WINPY_ZIP%" del "%WINPY_ZIP%"
    )
    echo Creating virtual environment from WinPython...
    "%WINPY_DIR%\python.exe" -m venv "%VENV_DIR%"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

REM --- 2. Dependencies ----------------------------------------------------------
echo.
echo === Upgrading pip ===
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip --no-warn-script-location

echo.
echo === Installing dependencies ===
echo     (RapidOCR downloads ~15 MB of models on first reading)
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

REM --- 3. Initial Configuration -------------------------------------------------
echo.
echo === Generating initial configuration ===
REM Creates config\config.json with default settings, and imports database credentials
REM if a legacy position.cfg file exists.
"%VENV_DIR%\Scripts\python.exe" "%SCRIPT_DIR%main.py" --check

echo.
echo ==============================================================================
echo  [COMPLETE]
echo.
echo  Next steps (double-click, no command line needed):
echo    1) Configure.bat (or Configurar.bat) - set up accounts, profiles, and databases
echo    2) Click "1 · Open game in Chrome", log in and navigate to the clan screen
echo    3) Click "2 · Calibrate" (or Calibrate.bat) - mark controls on the capture
echo    4) Click "▶ Run collection now" to test
echo    5) Point Windows Task Scheduler to run.bat
echo ==============================================================================
echo.
pause
endlocal
