@echo off
setlocal
cd /d "%~dp0"
echo ==================================================
echo         Sentra Bot - Dependency Setup
echo ==================================================
echo.

rem ---- Find a usable Python interpreter ----
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add python.exe to PATH" during installation,
    echo then run install.bat again.
    pause
    exit /b 1
)

echo Using interpreter: %PY%
%PY% --version
echo.

rem ---- Create the virtual environment ----
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment (.venv)...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists - using it.
)

set "VPY=.venv\Scripts\python.exe"

echo Upgrading pip...
%VPY% -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo.
echo Installing dependencies from requirements.txt...
%VPY% -m pip install -r requirements.txt
if errorlevel 1 goto :fail

rem ---- Create .env from template if missing ----
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo Created .env from .env.example.
    )
)

echo.
echo ==================================================
echo  Setup complete!
echo.
echo  Next steps:
echo   1. Open the .env file and paste your DISCORD_TOKEN
echo      (get one at https://discord.com/developers/applications)
echo   2. Run the bot using either:
echo      - python main.py
echo      - or double-click start.bat
echo ==================================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Read the messages above for the cause.
pause
exit /b 1