#!/usr/bin/env bash
set -Eeuo pipefail

# ---------- Colors ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ---------- Banner ----------
echo "========================================"
echo "      Projeqt-Ayla Discord Bot"
echo "========================================"
echo ""

# ---------- Cleanup ----------
cleanup() {
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        deactivate
    fi
}
trap cleanup EXIT INT TERM

# ---------- Python presence ----------
PYTHON="python3"
if ! command -v python3 >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1; then
        PYTHON="python"
    else
        echo -e "${RED}[ERROR] Python is not installed or not in PATH${NC}"
        echo "Please install Python 3.13+ from https://www.python.org/downloads/"
        exit 1
    fi
fi

# ---------- Python version check ----------
PYTHON_VERSION=$($PYTHON - <<'EOF'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
EOF
)

MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$MAJOR" -lt 3 ]; then
    echo -e "${RED}[ERROR] Python 3.13+ required. Found ${PYTHON_VERSION}${NC}"
    exit 1
fi

if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 13 ]; then
    echo -e "${RED}[ERROR] Python 3.13+ required. Found ${PYTHON_VERSION}${NC}"
    exit 1
fi

echo -e "${GREEN}[INFO] Using Python $($PYTHON --version)${NC}"

# ---------- Virtual environment ----------
if [ ! -f ".venv/bin/activate" ]; then
    echo -e "${YELLOW}[INFO] Creating virtual environment...${NC}"
    $PYTHON -m venv .venv || {
        echo -e "${RED}[ERROR] Failed to create virtual environment${NC}"
        exit 1
    }
fi

echo -e "${GREEN}[INFO] Activating virtual environment...${NC}"
source .venv/bin/activate

# ---------- Pip sanity ----------
python -m pip install --upgrade pip setuptools wheel >/dev/null

# ---------- Dependencies ----------
if [ -f "requirements.txt" ]; then
    echo -e "${GREEN}[INFO] Installing/checking dependencies...${NC}"
    pip install -r requirements.txt || {
        echo -e "${RED}[ERROR] Dependency installation failed${NC}"
        exit 1
    }
else
    echo -e "${YELLOW}[WARNING] requirements.txt not found${NC}"
fi

# ---------- Environment file ----------
if [ ! -f ".env" ]; then
    echo -e "${RED}[ERROR] .env file not found${NC}"
    echo "Copy .env.example to .env and configure it:"
    echo "  cp .env.example .env"
    exit 1
fi

# ---------- Run bot ----------
echo ""
echo -e "${GREEN}[INFO] Starting bot...${NC}"
echo "========================================"
echo ""

python bot.py

echo ""
echo -e "${GREEN}[INFO] Bot has stopped${NC}"
