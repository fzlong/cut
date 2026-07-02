@echo off
setlocal
cd /d "%~dp0"
python roughcut.py %*
if errorlevel 1 pause
