@echo off
REM ==============================================================================
REM  Total Battle Chest Collector - single entry point
REM ==============================================================================
REM
REM  This is the only file Windows Task Scheduler needs to call.
REM  It does not hold any configuration: everything (accounts, profiles, timings,
REM  browser, OCR, log level and retention) is in config\config.json and is edited
REM  via the graphical interface -- run.bat config
REM
REM  Logging is NOT redirected from here. Python writes directly to
REM  execution_logs\collector_YYYY-MM-DD.log in UTF-8, cleans up older logs
REM  automatically, and records in-process errors. Redirecting batch output
REM  corrupted unicode characters and truncated logs if the process died.
REM
REM  Usage:
REM      run.bat                 collects chests (what Task Scheduler should call)
REM      run.bat config          opens the configuration interface
REM      run.bat calibrate       opens the calibration wizard directly
REM      run.bat check           validates configuration and calibration only
REM
REM  Exit codes (seen in Task Scheduler's "Last Run Result"):
REM      0 = all collected    1 = failure    2 = configuration/calibration issue
REM      3 = cancelled
REM ==============================================================================

setlocal
cd /d "%~dp0"

if not exist "execution_logs" mkdir "execution_logs"

REM --- Python Interpreter: project venv, otherwise system Python ---------------
set "PYTHON_EXE=python"
set "PYTHONW_EXE=pythonw"
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
    set "PYTHONW_EXE=%~dp0venv\Scripts\pythonw.exe"
)

REM --- Mode -------------------------------------------------------------------
set "MODE=%~1"

if /i "%MODE%"=="config"    goto :gui
if /i "%MODE%"=="gui"       goto :gui
if /i "%MODE%"=="calibrate" goto :calibrate
if /i "%MODE%"=="calibrar"  goto :calibrate
if /i "%MODE%"=="check"     goto :check
if /i "%MODE%"=="verificar" goto :check
goto :collect

:gui
call :check_interface
if errorlevel 1 exit /b 2
REM pythonw: the interface doesn't need a console window behind it.
start "" "%PYTHONW_EXE%" "%~dp0main.py" --gui
exit /b 0

:calibrate
call :check_interface
if errorlevel 1 exit /b 2
start "" "%PYTHONW_EXE%" "%~dp0main.py" --calibrate
exit /b 0

:check_interface
REM "start" is fire-and-forget: if Python crashes on launch, the double-click
REM window closes instantly leaving nothing on screen. This import check takes
REM a second and replaces silence with a clear error message.
"%PYTHON_EXE%" -c "import gui.app" 2>"%~dp0execution_logs\startup.log"
if errorlevel 1 (
    echo.
    echo  [ERROR] The graphical interface could not be opened.
    echo.
    echo  Probable cause: missing dependencies or Python is not installed.
    echo  Run install.bat once and try again.
    echo.
    echo  Technical details in: execution_logs\startup.log
    echo.
    pause
    exit /b 1
)
exit /b 0

:check
"%PYTHON_EXE%" "%~dp0main.py" --check
exit /b %ERRORLEVEL%

:collect
REM Pre-logger failures (missing Python, missing dependency) have nowhere else
REM to be seen; that is the only reason startup.log exists.
"%PYTHON_EXE%" "%~dp0main.py" 2>>"%~dp0execution_logs\startup.log"
exit /b %ERRORLEVEL%
