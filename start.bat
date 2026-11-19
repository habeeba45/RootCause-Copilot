@echo off
REM Launches backend (port 8000) and frontend dev server (port 5173) in two windows.
start "RootCause Copilot API" cmd /k "%~dp0start-backend.bat"
start "RootCause Copilot UI" cmd /k "%~dp0start-frontend.bat"
timeout /t 6 >nul
start http://localhost:5173
