@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo             Starting Sentra Bot...
echo ===================================================
echo.

rem 1. Check for Python virtual environment or system Python
set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1 && set "PY=python"
    if not defined PY (
        where py >nul 2>&1 && set "PY=py"
    )
)

if not defined PY (
    echo [ERROR] Python was not found on your system!
    echo.
    echo Please install Python 3.11 or newer from:
    echo https://www.python.org/downloads/
    echo IMPORTANT: Make sure to check "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

rem 2. Check for .env file
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [Sentra] Created .env from .env.example.
        echo [Sentra] Please edit .env, add your DISCORD_TOKEN, and run start.bat again!
        echo.
        pause
        exit /b 1
    )
)

rem 3. Run main.py
%PY% main.py
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [Sentra] Bot exited with code %EXITCODE%.
) else (
    echo [Sentra] Bot stopped.
)
pause
exit /b %EXITCODE%