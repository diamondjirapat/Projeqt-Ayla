# Repository Guidelines

## Project Structure & Module Organization

This repository is the `Backend/` service. `bot.py` and `config.py` are at its
root; bot extensions live in `cogs/`, internal API routes in `api/`, database
models in `database/`, background tasks in `services/`, shared utilities in
`utils/`, translations in `locales/`, and Python tests in `tests/`.

The optional sibling repositories are `../frontend/` (Vue client and Bun/Elysia
public API) and `../Nodelink/` (audio server, including `patches/nodelink/`).
Backend checks must work without those sibling repositories. Cross-service
integration checks require explicit setup as described in `../README.md`.

## Build, Test, and Development Commands

Run these commands from this repository's root:

- `python -m pip install -r requirements-dev.txt` (runtime plus test and lint tools)
- `python bot.py`
- `python -m unittest discover -s tests -v`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `run_dev.bat` / `./run_dev.sh` starts the backend for development.
- `run_prod.bat` / `./run_prod.sh` starts the backend for production.

## Coding Style & Naming Conventions

Target Python 3.14, four-space indentation, and single quotes as configured in `pyproject.toml`; there is no line-length limit. Use `snake_case` for modules/functions and `PascalCase` for classes and cogs. Vue/TypeScript uses two spaces, single quotes, `camelCase` identifiers, and `PascalCase.vue` components.

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

Copy `.env.example` to `.env`; never commit tokens, MongoDB URIs, OAuth secrets, NodeLink credentials, or session keys. Restrict `WEB_ALLOWED_ORIGINS`. Frontend sources and tests must remain visible to Git. Generated `static/` output, dependencies, caches, scratch work, secrets, and test results remain ignored; use `git check-ignore -v <path>` to confirm intended files are visible before submitting changes.
