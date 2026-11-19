@echo off
REM One-time setup: Python virtual environment + backend deps, then frontend deps.
cd /d "%~dp0backend"
python -m venv .venv || goto :err
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :err
if not exist .env copy .env.example .env >nul
cd /d "%~dp0frontend"
call npm install || goto :err
echo.
echo Setup complete. Run start.bat to launch RootCause Copilot.
pause
exit /b 0
:err
echo Setup failed - see the messages above.
pause
exit /b 1
