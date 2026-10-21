"""Does the store hold what the manifest says it holds?

THE GAP THIS CLOSES. An `artifact` criterion was answered entirely from
`kernel.artifact_manifest`: a row meant a pass. A row is a claim that
something was emitted, and between 2026-09-18 and 2026-09-20 that claim
was false for every artifact on the cluster -- the leg ran in a pod, put
its bytes on a PVC that died with the pod, and wrote the row anyway.
Three stories reported "satisfied by 1 artifact(s)" for objects nobody
could open. Nothing in the platform noticed until a later story was
granted one and its workspace build failed.

WHO IS ASKED, and it is not this process. The check runs on the WORKER,
whose binding is ARTIFACT_STORE=filesystem rooted on its own workspace.
Since `emit` now stores through the control panel, the bytes are in the
bucket and NOT on that disk -- a local `exists()` would answer False for
every correctly stored artifact and fail every story. So where there is
a broker, the broker answers; the panel holds the credential and wrote
the bytes in the first place.

WHERE THERE IS NO BROKER -- E0, a laptop, a test -- the local store IS
the durable one and answers for itself, exactly as it does on the write
side.

`Unanswerable` IS NOT `missing`. A store that cannot be reached tells
you nothing, and a check that cannot say anything is an `error`, never a
fail. Turning an outage into a refused story would be a worse bug than
the one above: it would reject work that was actually delivered.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class Unanswerable(Exception):
    """No store could be asked, so nothing may be concluded."""


async def missing(artifact_ids: list, execution_id: int) -> list:
    """Those ids the artifact store cannot produce.

    Raises `Unanswerable` when no store can be consulted at all.
    """
    ids = [str(a).strip() for a in artifact_ids if str(a).strip()]
    if not ids:
        return []

    from kernel.workspace import broker, podfs_auth

    url = broker.endpoint(broker.ARTIFACT_EXISTS, "ARTIFACT_EXISTS_URL")
    # `in os.environ` rather than `configured_token()`: absence here is
    # not an error, it is the E0 shape, and the branch below is correct
    # for it. B1 bans the defaulted `.get`.
    token = (os.environ[podfs_auth.ENV_VAR]
             if podfs_auth.ENV_VAR in os.environ else "")
    if url and token:
        return await asyncio.to_thread(_ask_panel, url, token, ids,
                                       execution_id)

    from kernel.artifacts.store import BINDING, describe

    if BINDING == "filesystem" and "AGENTOS_EXECUTION_ID" in os.environ:
        raise Unanswerable(
            f"this process can only see {describe()}, which is this "
            f"pod's own disk -- the artifacts live in the control "
            f"panel's store and it cannot be reached from here. Needs "
            f"CONTROL_PANEL_URL and {podfs_auth.ENV_VAR}")

    from kernel.artifacts.aio import aexists

    out = []
    for artifact_id in ids:
        try:
            present = await aexists(artifact_id)
        except Exception as exc:                            # noqa: BLE001
            raise Unanswerable(
                f"asking {describe()} about {artifact_id[:12]}: "
                f"{exc}") from exc
        if not present:
            out.append(artifact_id)
    return out


def _ask_panel(url: str, token: str, ids: list, execution_id: int) -> list:
    """One round trip for the whole set. Synchronous, run in a thread."""
    query = urllib.parse.urlencode(
        [("execution_id", str(execution_id))] + [("id", i) for i in ids])
    req = urllib.request.Request(
        f"{url}?{query}",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise Unanswerable(
            f"the control panel could not say which artifacts exist "
            f"({exc.code}): {detail}") from exc
    except Exception as exc:                                # noqa: BLE001
        raise Unanswerable(
            f"could not reach the control panel to check the "
            f"artifacts: {exc}") from exc
    if "missing" not in answer:
        # A SHAPE IT DID NOT PROMISE. Reading a missing key as "nothing
        # missing" is how this class of bug is written in the first
        # place.
        raise Unanswerable(
            "the control panel's answer carried no `missing` field, so "
            "it cannot be read as a result")
    return [str(a) for a in answer["missing"]]
