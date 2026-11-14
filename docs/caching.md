# Backend caching

Caches are local to each backend process. No Redis or additional service is
required. Writes invalidate local copies; other processes and external writers
are observed on TTL expiry.

## Consistency and limits

- Guild documents default to fresh database reads (`GUILD_CACHE_MODE=fresh`).
  Message listeners share a snapshot for one message event, for at most one
  second. `GUILD_CACHE_MODE=ttl` opts into a 60-second document cache, capped at
  2,000 entries per repository instance.
- Autoroles use a separate 60-second cache, capped at 2,000 guilds per cog.
  The first join after expiry reads the current role directly from MongoDB.
  Guild writes invalidate all local autorole copies, including other bot
  instances. Failed refreshes skip assignment rather than using expired roles.
- Social polls cache at most 2,000 results and 2,000 metadata records per
  provider service. Results expire at the platform's configured polling
  interval; failures retain their retry delay. Metadata expires after 24 hours.
  Full caches evict the least recently used entry. Concurrent requests for the
  same normalized platform, handle, and live mode share one provider call.
  Each caller receives a private result. Evicted metadata can be recovered from
  the persisted subscription metadata supplied by the worker.
- Global ranks and leaderboard pages use a 15-second snapshot with at most
  128 entries per model. Routine XP awards preserve the snapshot so active chat
  does not continuously discard expensive aggregations. Explicit `set_xp`
  corrections invalidate all local ranking caches. Rankings may therefore lag
  routine XP awards or external writes by the cache TTL.
- Prefixes and locales expire after five minutes. Profile, artwork, attachment
  URL, ticket-miss, and missing-member caches retain their existing TTLs and
  size limits. Permission-bearing member objects are not retained.

In-flight reads that predate an invalidation can finish for their existing
callers, but cannot refill the invalidated cache. Cancelling a waiter does not
cancel a load shared by other callers. The shared cache targets one asyncio
event loop, not access from multiple threads.

## Metrics

`GET /internal/cache-metrics` requires the existing `X-Internal-Key` header and
returns `Cache-Control: no-store`. Keep this endpoint on the internal API;
do not expose the internal key in browser code.

The `caches` array groups live cache instances by a static name, such as
`social.polls`, `guild.autoroles`, or `leveling.global`. No cache keys, user or
guild IDs, cached values, or credentials are returned.

Each group includes:

- `instances`, `size`, `max_size`, and `pending`: current occupancy and capacity;
  capacity is summed across instances. Expired entries are pruned on inspection.
- `hits`, `misses`, `hit_rate`: completed-value lookups. Concurrent waiters that
  find no stored value count as misses, even when they share a load.
- `evictions`, `expirations`: capacity removals and expired entries removed.
  Explicit invalidations are not counted as evictions.
- `loads`, `coalesced`, `load_errors`: actual loader invocations, callers sharing
  an existing load, and loads that raised or were cancelled. Social retryable
  failures are cached as safe data, so they do not increment `load_errors`.
- `load_seconds`, `average_load_seconds`: total and average loader elapsed time,
  including failed loads; cache hits and shared waiters add no loader time.

Counters survive cache clears and last for the lifetime of each instance.
Discarded instances disappear from the snapshot, and restarting the process
resets counters. Scrape each backend process separately and retain snapshots
in your monitoring system to measure trends. A low ranking hit rate or high
social eviction rate can guide later TTL/capacity tuning using actual traffic.
