@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Projeqt-Ayla Discord Bot

echo ========================================
echo       Projeqt-Ayla Discord Bot
echo ========================================
echo.

REM ---------- Python presence ----------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH
    echo Please install Python 3.13+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ---------- Python version check ----------
for /f "tokens=2 delims= " %%v in ('python --version') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set MAJOR=%%a
    set MINOR=%%b
)

if %MAJOR% LSS 3 (
    echo [ERROR] Python 3.13+ required (found %PYVER%)
    pause
    exit /b 1
)

if %MAJOR% EQU 3 if %MINOR% LSS 13 (
    echo [ERROR] Python 3.13+ required (found %PYVER%)
    pause
    exit /b 1
)

echo [INFO] Using Python %PYVER%

REM ---------- Virtual environment ----------
if not exist ".venv\Scripts\activate.bat" (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
)

echo [INFO] Activating virtual environment...
call ".venv\Scripts\activate.bat"

REM ---------- Pip sanity ----------
python -m pip install --upgrade pip setuptools wheel >nul

REM ---------- Dependencies ----------
if exist "requirements.txt" (
    echo [INFO] Installing/checking dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed
        pause
        exit /b 1
    )
) else (
    echo [WARNING] requirements.txt not found
)

REM ---------- Environment file ----------
if not exist ".env" (
    echo [ERROR] .env file not found
    echo Copy .env.example to .env and configure it:
    echo   copy .env.example .env
    pause
    exit /b 1
)

REM ---------- Run bot ----------
echo.
echo [INFO] Starting bot...
echo ========================================
echo.

python bot.py

echo.
echo [INFO] Bot has stopped
pause
