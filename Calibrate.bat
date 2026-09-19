@echo off
REM ==============================================================================
REM  Opens the calibration wizard directly (double-click).
REM
REM  Requires the game to be open and connected. If it isn't yet, use
REM  Configure.bat and the "1 - Open game in Chrome" button before calibrating.
REM ==============================================================================
call "%~dp0run.bat" calibrate
