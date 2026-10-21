"""Which model an execution runs on. Per profile, not per worker.

WHY THIS EXISTS. `_worker_forever` built ONE adapter at startup —
`get_adapter("gemini")` — and injected it into every leg of every
execution for the life of the process. Both adapters have been built the
whole time, and `adapter_for_model` has a docstring calling itself "THE
ENTRY POINT FOR AN AGENT", and nothing called it. So an agent type could
not run on Anthropic no matter what it declared, and `Profile.model` was
a field the loader read and the validator refused.

THE MODEL IS THE CHOICE; THE PROVIDER IS DERIVED. `registry.provider_of`
maps `claude-sonnet-5` to the anthropic seam and `gemini-3.7-flash` to
the gemini one, so a profile names a model and never a provider. Asking
somebody to keep two facts in step is asking for the pair to drift, and
a model handed to the wrong adapter surfaces as a vendor 404 several
seconds later.

CACHED BY MODEL ID. An adapter is a client and a set of limits, not a
per-leg object, and rebuilding one every leg would re-open a connection
pool eight times a minute under normal concurrency.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_cache: dict[str, object] = {}


class UnrunnableProfile(Exception):
    """A profile names a model nothing can serve.

    Raised at the point of use rather than at load, because resolving a
    model id pulls in the provider SDKs and the profile loader is
    deliberately importable without them — `effect_coverage` and the
    pre-push hook both depend on that staying true.
    """


def adapter_for_profile(profile, default):
    """The adapter this profile's model needs, or the worker's default.

    `default` is what the worker was started with, and an empty
    `profile.model` keeps it — so a profile that expresses no opinion
    behaves exactly as it did before this function existed. That is the
    property worth preserving: nothing about story-coding changes until
    somebody writes a model into its toml.
    """
    wanted = (getattr(profile, "model", "") or "").strip()
    if not wanted:
        return default

    got = _cache.get(wanted)
    if got is not None:
        return got

    try:
        from kernel.harness.models import adapter_for_model

        got = adapter_for_model(wanted)
    except Exception as exc:                                # noqa: BLE001
        # LOUD, AND IT STOPS THE LEG. Falling back to the default would
        # run the work on a model nobody asked for and report success —
        # and a profile that names `claude-opus-5` and quietly gets
        # gemini-flash is a cost and quality difference nobody would see
        # in the journal.
        raise UnrunnableProfile(
            f"profile {getattr(profile, 'kind', '?')}/"
            f"{getattr(profile, 'domain', '?')} declares model {wanted!r} "
            f"and it cannot be served: {exc}") from exc

    _cache[wanted] = got
    logger.info("profile %s/%s runs on %s",
                getattr(profile, "kind", "?"), getattr(profile, "domain", "?"),
                wanted)
    return got


def _adapter_for(model: str, why: str):
    """One cached adapter per model id. Raises if nothing can serve it."""
    got = _cache.get(model)
    if got is not None:
        return got
    from kernel.harness.models import adapter_for_model      # noqa: PLC0415

    got = adapter_for_model(model)
    _cache[model] = got
    logger.info("%s runs on %s", why, model)
    return got


async def adapter_for_execution(conn, execution_id: int, profile, default):
    """The adapter THIS RUN's legs use. A person's choice wins.

    WHY THIS EXISTS, and it is the same shape as the bug the module
    docstring above records. `choice.freeze` wrote the story's or the
    goal's model onto `executions.executor_model` at dispatch, the UI
    wrote both columns, and NOTHING EVER READ THEM -- `pod_main` built
    one adapter from the deployment default and handed it to every leg
    of every run. So a story set to Anthropic was recorded as Anthropic,
    frozen as Anthropic, and executed on the deployment's Gemini, with
    nothing in the journal naming which model answered. `pod_main`'s own
    comment claimed the opposite.

    THE ORDER, most specific first:

        executions.executor_model   a person picked it, for THIS run
        profile.model               the system's opinion about this work
        `default`                   the deployment default

    A profile keeps its model whenever nobody chose one, which is what
    `adapter_for_profile` promises and what makes this safe to add: a
    deployment where no goal or story has ever opened the dropdown
    behaves exactly as it did.

    NEVER FATAL ON THE READ. A database that cannot answer "which model"
    must not end a leg -- the run falls through to the profile and the
    default, which is what it used before the column existed. A model
    that IS named and cannot be served still raises, for the reason
    `adapter_for_profile` gives: running on a model nobody asked for and
    reporting success hides a cost and quality difference.
    """
    from kernel.harness.models.choice import frozen_for      # noqa: PLC0415

    try:
        wanted = await frozen_for(conn, execution_id)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("execution %s: could not read its model choice (%s); "
                       "the profile and the deployment default apply",
                       execution_id, exc)
        wanted = None
    if not wanted:
        return adapter_for_profile(profile, default)
    try:
        return _adapter_for(wanted, f"execution {execution_id}")
    except Exception as exc:                                # noqa: BLE001
        raise UnrunnableProfile(
            f"execution {execution_id} was dispatched with model "
            f"{wanted!r} and it cannot be served: {exc}") from exc


def forget() -> None:
    """For tests. A cached adapter is shared state between them."""
    _cache.clear()
