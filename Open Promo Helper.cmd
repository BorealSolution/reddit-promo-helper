@echo off
title Promo Helper - keep this window open
cd /d "%~dp0"

echo.
echo   Starting the promo helper...
echo   Your browser will open in a moment.
echo.
echo   KEEP THIS WINDOW OPEN while you use it.
echo   Closing this window shuts the helper down.
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo   Setup has not been run yet. In a terminal here, run:
    echo.
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Port 5000 is often taken (AirPlay, other dev servers); fall back if so.
"%PY%" promoter.py web --port 5000
if errorlevel 1 (
    echo.
    echo   Port 5000 was busy - trying 5001 instead.
    echo.
    "%PY%" promoter.py web --port 5001
)

echo.
echo   The promo helper has stopped.
pause
