@echo off
cd /d "%~dp0"
python -X utf8 "%~dp0main.py" --demo
if errorlevel 1 pause
