@echo off
title Goofish Automation - Setup
cd /d "%~dp0"

echo ============================================
echo   Goofish Automation - First-time Setup
echo ============================================
echo.

REM ---- 1. Check Python on PATH ----
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found on PATH.
  echo   Please install Python 3.13 first, and tick
  echo   "Add Python to PATH" during installation.
  echo   Download: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

REM ---- 0. requirements.txt must exist ----
if not exist "requirements.txt" (
  echo [ERROR] requirements.txt not found in this folder.
  echo   Make sure you run setup.bat inside the project folder.
  pause
  exit /b 1
)

REM ---- 2. Create virtual environment ----
echo [1/3] Creating virtual environment (.venv) ...
if exist ".venv\Scripts\python.exe" (
  echo       .venv already exists, skipping.
) else (
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
)

REM ---- 3. Upgrade pip (non-fatal) ----
echo [2/3] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

REM ---- 4. Install dependencies ----
echo [3/3] Installing dependencies (may take a few minutes) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Failed to install dependencies. Check your network and retry.
  pause
  exit /b 1
)

echo.
echo ============================================
echo   Setup finished!
echo.
echo   Next steps:
echo     1. Make sure Microsoft Edge is installed.
echo     2. Make sure config.yml has your DeepSeek api_key.
echo     3. Double-click start.bat to launch.
echo     4. In the debug Edge window that opens, log in to Goofish.
echo ============================================
echo.
pause
