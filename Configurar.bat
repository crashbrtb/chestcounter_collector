@echo off
REM ==============================================================================
REM  Abre a interface de configuracao (duplo clique).
REM
REM  Mesma coisa que "run.bat config", num arquivo proprio: no Explorer, um duplo
REM  clique resolve, sem abrir prompt de comando nem lembrar de argumento.
REM ==============================================================================
call "%~dp0run.bat" config
