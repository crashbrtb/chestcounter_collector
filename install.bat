@echo off
set "WINPY_URL=https://github.com/winpython/winpython/releases/download/15.3.20250425final/Winpython64-3.12.10.0dot.zip"
set "WINPY_DIR=python"
set "WINPY_ZIP=winpython.zip"
set "VENV_DIR=venv"

echo.
echo === 1. Baixando WinPython ===
curl -L -o %WINPY_ZIP% %WINPY_URL%

echo.
echo === 2. Extraindo arquivos de base ===
mkdir %WINPY_DIR%
REM Assume-se que a estrutura interna do ZIP WinPython é "Winpython64-3.12.10.0dot/python-3.12.10.amd64", e queremos o conteudo de "python-3.12.10.amd64"
tar -xf %WINPY_ZIP% --strip-components=2 -C %WINPY_DIR%

echo.
echo === 3. Removendo arquivo ZIP de download ===
del %WINPY_ZIP%

echo.
echo === 4. Atualizando o pip (Base) ===
REM Usamos o executável base para garantir que o venv seja criado com o pip mais recente.
%WINPY_DIR%\python.exe -m pip install --upgrade pip --no-warn-script-location

echo.
echo === 5. Criando o ambiente virtual (%VENV_DIR%) ===
%WINPY_DIR%\python.exe -m venv %VENV_DIR%

echo.
echo === 6. Ativando o ambiente virtual e atualizando seu pip ===
REM Chamamos o executável do ambiente virtual para operar dentro dele.
%VENV_DIR%\Scripts\python.exe -m pip install --upgrade pip --no-warn-script-location

echo.
echo === 7. Instalando dependências no ambiente virtual ===
REM Note: Aqui, o requirements.txt deve estar no mesmo diretório do script batch.
%VENV_DIR%\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo === 8. Concluído! ===
echo Para usar o ambiente virtual, execute: %VENV_DIR%\Scripts\activate.bat
pause