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
set "WINPY_URL=https://github.com/winpython/winpython/releases/download/15.3.20250425final/Winpython64-3.12.10.0dot.zip"

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
REM  The dependencies decide the floor: Pillow and mysql-connector-python both
REM  require 3.10 or newer. A Python below that used to get this far, build a
REM  venv, and only fail pages later inside pip - so the version is checked
REM  here, where the message can still say what is wrong.
set "SYS_PYTHON="
set "SYS_PYVER="
where python >nul 2>nul
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "SYS_PYTHON=%%i"
    for /f "tokens=*" %%i in ('python -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "SYS_PYVER=%%i"
)

if defined SYS_PYTHON (
    python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [WARN] System Python is %SYS_PYVER%, and 3.10 or newer is required.
        echo        Falling back to the portable copy.
        set "SYS_PYTHON="
    )
)

if defined SYS_PYTHON (
    echo [OK] System Python: %SYS_PYTHON% ^(%SYS_PYVER%^)
    python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,15) else 1)" >nul 2>nul
    if not errorlevel 1 (
        echo [WARN] Python %SYS_PYVER% is newer than anything this has been tried on.
        echo        The OCR engine needs an 'onnxruntime' build for it; if the install
        echo        below fails, use Python 3.14.
    )
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
