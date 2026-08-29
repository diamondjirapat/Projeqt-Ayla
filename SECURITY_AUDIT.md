# OWASP Top 10 Security Audit — Projeqt-Ayla

**Date:** 2026-08-25 · **Branch:** `web-base` · **Scope:** full repository (~12.8k lines Python + Vue 3 SPA)
**Method:** 7 parallel category finders → dedup → adversarial verification against strict false-positive criteria (confidence ≥ 8/10 required).

## Confirmed & Remediated Vulnerabilities

### HIGH

| # | OWASP | Location | Issue | Fix |
|---|-------|----------|-------|-----|
| 1 | A07/A02 | `config.py:63`, `web_server.py` cog_load | `SESSION_SECRET_KEY` defaulted to `''`; itsdangerous accepts an empty HMAC key, so on deployments without OAuth configured anyone reaching the web port could forge a session cookie for any Discord ID (incl. `OWNER_IDS`) → cross-guild admin takeover. Verified empirically. | `Config.resolve_session_secret()` falls back to a random per-boot key with a prominent warning; hard validation still enforced when OAuth is enabled. |
| 2 | A01 | `web_server.py` leveling endpoints | `/api/leveling/guilds`, `/api/leveling/leaderboard/global`, `/api/leveling/leaderboard/server/{id}` were fully unauthenticated: tenant enumeration + cross-tenant member activity data. | All three require a valid session; guild list filtered to the user's own guilds; per-server leaderboard requires membership (or owner). |
| 3 | A01 | `cogs/reactionroles.py:340+` | Hybrid groups force `invoke_without_command=True`, so the group-level `@has_permissions(manage_roles=True)` never ran → **any member could self-grant Administrator roles** via `/reactionrole add`. | Permission decorator repeated on every mutating subcommand (`add/remove/create/update/edit`); added managed-role and invoker-hierarchy guards to `rr_add`. |
| 4 | A03 | `ServerSettingsPanel.vue:897`, `models.py` | Stored XSS: level-alert description stored without HTML filtering, rendered via `v-html` with escape-never regex decorations. Manage-Guild attacker → script execution in co-admin/bot-owner sessions (cross-guild). | Server: HTML tags stripped in `GuildModel.set_level_alert_config`. Client: escape-before-decorate computed (`formattedPreviewDescription`). |

### MEDIUM

| # | OWASP | Location | Issue | Fix |
|---|-------|----------|-------|-----|
| 5 | A07 | `web_server.py /login`, `/api/auth/callback` | No OAuth `state` parameter → login CSRF (victim silently logged into attacker's account for 30 days). | Random state + short-lived HttpOnly cookie, constant-time comparison in callback. |
| 6 | A07 | `web_server.py /api/lastfm/*` | Last.fm linking trusted only a signed user-id state → victim's Last.fm session key bindable to attacker's account. | State must match an HttpOnly cookie set when the flow starts (browser-bound), constant-time compared. |
| 7 | A01 | `cogs/music.py musicchannel set/remove` | Same dead-check pattern as #3 → any member rebinds the guild music channel. | `manage_channels` repeated on both subcommands. |
| 8 | A01 | `cogs/music.py serverplaylist create/add/remove/setcover/delete/import` | Dead-check pattern → any member wipes/modifies shared playlists. | `manage_guild` repeated on all mutating subcommands (read-only ones left open). |
| 9 | A01 | `moderation.py kick/ban`, `autorole.py`, `reactionroles.py` | No invoker-hierarchy checks anywhere: Kick/Ban holders could ban server owners; role-mapping holders could grant roles above themselves. | `_target_above_invoker` hierarchy guard on kick/ban (+ locale keys en/th); hierarchy guard in `rr_add`; web parity guard below. |
| 10 | A01 | `web_server.py /api/guild/giveaways*` | Any authenticated outsider could read any guild's giveaways incl. entrant user-ID lists, enter foreign giveaways. | Membership gate on read/enter; entrant ID list exposed only to managers; safe projection otherwise. |
| 11 | A01/A04 | `web_server.py create_giveaway_endpoint` | `channel_id` resolved through global bot cache without tenant scoping → arbitrary bot-identity posts into other servers' channels. | Reject unless `channel.guild.id == guild_id`. |
| 12 | A10 | `web_server.py player search/play` | Blind SSRF: arbitrary user URLs forwarded to Lavalink from the host's internal network position; raw exception text echoed to callers. | `assert_public_url_host()` resolves DNS and rejects loopback/private/link-local/reserved/multicast hosts before fetching; generic 500 detail (details logged server-side). |
| 13 | A01 | `web_server.py save_guild_settings` | Web panel let Manage-Guild-only users bind reaction/auto roles above themselves or integration-managed roles, collapsing Discord's Manage-Server/Manage-Roles boundary. | `validate_assignable_role()` mirrors Discord-side guards: bot top-role, managed roles, and invoker hierarchy (admins exempt). |

### LOW (also fixed)

- `/api/guild/giveaways` serialized whole Mongo docs (entrant ID lists) to any member — now projected.

### A06 Advisories (dependencies)

- `aiohttp==3.13.3` sat inside the Jun–Aug 2026 advisory cohort (14 GHSAs, worst CVE-2026-69244 OOB heap read) → bumped to `aiohttp==3.14.3`.
- Unpinned auth/web-critical packages made builds non-reproducible → pinned exactly: `fastapi==0.141.1`, `uvicorn==0.52.0`, `itsdangerous==2.2.0`, `discord.py~=2.7.1`.

### Clean areas

No SQL/NoSQL operator injection, no command/template injection, no unsafe deserialization, no path traversal in static serving, no secrets logged, exactly one XSS sink repo-wide (fixed above).

## Residual risks (accepted)

- Imported-playlist source URLs are re-fetched by Lavalink at play time; URLs can only be planted by Manage-Guild holders via the now-gated import commands.
- DNS-based SSRF guards cannot fully prevent DNS-rebinding (TOCTOU between resolution and Lavalink fetch); provider allowlisting would close this if needed.
- The web port binds `0.0.0.0`; deploy behind a reverse proxy with TLS (`WEB_URL=https://…` sets the Secure cookie flag automatically).

## Verification

- `ruff check .` — clean
- `python -m unittest discover -s tests` — 69/69 runnable tests pass (6 `test_giveaway` errors are pre-existing environment failures requiring a live MongoDB; they predate these changes)
- Frontend: Vitest 15/15 pass, `vite build` clean (XSS fix included in `static/` bundle)
- Regression tests added for the new leveling-endpoint auth contract
