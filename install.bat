@echo off
setlocal enabledelayedexpansion

REM ==============================================================================
REM Total Battle Collector - Automated Setup & Environment Installer
REM ==============================================================================

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_DIR=%SCRIPT_DIR%venv"
set "WINPY_DIR=%SCRIPT_DIR%python"
set "WINPY_ZIP=%SCRIPT_DIR%winpython.zip"
set "WINPY_URL=https://github.com/winpython/winpython/releases/download/15.3.20250425final/Winpython64-3.12.10.0dot.zip"

echo.
echo ==============================================================================
echo     Total Battle Collector - Instalacao do Ambiente Virtual
echo ==============================================================================
echo.

REM 1. Criar diretorio de logs
if not exist "%SCRIPT_DIR%execution_logs" (
    mkdir "%SCRIPT_DIR%execution_logs"
    echo [OK] Diretorio 'execution_logs' criado.
)

REM 2. Verificar se o Python esta instalado no sistema
set "SYS_PYTHON="
where python >nul 2>nul
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "SYS_PYTHON=%%i"
)

if defined SYS_PYTHON (
    echo [OK] Python do sistema detectado: %SYS_PYTHON%
    echo Criando ambiente virtual venv...
    python -m venv "%VENV_DIR%"
) else (
    echo [INFO] Python nao encontrado no sistema. Baixando WinPython portátil...
    
    if not exist "%WINPY_DIR%\python.exe" (
        echo [1/3] Baixando WinPython...
        curl -L -o "%WINPY_ZIP%" "%WINPY_URL%"
        
        echo [2/3] Extraindo arquivos de base...
        if not exist "%WINPY_DIR%" mkdir "%WINPY_DIR%"
        tar -xf "%WINPY_ZIP%" --strip-components=2 -C "%WINPY_DIR%"
        
        echo [3/3] Removendo arquivo ZIP temporario...
        if exist "%WINPY_ZIP%" del "%WINPY_ZIP%"
    )
    
    echo Criando ambiente virtual venv a partir do WinPython...
    "%WINPY_DIR%\python.exe" -m venv "%VENV_DIR%"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [ERRO] Falha ao criar o ambiente virtual venv.
    pause
    exit /b 1
)

echo.
echo === Atualizando pip no ambiente virtual ===
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip --no-warn-script-location

echo.
echo === Instalando dependencias do requirements.txt ===
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%SCRIPT_DIR%requirements.txt"

echo.
echo ==============================================================================
echo [CONCLUIDO] Instalacao finalizada com sucesso!
echo Todas as dependencias (incluindo o motor neural RapidOCR) foram instaladas.
echo Para executar o bot, utilize o arquivo: run.bat
echo ==============================================================================
echo.
pause
endlocal