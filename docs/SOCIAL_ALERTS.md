# Social alerts

The Python bot automatically loads `cogs/social_alerts.py`. Restart the bot after setting provider credentials in `.env`.
The Bun gateway serves the editor and proxies guild settings; it does not poll platforms or hold provider credentials.

## Provider setup

| Platform | Configuration | Supported events |
| --- | --- | --- |
| Twitch | `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` from a [Twitch Developer application](https://dev.twitch.tv/docs/authentication/register-app/) | Live streams |
| YouTube | `YOUTUBE_API_KEY` from a Google Cloud project with [YouTube Data API v3 enabled](https://developers.google.com/youtube/v3/getting-started) | New public videos and live streams |
| X | `X_BEARER_TOKEN` from an X application with [user timeline access](https://docs.x.com/x-api/users/get-posts) | New posts, excluding replies and reposts |

Example placeholders (replace locally, never commit real values):

```dotenv
SOCIAL_ALERTS_ENABLED=true
TWITCH_CLIENT_ID=your_application_id
TWITCH_CLIENT_SECRET=your_application_secret
YOUTUBE_API_KEY=your_youtube_data_api_key
X_BEARER_TOKEN=your_x_app_bearer_token
```

Provider access, review requirements, quotas, and X billing depend on your developer account. Polling uses your project's
API quota. Configure only providers you intend to use; missing credentials never prevent the rest of the bot from starting.
Set `SOCIAL_ALERTS_ENABLED=false` to stop all polling.

## Create an alert

1. Restart the bot and open **Server Settings → Social Alert**.
2. Add an alert, select a platform, and enter a handle (for example `@creator` or `creator`). YouTube also accepts a `UC…` channel ID.
3. Choose a text channel and the event types you want. Full profile URLs are not accepted.
4. Configure the message and optional mention, then enable the alert. Changes save automatically.
5. Use **Refresh monitoring status** to check connection, permission, quota, or delivery errors.

The sending bot needs **View Channel**, **Send Messages**, and **Embed Links** in the destination.
**Read Message History** allows recovery after an interrupted send. Role/everyone pings require Discord's applicable
mention permissions. Creator text and templates cannot ping arbitrary users or roles; only the explicitly selected
mention target is permitted.

## Polling and first-run behavior

The worker ticks every 30 seconds. Each alert has a durable next-check time; repeated sources share a bounded process cache.

| Setting | Default | Minimum |
| --- | --- | --- |
| `SOCIAL_TWITCH_POLL_SECONDS` | 120 | 60 |
| `SOCIAL_YOUTUBE_POLL_SECONDS` | 300 | 120 |
| `SOCIAL_YOUTUBE_LIVE_DISCOVERY_SECONDS` | 3600 | 300 |
| `SOCIAL_X_POLL_SECONDS` | 900 | 300 |

YouTube uses the uploads playlist and video details for uploads and known streams. Periodic live search discovers
streams absent from the uploads playlist. Its discovery interval affects detection latency and your project's search quota;
choose an interval appropriate to the number of watched channels. Quota failures during discovery do not stop upload alerts.

The first successful check records existing content without sending it. Subsequent checks announce new events once.
Pausing and resuming, changing the watched account, changing the destination, or changing event types establishes a new
baseline. Message and mention edits do not reset it. Existing live broadcasts on the first check are skipped too.
Catch-up is bounded by the provider's recent result window (50 YouTube uploads, 100 X posts).
The worker does not promise exhaustive backfill after long outages.

## Persistence and recovery

`social_alert_state` stores per-subscription checkpoints, provider identifiers, bounded pending events, next-check times,
and 180-second leases. One ready bot polls per process; MongoDB `_id` uniqueness and conditional claims protect replicas.
Lease-token checks prevent stale workers from acknowledging or starting a send. The database manifest supplies a guild
lookup index and a 90-day inactivity TTL via `expires_at`; active checkpoints refresh their expiry.

An attempt is persisted **before** contacting Discord. A definite 4xx rejection remains retryable. A timeout, server error,
or crash after the request starts is treated as uncertain: the bot searches recent channel history for its unique embed
footer marker and acknowledges a confirmed send. It never blindly repeats an uncertain attempt. If delivery cannot be
confirmed, inspect channel history; remove and recreate the alert only after checking for duplicates. This favors avoiding
duplicate pings over automatic recovery of an unconfirmed message.

Provider failures back off and are isolated per alert. Diagnostic responses include status codes and check/send timestamps,
never API keys, tokens, raw provider errors, or request headers. Credentials remain in deployment configuration.

## Verification

```powershell
.venv/Scripts/python.exe -m unittest tests.test_social_monitor tests.test_social_alerts tests.test_web_server_guild_settings -v
.venv/Scripts/python.exe -m ruff check config.py cogs/social_alerts.py database/social_alerts.py services/social_providers.py services/social_delivery.py services/social_runtime.py
bun --cwd frontend test:client src/views/server/SocialAlertsView.spec.ts
bun --cwd frontend build
```

The automated provider/delivery tests use fake responses and mocked Discord sends, so they do not consume API quota or
send real notifications. After supplying real credentials, verify an alert with a controlled new post/stream after its first check.
