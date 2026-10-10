@echo off
cd /d "%~dp0"
py -3 -m venv .venv || goto error
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto error
if not exist .env copy .env.example .env
if not exist .env goto error
echo Ready. Edit .env (for example: notepad .env)
echo Then open your bot, send /start and run: .venv\Scripts\python.exe chat_id.py
echo After entering TG_CHAT_ID and VK_TOKEN run start_windows.bat
pause
exit /b 0
:error
echo Setup failed: check Python 3 installation and network.
pause
exit /b 1
