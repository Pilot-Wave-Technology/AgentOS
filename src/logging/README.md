# `src/logging/`

OpenClaw's logging stack: subsystem-scoped loggers, console formatting, an
on-disk rolling file logger, log-level resolution, and text redaction.

## Key files

- `subsystem.ts` — `createSubsystemLogger(name)` is what most of the codebase
  calls (129 call sites). Each call fans out to both the console and the file
  logger independently, based on separately configured levels.
- `console.ts` — resolves console `level`/`style` settings and owns
  `enableConsoleCapture()`, which monkey-patches `console.log/info/warn/error`
  so raw `console.*` calls also get mirrored into the file logger.
- `logger.ts` — the on-disk rolling file logger (`tslog`-based). Writes JSON
  lines to a file under the OS-specific openclaw tmp dir
  (`resolvePreferredOpenClawTmpDir()`), with a 500MB size cap and 24h
  retention for dated rolling files.
- `levels.ts` — the `LogLevel` union (`silent` < `fatal` < `error` < `warn` <
  `info` < `debug` < `trace`) and level-comparison helpers.
- `config.ts` — reads the `logging.*` block from `openclaw.json`.
- `redact.ts` / `redact-bounded.ts` / `redact-identifier.ts` — strip secrets
  and long identifiers out of log text before it's written anywhere.
- `timestamps.ts` — shared timestamp formatting (local ISO with UTC offset).

## Two separate log destinations — don't conflate them

Every subsystem logger call can end up in two independent places:

1. **Console** (`console.ts` / `subsystem.ts`'s `writeConsoleLine`) — written
   to `stdout`/`stderr`. Under the gateway's systemd unit
   (`openclaw-gateway@<port>.service`), systemd redirects this straight to
   `/var/log/openclaw-<port>.log` via `StandardOutput=append:...`. This is
   the file the GCP Ops Agent tails in production (configured in
   `scripts/redeploy.sh`).
2. **File logger** (`logger.ts`) — a *different* file, always JSON, always
   under the openclaw tmp dir — independent of the console output above and
   not read by the Ops Agent.

These have independently configurable levels (`logging.level` for the file
logger, `logging.consoleLevel` for console) and can diverge — e.g. console at
`info` while the file logger captures `debug`.

## `logging.consoleStyle` — production Cloud Logging depends on this

`consoleStyle` (`pretty` | `compact` | `json`) controls how console lines are
formatted (`normalizeConsoleStyle()` in `console.ts`):

- Not set, and `stdout.isTTY` → `pretty` (colorized, `HH:MM:SS` prefix).
- Not set, and `stdout` is **not** a TTY → `compact` (colorized, full ISO
  timestamp prefix). This is the default whenever the gateway runs under
  systemd, since a file-redirected stdout is never a TTY.
- Explicitly `json` → each line is a single JSON object:
  `{time, level, subsystem, message, ...meta}` (see `formatConsoleLine()` in
  `subsystem.ts`).

**Production requires `json`.** `scripts/redeploy.sh` configures the GCP Ops
Agent to tail `/var/log/openclaw-*.log` with a `parse_json` processor that
extracts `time` (→ `LogEntry.timestamp`) and `level` (→ `LogEntry.severity`).
If `consoleStyle` isn't `json`, every line is plain human-readable text
instead, and:

- `parse_json` silently fails, so Cloud Logging falls back to its own
  ingest-time timestamp — while the app's own printed timestamp (added by
  `setConsoleTimestampPrefix(true)` in the gateway's `run.ts`) sits in the
  message body as plain text. You get two timestamps per entry.
- There's no `jsonPayload.level` to map, so severity stays `DEFAULT` instead
  of `INFO`/`WARNING`/`ERROR`.

This bit us in production: `consoleStyle` was never explicitly set, so it
silently resolved to `compact`, and every gateway log line in Cloud Logging
showed `DEFAULT` severity with a duplicate timestamp. The fix is
`node openclaw.mjs config set logging.consoleStyle json`, applied in
`scripts/redeploy.sh` on every deploy — **do not remove that line** without
also updating (or dropping) the Ops Agent's `parse_json`/`map_severity`
config in the same script.

### Known residual gaps (present even with `consoleStyle=json`)

- Raw `console.log(...)` calls that bypass `createSubsystemLogger` (i.e. code
  that never goes through `subsystem.ts`) are only timestamp-prefixed by
  `console.ts`'s `enableConsoleCapture` patch — they are **not** JSON-encoded,
  and will still show up as `DEFAULT` severity in Cloud Logging.
- `gateway/ws-log.ts` logs failed RPC responses (`ok: false`, rendered as
  `✗` in the message) via `.info()`, not `.warn()`/`.error()` — the failure
  only shows up in the message text, not in `LogEntry.severity`.
- Anything **not** written by the gateway process at all still shows up as
  unparsed `DEFAULT` severity — see below.

### Other DEFAULT-severity entries aren't a regression — check the source first

Ops Agent's `config.yaml` (written by `scripts/redeploy.sh`) is *merged* into
Ops Agent's built-in default config, not a replacement for it. Any built-in
identifier the script doesn't redeclare keeps running — in particular the
built-in `default_pipeline`, which tails `/var/log/syslog` with no parsing at
all. That's how sshd/cron/kernel logs still reach Cloud Logging, but it also
means unrelated services on the box (e.g. `chrome-remote-desktop`, when
`enable_chrome_remote_desktop=true`) show up as raw, unparsed, `DEFAULT`
lines — nothing to do with the gateway or `consoleStyle`. Before assuming a
`DEFAULT`-severity entry is a regression of the fix above, check whether it's
actually from the `openclaw-gateway@<port>` process at all.
`scripts/redeploy.sh` overrides `default_pipeline` just enough to exclude
known-noisy non-gateway sources (currently: chrome-remote-desktop heartbeats)
via an `exclude_logs` processor, without disabling syslog capture entirely.

## Log rotation

The file logger (`logger.ts`) manages its own rotation (500MB cap, 24h
retention for dated files). `/var/log/openclaw-*.log` — the systemd-captured
console output described above — has no in-app rotation; it's rotated by the
`logrotate.d/openclaw-gateway` config installed in `scripts/redeploy.sh`
(daily, 7 rotations, `copytruncate` since the gateway never reopens that fd).
