"""What the host supplies, and what the package does without it.

WHY THIS EXISTS. Five places in this package reached back into
`app.*` -- the control panel's settings, its GCS helpers, its actor
registry and its memory bridge -- each wrapped in `except Exception` so
the package "worked" outside agentos. It did not work; it
degraded, silently, and in one case destructively:

    # app/ledger/actors.py, reached from append_event
    await conn.fetchval("SELECT 1 FROM actors WHERE id = $1", actor_id)

On a database holding only the ledger tables -- which is precisely what
a consumer has -- there is no `actors` table. The query fails, the
`except Exception` swallows it, and POSTGRES HAS ALREADY POISONED THE
TRANSACTION: the next statement dies with
`InFailedSQLTransactionError`, naming neither the cause nor the missing
table. So the audit a consumer never asked for broke the append it did.

Found by running the suite against a genuinely empty Postgres. The
shared test database has `actors` from the migrations, so it had hidden
this for as long as it existed. It is the same failure
`app/services/story_cancellation.py` documents at length: "the failed
insert left the CALLER'S transaction aborted, so everything after it in
the same transaction failed too."

SO: NOTHING IS IMPORTED FROM THE HOST. The host hands its wiring in
once, and a consumer that hands in nothing gets a package that does
less rather than one that breaks. Every hook defaults to absent, and
absent means "skip", never "try and swallow the error".

    # agentos, once, in app/ledger/__init__.py
    configure(actor_check=..., bucket_name=..., gcs_client=...)

    # a consumer
    pass                      # inline blobs, no audit, no bridge

MODULE-LEVEL AND NOT PASSED PER CALL, deliberately. `append_event` and
`store_blob` are free functions called from everywhere in the host;
threading a context object through every call site would be a larger
change to this PR than the one it is making, and there is one host per
process. `LedgerClient` may grow per-instance overrides later; this is
the seam, not the ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional

# An async `(conn, actor_id) -> bool`. Returning False makes the journal
# log that the actor will not resolve for attribution; raising is the
# caller's bug and is not caught here.
ActorCheck = Callable[[Any, Any], Awaitable[bool]]

# A sync `() -> client`, and an async `(uri) -> bytes`. Only consulted
# for payloads over the inline threshold.
GcsClientFactory = Callable[[], Any]
Downloader = Callable[..., Awaitable[bytes]]

# An async `(event: dict) -> None`, fired after a successful append.
# Detached by the caller; see journal._swallow.
EventSink = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class Integration:
    """Everything the package will use IF given, and nothing it needs.

    Every field is optional and every default is inert. A consumer that
    calls `configure()` never gets a feature it did not ask for, and --
    the point of the rewrite -- never gets a failure from one either.
    """

    # The bucket for blobs over the inline threshold. Empty means "no
    # external storage configured", and `store_blob` raises only when a
    # payload actually needs it -- a consumer storing small blobs never
    # has to think about this.
    bucket_name: str = ""
    gcs_client: Optional[GcsClientFactory] = None
    download: Optional[Downloader] = None

    # Attribution. Absent means the audit does not run. It MUST NOT mean
    # "run it and hope", which is what the try/except version did.
    actor_check: Optional[ActorCheck] = None

    # Fired after a successful append, detached. The host's memory
    # bridge; a consumer has none.
    on_event: Optional[EventSink] = None


_current = Integration()


def configure(**kw: Any) -> Integration:
    """Set the host's wiring. Returns the new configuration.

    Keyword-only and merging, so a host can call it more than once (at
    import and again once settings are loaded) without restating fields
    it does not own.
    """
    global _current
    unknown = set(kw) - {f for f in Integration.__dataclass_fields__}
    if unknown:
        raise TypeError(f"unknown integration field(s): {sorted(unknown)}")
    _current = replace(_current, **kw)
    return _current


def current() -> Integration:
    """The active configuration. Always an `Integration`, never None."""
    return _current


def reset() -> None:
    """Back to the inert default. For tests that configure a host and
    must not leak it into the next one."""
    global _current
    _current = Integration()
