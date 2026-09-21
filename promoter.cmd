@echo off
REM Run the tool without activating the virtualenv first.
REM   promoter stats
REM   promoter review
REM Double-clicking this file with no arguments opens a shell in this folder.

setlocal
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo   The virtualenv is missing. Set it up once with:
    echo.
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    exit /b 1
)

if "%~1"=="" (
    echo.
    echo   Reddit Promo Code Assistant
    echo.
    echo   Common commands:
    echo     promoter stats                                  how many codes are left
    echo     promoter add --app sleepbound --users a,b,c     record who asked for a code
    echo     promoter review                                 approve and send
    echo.
    echo   Full list:  promoter --help
    echo.
    exit /b 0
)

"%PY%" "%~dp0promoter.py" %*
