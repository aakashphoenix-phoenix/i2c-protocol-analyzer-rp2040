@echo off
cd /d "%~dp0"
"C:\Program Files\WindowsApps\OpenAI.CodexPrimaryRuntime.v26-1105-1297-0_26.1105.1297.0_x64__3k8sg7r9htsxt\dependencies\python\python.exe" -X utf8 "%~dp0tools\launch_visual_demo.py"
if errorlevel 1 pause
