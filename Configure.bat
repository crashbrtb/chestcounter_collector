@echo off
REM ==============================================================================
REM  Opens the configuration interface (double-click).
REM
REM  Same as "run.bat config" in its own file: in Explorer, a double-click works
REM  directly without opening a command prompt or remembering command line args.
REM ==============================================================================
call "%~dp0run.bat" config
