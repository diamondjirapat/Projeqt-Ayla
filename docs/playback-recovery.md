# NodeLink 3.9.0 and playback recovery

The 19–21 October 2026 log showed overlapping PO-token generation, repeated SABR
stalls, duplicate track-end events, and tracks being stopped during recoverable
Discord voice reconnects.

The local checkout is now pinned to stable **NodeLink 3.9.0**, tag `v3.9.0`,
commit `cdb02c6814fd8abe37f1d02955770e51b1414d06`. The pin is recorded in
`patches/nodelink/version.json`. The original checkout, including its uncommitted
changes, configuration, dependencies, caches, and logs, is preserved locally at
`tmp/nodelink-3.8-backup-20261021/`.

The fixes in `patches/nodelink/` are rebased against that exact release:

- Keep upstream's lightweight NativeDOM and serialize complete token operations
  across managers. Defer resets during active requests and restore browser globals
  after each operation, so overlapping requests cannot remove each other's state.
- Refresh a stalled SABR stream using SABR clients, without resolving an
  incompatible direct stream or mirror. Stop after three refreshes without audio,
  ignore refresh results after cancellation, and report teardown errors once.
- Emit one track-end event per playback attempt. Preserve the previous local
  safeguards for early stream failures, disconnects, and synchronous playback-start
  events, including listener and timeout cleanup.
- Migrate legacy user settings before merging defaults, so a configured port or
  password cannot be shadowed by the new default configuration.

Upstream 3.9.0 already refreshes forbidden URLs and defers recoverable voice
reconnect errors. Those older local fixes are covered by tests and no longer need
duplicate patches. JSDOM and its canvas dependency are no longer needed.

The service runner checks and applies these patches before starting services.
Already-applied patches are skipped. Incompatible upstream changes stop startup
with the patch name, before modifying any pending files. It also checks the version
and commit pin. Keep the patches aligned with NodeLink when updating it.

The existing `config.js` is retained, and a validated `config.ts` has been generated
with the same server settings, credentials, YouTube client order, and cluster mode.
Encrypted credential and track caches are copied to the upgraded checkout.

To install the same release on another host, preserve its configuration and caches,
clone the pinned release into `Nodelink/`, and apply the patches before building:

```sh
git clone --branch v3.9.0 --single-branch https://github.com/PerformanC/NodeLink.git Nodelink
python scripts/patch_nodelink.py
cd Nodelink
npm install
npm run build
```

Restore that host's saved `config.js` and `.cache/` into the new checkout. On first
startup, NodeLink will create `config.ts`. An existing `config.ts` takes precedence
over `config.js`.

For a separately managed NodeLink service, apply the fixes before restarting it:

```sh
python scripts/patch_nodelink.py /path/to/Nodelink
```

The runner executes TypeScript with Node's native type stripping; it no longer
requires `tsx`. Node.js 22.22.2 or newer is required. The local JavaScript output
has also been rebuilt. For `start:dist`, rebuild after changing the source patches.

Install the updated Python requirements to include discord.py's voice extras
(PyNaCl and davey), then restart the bot and NodeLink. Live playback still needs
verification on the deployed host; the automated tests use simulated upstream
responses and a local HTTP server. Startup tests launch source and compiled builds
with synthetic credentials, disabled sources, and temporary configurations; they
check `/version`, authenticated `/v4/info`, and legacy configuration migration.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node --experimental-strip-types --test tests/nodelink_playback.test.mjs tests/nodelink_startup.test.mjs
cd Nodelink
npm run type-check
```

YouTube can still reject streams. Discord rate limits and Last.fm outages are
external responses; their existing retry handling remains in place. A missing
Yandex token requires operator-supplied credentials, and unreachable Monochrome
instances require a working provider. These patches do not fabricate credentials
or disable those features.
