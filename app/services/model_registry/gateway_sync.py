"""Push live catalog/provider state to the OpenClaw gateway.

Two independent gateway-facing actions live here, both soft-fail (a DB
write always lands regardless of gateway reachability — the gateway's own
5-minute self-heal poll is the fallback either way):

  * `reload_gateway_model_catalog` — tells the `model-registry` plugin to
    re-pull `GET /internal/model_catalog` immediately instead of waiting
    for its `REFRESH_INTERVAL_MS` (5 min) self-heal poll.
  * `sync_provider_auth_configs_to_gateway` — merges every synthesized
    provider's connection info directly into the gateway's persisted
    `openclaw.json` (`models.providers.*`) via `config.patch`.

The second one is necessary, not cosmetic. The model-registry plugin's
`resolve_providers` hook only feeds `models.json` (the per-agent model
*availability* list `models-config.plan.ts` builds) — it never writes
back into the gateway's persisted config. But `model-auth.ts`'s API-key
resolution (`getCustomProviderApiKey` / `resolveProviderConfig`) reads
`cfg.models.providers` from that SAME persisted config directly, a
completely separate structure the dynamic catalog poll never touches.
Without this, a dynamically-registered provider can become "known" to
the gateway (a model resolves, no more `Unknown model` error) while
still being fully unusable (`No API key found for provider ...`) —
confirmed live: a synthesized provider was absent from `openclaw.json`'s
`models.providers` even after `models.reload` succeeded and the catalog
endpoint served the corrected payload.

`config.patch` applies a JSON merge patch (additive per-key, not a
replace) — mirrors exactly what `ensure-gateway-models.mjs` does at
gateway boot for the static `models-synthetic.json` providers, just live
instead of boot-time only. Requires a fresh `baseHash` from `config.get`
each call (optimistic concurrency) since any cached value would already
be stale.

Both callers of these functions (the admin CRUD routes in
`app/routers/model_registry.py`, and `go_agent.py::create_go_for_goal`
right before its first `agents.create`) need the exact same two-step
sync, hence this shared module rather than duplicating it.
"""
import asyncio
import copy
import json
import logging
import re

from app.database import get_pool
from app.openclaw import GatewayError, gateway_admin
from app.services.model_registry.emitter import build_providers_payload
from app.services.model_registry.management import (
    set_models_gateway_reachability_v2,
    set_provider_credential_status_v2,
)
from app.services.model_registry.queries import (
    fetch_active_base_models,
    fetch_all_provider_owner_ids,
    fetch_native_provider_configs,
)

_log = logging.getLogger(__name__)


async def _persist_credential_report(response: dict) -> None:
    """Relay the gateway's credential/model-reachability report (from a
    `models.reload` response) into `model_providers`/`models` — AgentOS
    never resolves anything itself, only records what the gateway says.

    Tolerant of an old/un-extended plugin response that has neither
    `credentials` nor `modelReachability` keys at all (nothing to do,
    not an error) — this lets the AgentOS-side and gateway-side halves
    of this feature ship independently without one breaking on the
    other's absence.
    """
    pool = get_pool()
    credentials = response.get("credentials") or {}
    for provider_id, status in credentials.items():
        try:
            await set_provider_credential_status_v2(
                pool, provider_id,
                healthy=bool(status.get("healthy")),
                error=status.get("error"),
            )
        except Exception as exc:
            _log.warning(
                "Failed to persist credential status for provider=%s: %s",
                provider_id, exc,
            )

    reachability = response.get("modelReachability") or {}
    if reachability:
        try:
            await set_models_gateway_reachability_v2(pool, reachability)
        except Exception as exc:
            _log.warning("Failed to persist model reachability report: %s", exc)


async def reload_gateway_model_catalog() -> None:
    """Trigger the gateway's `models.reload` RPC and relay any
    credential/model-reachability report in its response. Soft-fail.
    """
    try:
        response = await gateway_admin.call("models.reload", {})
        _log.info("Gateway models.reload OK")
        await _persist_credential_report(response or {})
    except Exception as exc:
        _log.warning(
            "Gateway models.reload failed: %s — DB updated, will take "
            "effect on next 5-minute poll", exc,
        )


_SENSITIVE_PROVIDER_FIELDS = ("apiKey", "headers")  # both registered `sensitive`
# in agent_os_openclaw's zod-schema.core.ts:237,243 — config.get redacts both to
# "__OPENCLAW_REDACTED__", so comparing them against our freshly-emitted
# plaintext values would never match and the skip below would never fire.

# The exact per-model fields emitter.build_providers_payload emits (id, name,
# upstreamId, contextWindow, plus _capability_fields's reasoning/input) — see
# that function's own docstring. Once the gateway accepts a config.patch, it
# backfills every model entry with its own defaults (api, cost, maxTokens)
# that we never emit and never asked for. Found 2026-08-07: comparing full
# model dicts against the live (backfilled) ones meant this side of the
# comparison NEVER matched — every 5-minute self-heal tick took the write
# path and restarted the gateway, in every environment, since F1 landed.
# Project each live model dict down to only the fields we actually emit
# before comparing, so gateway-added defaults don't count as a diff.
_EMITTED_MODEL_FIELDS = frozenset(
    {"id", "name", "upstreamId", "contextWindow", "reasoning", "input"},
)


def _comparable_provider(provider: dict) -> dict:
    """Provider entry minus the fields `config.get` redacts, so a live
    (redacted) entry can be meaningfully compared against a freshly-emitted
    (plaintext) one. See F1 in the model-registry cleanup plan for why a
    naive deep-equality check silently never matches without this."""
    result = {k: v for k, v in provider.items() if k not in _SENSITIVE_PROVIDER_FIELDS}
    models = result.get("models")
    if isinstance(models, list):
        result["models"] = [
            {k: v for k, v in m.items() if k in _EMITTED_MODEL_FIELDS}
            if isinstance(m, dict) else m
            for m in models
        ]
    return result


_AGENTOS_PROVIDER_KEY_SUFFIX_RE = re.compile(r"^[a-z0-9]+-\d+[km]$")


def _agentos_owned_provider_keys(live_keys: "set[str]", owner_ids: "set[str]") -> "set[str]":
    """Which `live_keys` (gateway `models.providers` keys) match AgentOS's own
    synthetic-provider naming convention `<owner>-<shorthand>-<size>` — e.g.
    `anthropic-sonnet-1m` for owner `anthropic`, shorthand `sonnet`, size `1m`
    (`synthesizer.synthesize_variant`'s `provider_key = f"{owner}-{shorthand}-{suffix}"`).

    Deliberately conservative: a key only counts as ours if its prefix is an
    EXACT known `model_providers.id` (so a bare built-in key like `anthropic` or
    `google` never matches — no owner-only key can also satisfy the required
    `-<shorthand>-<size>` suffix), and the remainder matches
    `<alnum shorthand>-<digits><k|m>` exactly. This is what makes pruning safe:
    it can only ever recognize keys AgentOS's own emitter could have produced,
    never a gateway built-in or a hand-configured provider that merely happens
    to contain a dash.
    """
    owned = set()
    for key in live_keys:
        for owner in owner_ids:
            prefix = f"{owner}-"
            if key.startswith(prefix) and _AGENTOS_PROVIDER_KEY_SUFFIX_RE.match(key[len(prefix):]):
                owned.add(key)
                break
    return owned


def _stale_agentos_provider_keys(
    live: dict, providers: dict, owner_ids: "set[str]",
) -> "set[str]":
    """AgentOS-managed provider keys present in the gateway's live config that
    this round's payload no longer emits — i.e. the DB row that produced them
    (a `model_context_windows` window, or the base model itself) was removed or
    deactivated since the last sync, and nothing has told the gateway to drop
    the now-orphaned entry. This is what `.update()`-only merging can never
    detect on its own: it can add and overwrite, but a key simply absent from
    `providers` looks identical to a key the emitter never owned in the first
    place. See the module docstring and
    `research/GEMINI_OPAQUE_AGENT_FAILURES_2026-08-16.md`'s "Unrelated defect"
    section for how this was found: hours after a migration removed two
    `model_context_windows` rows, `anthropic-sonnet-500k`/`-1m` were still live
    in the gateway config because nothing ever pruned them.
    """
    return _agentos_owned_provider_keys(set(live), owner_ids) - set(providers)


def _providers_already_current(live: dict, providers: dict) -> bool:
    """True if every provider we'd emit is already present in the gateway's
    live config with the same (non-sensitive) fields. Uses `>=` on the key
    set deliberately — the gateway legitimately holds provider keys we don't
    manage (its own built-ins), so requiring exact set equality would never
    match. A provider *removed* from our registry must still trigger a
    write (shrinking `providers` must not look "already satisfied" just
    because everything we still emit happens to match)."""
    if not set(live) >= set(providers):
        return False
    return all(_comparable_provider(live.get(k) or {}) == _comparable_provider(v)
               for k, v in providers.items())


_STALE_BASE_HASH_MARKER = "re-run config.get and retry"  # substring every
# requireConfigBaseHash rejection ends with (config.ts:73,84,98) — the
# generic error code (INVALID_REQUEST) is shared with unrelated validation
# failures, so matching on this message fragment is the only way to tell
# "the doc moved under us, refetch and retry" apart from a real error.

_MAX_STALE_HASH_RETRIES = 3


def _assert_only_providers_changed(before: dict, after: dict) -> None:
    """Refuse to send a `config.set` payload unless the only thing that
    differs from the live document is `models.providers`. `config.set`
    replaces the ENTIRE document — unlike `config.patch`'s merge, sending
    anything with a stray change elsewhere (a bug in this function, a
    concurrent mutation of the same in-memory dict, ...) would silently
    write that drift permanently. Raising here converts a would-be silent
    config-corruption bug into a loud, caught-and-logged failure instead."""
    keys = set(before) | set(after)
    for key in keys:
        if key == "models":
            before_rest = {k: v for k, v in (before.get("models") or {}).items() if k != "providers"}
            after_rest = {k: v for k, v in (after.get("models") or {}).items() if k != "providers"}
            if before_rest != after_rest:
                raise RuntimeError(
                    "config.set aborted: a key under .models other than 'providers' "
                    "changed unexpectedly — refusing to risk overwriting it",
                )
            continue
        if before.get(key) != after.get(key):
            raise RuntimeError(
                f"config.set aborted: top-level key {key!r} changed unexpectedly "
                "outside models.providers — refusing to send a full-document "
                "replace that could silently corrupt it",
            )


async def sync_provider_auth_configs_to_gateway() -> None:
    """Merge every synthesized provider's connection info into the
    gateway's `openclaw.json` (`models.providers.*`) via `config.set`.
    See module docstring for why this is required. Soft-fail throughout.

    F2: uses `config.set` (whole-document replace, no restart, no rate
    limit) instead of `config.patch` (partial merge, but its gateway-side
    handler ALWAYS schedules a SIGUSR1 restart on any accepted write —
    config.ts:442 — with no no-op branch). F1 already made the write
    conditional (skip when nothing changed, closing the every-5-minute
    self-heal restart storm); F2 removes the restart on the remaining
    case where a provider genuinely changes, so a real model/provider
    edit no longer costs the gateway a restart either.

    `config.set` requires sending the FULL config document — a partial
    send reads as "every omitted key was deleted." So this builds the
    full document by merging only `models.providers` into a fresh copy
    of the live config, then asserts (`_assert_only_providers_changed`)
    that nothing else actually changed before sending, as an enforced
    invariant rather than a convention. Sentinel values for other
    providers' redacted `apiKey`/`headers` pass through untouched — the
    gateway's own `restoreRedactedValues` (config.ts:150, shared by both
    config.set and config.patch) resolves those from disk on write,
    exactly as it already does for config.patch today.

    Retries a bounded number of times on a stale `baseHash` (a concurrent
    goal creation or an overlapping deploy can move the doc between our
    `config.get` and `config.set`) by refetching and rebuilding — not a
    regression, `config.patch` had the same constraint.

    PRUNING (added 2026-08-18): `.update(providers)` on its own can only add or
    overwrite keys, so a provider whose backing DB row was removed (e.g. a
    `model_context_windows` window deleted, or the whole model deactivated)
    stayed live in the gateway config forever — `_providers_already_current`'s
    own docstring already flagged the shrink case as needing a write, but the
    write itself never actually deleted anything. Confirmed live: hours after
    a migration removed two `model_context_windows` rows, the corresponding
    `anthropic-sonnet-500k`/`-1m` gateway providers were still present. Fixed by
    computing `_stale_agentos_provider_keys` — keys that match AgentOS's own
    naming convention (so a gateway built-in is never touched) and are absent
    from this round's payload — and deleting exactly those from `full_config`
    before it is sent.

    KNOWN GAP, not fixed here: if `providers` is empty (every model
    deactivated), this function returns before ever checking for stale keys —
    the early-return below predates pruning and exists to skip a needless
    `config.get` round trip in the common no-op tick. A full wipeout is an
    extreme edge case; if it matters later, check staleness before this
    early-return rather than after.
    """
    try:
        pool = get_pool()
        base_rows = await fetch_active_base_models(pool)
        native_providers = await fetch_native_provider_configs(pool)
        providers = build_providers_payload(base_rows, native_providers)
        if not providers:
            return
        owner_ids = {row["id"] for row in await fetch_all_provider_owner_ids(pool)}

        for attempt in range(1, _MAX_STALE_HASH_RETRIES + 1):
            current = await gateway_admin.call("config.get", {})
            base_hash = current.get("hash")
            if not base_hash:
                _log.warning(
                    "Gateway config.get returned no hash — skipping provider auth sync",
                )
                return

            # Refuse to write against a config the gateway can't parse or
            # validate. `config.get` blanks `config` to {} for ANY invalid
            # snapshot while still returning the real `hash` of the broken
            # file, so without this check the live document looks empty: the
            # comparison below sees nothing current, the write path builds a
            # "full" document from that empty base, and `config.set` replaces
            # openclaw.json with ONLY models.providers — losing gateway.bind,
            # the auth token, agents.defaults, channels, everything.
            #
            # `config.patch` guards this explicitly ("invalid config; fix
            # before patching"); `config.set` does not, so the check has to
            # live here. The trigger is unattended: the 5-minute self-heal
            # keeps calling this, so a hand-edit that leaves a trailing comma
            # would be escalated into a wiped config within one interval.
            # Recoverable from the gateway's own .bak, but silent until noticed.
            #
            # `is False` rather than falsy — an older gateway that doesn't
            # report the field must keep syncing normally.
            if current.get("valid") is False:
                _log.warning(
                    "Gateway reports its config is invalid — skipping provider "
                    "auth sync. Writing now would replace the whole document "
                    "with only models.providers, because config.get blanks an "
                    "invalid config to {}. Fix openclaw.json on the gateway "
                    "first; this will resume on the next tick.",
                )
                return

            live_config = current.get("config") or {}
            live_providers = (live_config.get("models") or {}).get("providers", {}) or {}
            stale_keys = _stale_agentos_provider_keys(live_providers, providers, owner_ids)
            if not stale_keys and _providers_already_current(live_providers, providers):
                _log.debug(
                    "Provider config already current (%d provider(s)) — skipping write",
                    len(providers),
                )
                return

            full_config = copy.deepcopy(live_config)
            full_providers = full_config.setdefault("models", {}).setdefault("providers", {})
            for stale_key in stale_keys:
                full_providers.pop(stale_key, None)
            full_providers.update(providers)
            if stale_keys:
                _log.info(
                    "Pruning %d stale synthetic provider(s) no longer backed by a DB "
                    "row: %s",
                    len(stale_keys), sorted(stale_keys),
                )
            _assert_only_providers_changed(live_config, full_config)

            try:
                await gateway_admin.call(
                    "config.set", {"raw": json.dumps(full_config), "baseHash": base_hash},
                )
                _log.info(
                    "Gateway config.set OK — synced %d synthetic provider(s) into "
                    "models.providers (no restart)",
                    len(providers),
                )
                return
            except GatewayError as exc:
                if _STALE_BASE_HASH_MARKER not in str(exc) or attempt == _MAX_STALE_HASH_RETRIES:
                    raise
                _log.debug(
                    "Gateway config.set: stale baseHash (attempt %d/%d) — refetching and retrying",
                    attempt, _MAX_STALE_HASH_RETRIES,
                )
                await asyncio.sleep(0.5 * attempt)
    except Exception as exc:
        _log.warning(
            "Gateway config.set provider sync failed: %s — DB updated, "
            "provider will be unusable on the gateway until this succeeds", exc,
        )


async def sync_gateway_model_catalog() -> None:
    """Convenience wrapper: reload the catalog, then sync provider auth
    config. Use this from any caller that needs "make the gateway fully
    up to date" in one call.
    """
    await reload_gateway_model_catalog()
    await sync_provider_auth_configs_to_gateway()
