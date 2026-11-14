# Projeqt-Ayla - Backend Service 🤖

This directory contains the Python Discord Bot and FastAPI internal API service for Projeqt-Ayla.

## Features
- Discord Bot built with `discord.py` and modular cogs
- Authenticated internal REST API (`FastAPI`) for the web dashboard and player controls
- MongoDB database integration and schema models
- Multi-language localization support (`en`, `th`)
- Music playback orchestration via NodeLink
- Moderation, giveaways, leveling, scheduler, and tickets

## Project Structure
- `bot.py` - Main bot entry point and cog loader
- `config.py` - Configuration loading and validation
- `api/` - Internal FastAPI routes, models, and contract definitions
- `cogs/` - Discord bot extensions (music, moderation, leveling, etc.)
- `database/` - MongoDB connection and models
- `locales/` - Translation catalogs (`en.json`, `th.json`)
- `services/` - Business logic and background services
- `utils/` - Utility functions, queues, and helpers
- `tests/` - Backend test suite (`unittest`)

## Environment

Copy `.env.example` to `.env` and configure the bot token, MongoDB URI, and
NodeLink credentials. Use the same `AYLA_INTERNAL_KEY` as the public server in
`../frontend/.env`. Keep `WEB_URL` as the public origin without `/webplayer`.
The backend loads its own `.env` regardless of the caller's working directory.

## Running Locally

The launchers start only this backend (bot and internal API). They use
`Backend/.env` and create/use `Backend/.venv`; parent environment files and
virtual environments are never reused. Missing local configuration stops startup
with instructions. Dependencies are checked using the local virtual environment.
MongoDB, NodeLink, and the frontend are started separately and reached through
configured URLs. Both modes run `bot.py` and return its exit status.

### Development Mode
```bash
# Windows
run_dev.bat

# Linux / macOS
chmod +x run_dev.sh
./run_dev.sh
```

For production, use `run_prod.bat` or `bash run_prod.sh`.

For optional backend supervision with automatic restarts and prefixed logs, use
`python -m scripts.run_services --mode dev` from an environment with the backend
dependencies installed. `--mode prod` also runs only the backend. Logs stay in
`Backend/logs/`; the runner never scans ports or terminates existing services.

### Manual Setup
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
python bot.py
```

### Running Tests
```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m ruff check .
```

Cache limits, consistency windows, and the authenticated metrics endpoint are
documented in [Backend caching](docs/caching.md).

### Docker

From this repository, start the backend:

```bash
docker compose up --build -d
```

Configure reachable external MongoDB (`MONGODB_URI`) and NodeLink URLs in `.env`.
For MongoDB running on the Docker host, use `host.docker.internal` instead of
`127.0.0.1` on Docker Desktop; container `localhost` refers to the container itself.
The optional Compose file
in the parent workspace starts the entire stack instead.
