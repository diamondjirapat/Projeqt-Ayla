# Repository Guidelines

## Project Structure & Module Organization

`bot.py` starts the application and auto-loads public modules from `cogs/`; keep Discord features there. MongoDB connections and models belong in `database/`, while shared queue, artwork, cache, prefix, Last.fm, and i18n helpers belong in `utils/`. Keep English and Thai catalog keys aligned in `locales/en.json` and `locales/th.json`.

The Vue 3/Vite client lives in `frontend/src/`. Put reusable components in `frontend/src/components/`, shared types in `frontend/src/types/`, and source images in `frontend/public/` or `frontend/src/assets/`. Vite builds into `static/`, which `cogs/web_server.py` serves; do not hand-edit hashed files under `static/assets/`. Backend tests are in `tests/`; frontend tests are colocated as `*.spec.ts`.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt` installs runtime dependencies and Ruff.
- `python bot.py` starts the bot and embedded FastAPI server after local services are configured.
- `run_dev.bat` / `./run_dev.sh` starts development mode with live Vite dev server and bot.
- `run_prod.bat` / `./run_prod.sh` creates `.venv`, installs dependencies, builds the client, and starts the bot in production.
- `python -m unittest discover -s tests -v` runs the backend suite.
- `python -m ruff check .` lints Python; `python -m ruff format --check .` verifies formatting.
- `bun install --cwd frontend` installs locked client dependencies. Use `bun --cwd frontend dev`, `bun --cwd frontend test`, or `bun --cwd frontend build` to serve, test, or type-check/build.

## Coding Style & Naming Conventions

Target Python 3.13, four-space indentation, single quotes, and a 120-character line limit as configured in `pyproject.toml`. Use `snake_case` for modules/functions and `PascalCase` for classes and cogs. Vue/TypeScript uses two spaces, single quotes, `camelCase` identifiers, and `PascalCase.vue` components.

## Testing Guidelines

Use standard-library `unittest`, `IsolatedAsyncioTestCase`, and mocks for backend isolation. Name files `test_<area>.py` and methods `test_<behavior>`. Use Vitest and Vue Test Utils for client behavior. No coverage threshold is configured; add regression tests for every bug fix and update both locale catalogs when copy changes.

## Commit & Pull Request Guidelines

#✨ feat: 
#└ Add new feature
#🐛 fix: 
#└ Fix a bug (including security fixes)
#📝 docs: 
#└ Add or update documentation
#💄 ui: 
#└ Update UI and style files
#⚡ perf: 
#└ Improve performance
#♻️ refactor: 
#└ Refactor code without changing functionality
#🎨 style: 
#└ Changes that do not affect the meaning of the code
#🍱 assets: 
#└ Add or update assets
#🗑️ remove: 
#└ Remove code or files
#🧪 test: 
#└ Add or update tests
#📦 build: 
#└ Add or update build system or dependencies
#🚑 hotfix: 
#└ Critical hotfix
#🔧 chore: 
#└ Add or update configuration files or scripts
#🚧 wip: 
#└ Work in progress
#⏪ revert: 
#└ Revert changes
#🔀 merge: 
#└ Merge branches
#🏷️ release: 
#└ Release / Version tags
#🚀 deploy: 
#└ Deploy stuff
#🎉 init: 
#└ Begin a project

History favors short imperative subjects prefixed by purpose emoji, for example `✨ Add playlist search`, `🐛 Fix queue ordering`, `♻️ Refactor cache`, or `📝 Update translations`. PRs should explain user-visible behavior, link relevant issues, list checks run, and include screenshots for UI changes. Keep unrelated refactors separate.

## Security & Repository Hygiene

Copy `.env.example` to `.env`; never commit tokens, MongoDB URIs, OAuth secrets, Lavalink credentials, or session keys. Restrict `WEB_ALLOWED_ORIGINS`. The current `.gitignore` excludes `/frontend`, `/static`, `/tests`, and `*.txt`; use `git check-ignore -v <path>` and confirm intended files are visible before submitting changes.
