@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The application is not installed. Run install.cmd first.
  pause
  exit /b 1
)
if not exist ".env" (
  echo Missing .env. Run install.cmd first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m app.main
if errorlevel 1 (
  echo.
  echo The application stopped with an error.
  pause
)

