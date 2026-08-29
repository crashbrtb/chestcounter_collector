@echo off
REM **Get Date and Time for log file name**
for /f "tokens=2 delims==" %%a in ('wmic OS Get LocalDateTime /VALUE ^| findstr LocalDateTime') do set "datahora=%%a"
set "data=%datahora:~0,4%-%datahora:~4,2%-%datahora:~6,2%"
set "hora=%datahora:~8,2%-%datahora:~10,2%-%datahora:~12,2%"
set "logfile=C:\chestcounter\execution_logs\log_%data%_%hora%.txt"

REM **Define Python Path for VENV (Important!)**
set "VENV_PYTHON_EXE=C:\chestcounter\venv\Scripts\python.exe"

REM **Enter relative path**
echo Entering directory C:\chestcounter >> "%logfile%"
cd /d C:\chestcounter

REM **Execute script using the VENV interpreter**
echo Starting script counter.py using VENV >> "%logfile%"

:: O 'call activate.bat' não é necessário se você chamar o executável diretamente.
:: Vamos chamar o Python do VENV diretamente e redirecionar a saída e erros para o log.
%VENV_PYTHON_EXE% C:\chestcounter\counter.py 1>>"%logfile%" 2>>&1

echo Script execution finished. >> "%logfile%"