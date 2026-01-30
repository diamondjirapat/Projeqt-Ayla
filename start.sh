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

# ---------- Python checks ----------
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}[ERROR] Python 3 is not installed${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 - <<'EOF'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
EOF
)

REQUIRED="3.13"

if [[ "$(printf '%s\n' "$REQUIRED" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]]; then
    echo -e "${RED}[ERROR] Python >= ${REQUIRED} required (found ${PYTHON_VERSION})${NC}"
    exit 1
fi

echo -e "${GREEN}[INFO] Using Python $(python3 --version)${NC}"

# ---------- Virtual environment ----------
if [ ! -f ".venv/bin/activate" ]; then
    echo -e "${YELLOW}[INFO] Creating virtual environment...${NC}"
    python3 -m venv .venv
fi

echo -e "${GREEN}[INFO] Activating virtual environment...${NC}"
source .venv/bin/activate

# ---------- Pip sanity ----------
python -m pip install --upgrade pip setuptools wheel >/dev/null

# ---------- Dependencies ----------
if [ -f "requirements.txt" ]; then
    echo -e "${GREEN}[INFO] Installing/checking dependencies...${NC}"
    pip install -r requirements.txt
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

exec python bot.py
