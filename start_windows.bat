@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python venv not found. Run setup_windows.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" bot.py
pause
