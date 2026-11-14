@echo off
setlocal EnableExtensions
cd /d "%~dp0.." || exit /b 1

if not exist ".env" (
    echo [ERROR] Backend/.env is missing. Copy .env.example to .env and configure it.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    python -c "import sys; sys.exit(sys.version_info < (3, 13))" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python 3.13+ is required to create Backend/.venv.
        exit /b 1
    )
    echo [INFO] Creating Backend/.venv...
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 13))" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Backend/.venv is invalid or uses Python older than 3.13. Recreate it with Python 3.13+.
    exit /b 1
)

echo [INFO] Checking backend dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo [INFO] Starting backend [%~1]...
".venv\Scripts\python.exe" bot.py
exit /b %errorlevel%
