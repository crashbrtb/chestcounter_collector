@echo off
REM ==============================================================================
REM  Total Battle Chest Collector - ponto de entrada unico
REM ==============================================================================
REM
REM  Este e o unico arquivo que o Agendador de Tarefas do Windows precisa chamar.
REM  Ele nao guarda configuracao nenhuma: tudo (contas, perfis, tempos, navegador,
REM  OCR, nivel e retencao de log) esta em config\config.json e se edita pela
REM  interface -- run.bat config
REM
REM  O log NAO e mais um redirecionamento daqui. O proprio Python escreve
REM  execution_logs\collector_AAAA-MM-DD.log em UTF-8, apaga os antigos sozinho e
REM  registra ate as falhas que acontecem dentro do processo. Redirecionar a saida
REM  do .bat perdia acentos e cortava o log quando o processo morria.
REM
REM  Uso:
REM      run.bat                 coleta os baus (e o que o agendador deve chamar)
REM      run.bat config          abre a interface de configuracao
REM      run.bat calibrar        abre direto o assistente de calibracao
REM      run.bat verificar       so confere configuracao e calibracao
REM
REM  Codigo de saida (o agendador enxerga em "Ultimo resultado da execucao"):
REM      0 = tudo coletado    1 = alguma falha    2 = configuracao/calibracao
REM ==============================================================================

setlocal
cd /d "%~dp0"

if not exist "execution_logs" mkdir "execution_logs"

REM --- Interpretador: venv do projeto, senao o Python do sistema ---------------
set "PYTHON_EXE=python"
set "PYTHONW_EXE=pythonw"
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
    set "PYTHONW_EXE=%~dp0venv\Scripts\pythonw.exe"
)

REM --- Modo -------------------------------------------------------------------
set "MODE=%~1"

if /i "%MODE%"=="config"    goto :gui
if /i "%MODE%"=="gui"       goto :gui
if /i "%MODE%"=="calibrar"  goto :calibrate
if /i "%MODE%"=="verificar" goto :check
goto :collect

:gui
call :check_interface
if errorlevel 1 exit /b 2
REM pythonw: a interface nao precisa de janela de console atras dela.
start "" "%PYTHONW_EXE%" "%~dp0main.py" --gui
exit /b 0

:calibrate
call :check_interface
if errorlevel 1 exit /b 2
start "" "%PYTHONW_EXE%" "%~dp0main.py" --calibrate
exit /b 0

:check_interface
REM O "start" e disparar e esquecer: se o Python morrer ao abrir a interface, a
REM janela do duplo clique fecha e nao sobra nada na tela. Este teste de import
REM custa um segundo e troca esse silencio por uma mensagem.
"%PYTHON_EXE%" -c "import gui.app" 2>"%~dp0execution_logs\startup.log"
if errorlevel 1 (
    echo.
    echo  [ERRO] A interface nao pode ser aberta.
    echo.
    echo  Causa provavel: dependencias faltando ou Python nao instalado.
    echo  Rode o install.bat uma vez e tente de novo.
    echo.
    echo  Detalhes tecnicos em: execution_logs\startup.log
    echo.
    pause
    exit /b 1
)
exit /b 0

:check
"%PYTHON_EXE%" "%~dp0main.py" --check
exit /b %ERRORLEVEL%

:collect
REM Falhas anteriores ao logger (Python ausente, dependencia faltando) nao teriam
REM onde aparecer: e so para isso que existe o startup.log.
"%PYTHON_EXE%" "%~dp0main.py" 2>>"%~dp0execution_logs\startup.log"
exit /b %ERRORLEVEL%
