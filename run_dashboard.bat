@echo off
REM =============================================================
REM run_dashboard.bat — Start XGB Bot Dashboard (Windows)
REM
REM Usage: Double-click or run from project root:
REM   run_dashboard.bat
REM
REM Then open: http://localhost:5050
REM =============================================================

cd /d "%~dp0"

echo.
echo   XGB Bot Dashboard
echo   ------------------------------------
echo   Project: %CD%
echo.

IF NOT EXIST "config\.env" (
  echo   WARNING: config\.env not found!
  echo   Copy config\.env.example to config\.env and fill credentials.
  echo.
)

echo   Installing dashboard dependencies...
pip install -q -r ui\api\requirements.txt

echo.
echo   Starting dashboard server...
echo   Open in browser: http://localhost:5050
echo   Press Ctrl+C to stop.
echo.

set PYTHONPATH=%CD%;%PYTHONPATH%
python -m ui.api.app
pause
