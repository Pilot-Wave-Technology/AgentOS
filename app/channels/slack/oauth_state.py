"""The OAuth `state` parameter, which this flow did not have.

WITHOUT IT THE INSTALL IS CSRF-ABLE. An attacker who can get an admin
to follow a link lands them on Slack's consent screen and then back at
our callback with a code of the attacker's choosing — which we would
exchange and store as the deployment's workspace credentials. The gap
was known and deferred; it is closed here because Phase 3 is the phase
that touches the install flow anyway.

Stored in `platform_integrations.config` rather than a session, for
the same reason every other Slack credential lives there: the
deployment may be several Cloud Run instances, and the instance that
starts the install is usually not the one that handles the callback.

Single-use and short-lived. A state that survives its exchange is a
replay waiting to be found.
"""

from __future__ import annotations

import json
import logging
import secrets
import time

_log = logging.getLogger(__name__)

_KEY = "pending_oauth_state"
TTL_SECONDS = 600          # ten minutes is a generous consent screen


async def _config(pool, platform: str) -> dict:
    row = await pool.fetchrow(
        "SELECT config FROM platform_integrations WHERE platform = $1", platform)
    if row is None:
        return {}
    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg) if cfg else {}
    return dict(cfg or {})


async def _save(pool, platform: str, cfg: dict) -> None:
    await pool.execute(
        "UPDATE platform_integrations SET config = $1 WHERE platform = $2",
        json.dumps(cfg), platform)


async def issue(pool, platform: str = "slack") -> str:
    """Mint a state token and remember it. Returns "" if we cannot store it.

    A failure to store must not block an install — it would make a DB
    blip look like a broken integration — but it must not silently
    weaken the check either, so `verify` treats an absent stored state
    as a failure rather than a pass.
    """
    token = secrets.token_urlsafe(32)
    try:
        cfg = await _config(pool, platform)
        cfg[_KEY] = {"token": token, "issued_at": int(time.time())}
        await _save(pool, platform, cfg)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Could not store OAuth state for %s: %s", platform, exc)
        return ""
    return token


async def verify(pool, presented: str, platform: str = "slack") -> bool:
    """True only for the token we issued, within the TTL, once.

    Consumed whether or not it matched: a token that survives a failed
    attempt can be brute-forced at leisure.
    """
    if not presented:
        return False
    try:
        cfg = await _config(pool, platform)
        stored = cfg.pop(_KEY, None)
        await _save(pool, platform, cfg)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Could not read OAuth state for %s: %s", platform, exc)
        return False

    if not isinstance(stored, dict) or not stored.get("token"):
        _log.warning("Slack OAuth callback carried a state we never issued")
        return False
    if int(time.time()) - int(stored.get("issued_at", 0)) > TTL_SECONDS:
        _log.warning("Slack OAuth state expired")
        return False
    return secrets.compare_digest(str(stored["token"]), str(presented))
