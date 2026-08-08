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
    echo [ERROR] Python 3.13+ required. Found %PYVER%
    pause
    exit /b 1
)

if %MAJOR% EQU 3 if %MINOR% LSS 13 (
    echo [ERROR] Python 3.13+ required. Found %PYVER%
    pause
    exit /b 1
)

echo [INFO] Using Python %PYVER%

REM ---------- Virtual environment ----------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [WARNING] Existing virtual environment is invalid or broken. Recreating...
        rmdir /s /q .venv
    )
)

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
    python -m pip install -r requirements.txt
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

REM ---------- Frontend build ----------
if exist "frontend\package.json" (
    echo [INFO] Building frontend...
    where npm >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] npm is not installed or not in PATH
        echo Please install Node.js from https://nodejs.org/
        pause
        exit /b 1
    )
    pushd frontend
    if not exist "node_modules" (
        echo [INFO] Installing frontend dependencies...
    ) else (
        echo [INFO] Checking frontend dependencies...
    )
    call npm install
    if errorlevel 1 (
        echo [ERROR] Frontend dependency installation failed
        popd
        pause
        exit /b 1
    )
    echo [INFO] Building frontend production bundle...
    call npm run build
    if errorlevel 1 (
        echo [ERROR] Frontend build failed
        popd
        pause
        exit /b 1
    )
    popd
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
