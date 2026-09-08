@echo off
setlocal enabledelayedexpansion

REM ==============================================================================
REM  Total Battle Chest Collector - instalacao do ambiente
REM ==============================================================================

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_DIR=%SCRIPT_DIR%venv"
set "WINPY_DIR=%SCRIPT_DIR%python"
set "WINPY_ZIP=%SCRIPT_DIR%winpython.zip"
set "WINPY_URL=https://github.com/winpython/winpython/releases/download/15.3.20250425final/Winpython64-3.12.10.0dot.zip"

echo.
echo ==============================================================================
echo     Total Battle Chest Collector - instalacao
echo ==============================================================================
echo.

if not exist "%SCRIPT_DIR%execution_logs" (
    mkdir "%SCRIPT_DIR%execution_logs"
    echo [OK] Pasta 'execution_logs' criada.
)
if not exist "%SCRIPT_DIR%config" mkdir "%SCRIPT_DIR%config"

REM --- 1. Python ----------------------------------------------------------------
set "SYS_PYTHON="
where python >nul 2>nul
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "SYS_PYTHON=%%i"
)

if defined SYS_PYTHON (
    echo [OK] Python do sistema: %SYS_PYTHON%
    echo Criando ambiente virtual...
    python -m venv "%VENV_DIR%"
) else (
    echo [INFO] Python nao encontrado. Baixando WinPython portatil...
    if not exist "%WINPY_DIR%\python.exe" (
        echo   [1/3] baixando...
        curl -L -o "%WINPY_ZIP%" "%WINPY_URL%"
        echo   [2/3] extraindo...
        if not exist "%WINPY_DIR%" mkdir "%WINPY_DIR%"
        tar -xf "%WINPY_ZIP%" --strip-components=2 -C "%WINPY_DIR%"
        echo   [3/3] limpando...
        if exist "%WINPY_ZIP%" del "%WINPY_ZIP%"
    )
    echo Criando ambiente virtual a partir do WinPython...
    "%WINPY_DIR%\python.exe" -m venv "%VENV_DIR%"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [ERRO] Falha ao criar o ambiente virtual.
    pause
    exit /b 1
)

REM --- 2. Dependencias ----------------------------------------------------------
echo.
echo === Atualizando o pip ===
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip --no-warn-script-location

echo.
echo === Instalando dependencias ===
echo     (o RapidOCR baixa ~15 MB de modelos na primeira leitura)
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 (
    echo [ERRO] Falha ao instalar as dependencias.
    pause
    exit /b 1
)

REM --- 3. Configuracao inicial --------------------------------------------------
echo.
echo === Gerando a configuracao inicial ===
REM Cria config\config.json com os padroes e, se existir um position.cfg da
REM versao antiga, aproveita dele as credenciais de banco ja cadastradas.
"%VENV_DIR%\Scripts\python.exe" "%SCRIPT_DIR%main.py" --check

echo.
echo ==============================================================================
echo  [CONCLUIDO]
echo.
echo  Proximos passos (duplo clique, nao precisa de linha de comando):
echo    1) Configurar.bat  - cadastre contas, perfis e bancos; revise parametros
echo    2) botao "Abrir o jogo no Chrome", faca login e va ate a tela do cla
echo    3) botao "Calibrar" (ou o Calibrar.bat) - marque os controles na captura
echo    4) botao "Executar coleta agora" para testar
echo    5) Agendador de Tarefas do Windows apontando para run.bat
echo ==============================================================================
echo.
pause
endlocal
