@echo off
set "WINPY_URL=https://github.com/winpython/winpython/releases/download/15.3.20250425final/Winpython64-3.12.10.0dot.zip"
set "WINPY_DIR=python"
set "WINPY_ZIP=winpython.zip"


echo Baixando WinPython...
curl -L -o %WINPY_ZIP% %WINPY_URL%

echo Extraindo arquivos...
mkdir %WINPY_DIR%
tar -xf %WINPY_ZIP% --strip-components=2 -C %WINPY_DIR%

echo Removendo arquivo ZIP...
del %WINPY_ZIP%

echo Atualizando o pip...
%WINPY_DIR%\python -m pip install --upgrade pip --no-warn-script-location

echo Instalando dependências...
%WINPY_DIR%\python -m pip install -r requirements.txt

echo Concluído!
pause