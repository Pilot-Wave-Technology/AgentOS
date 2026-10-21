"""The artifact store's five methods, off the event loop.

WHY THIS MODULE EXISTS. The store's interface is synchronous, and under
`FilesystemStore` that is free — a local read blocks for microseconds
and nobody notices. Bound to a bucket, every one of those calls is a
network round trip, and the worker is a single event loop running every
leg in the process. One `get_store().get(id)` on a 50 MB artifact stops
every other leg for the length of the download: not slowed, STOPPED,
because nothing else on that loop can run until it returns.

That failure is also the hardest kind to attribute. It does not appear
as an error, or in the logs of the leg that caused it. It appears as
other legs missing their heartbeat interval and a tick that took eleven
seconds, in a run that did nothing unusual.

THE INTERFACE STAYS SYNCHRONOUS, and this is a facade rather than a
rewrite, for two reasons. `app/routers/artifacts.py` and the control
panel call the store from places where a thread is the right answer
anyway, and making `put` a coroutine would force every caller to change
whether or not it was on a loop — including the ones that are not.

USE THIS FROM ANY `async def`. Use `get_store()` directly only from
synchronous code.
"""

from __future__ import annotations

import asyncio

from kernel.artifacts.store import get_store


async def aput(data: bytes) -> str:
    """Store bytes, return the sha256 id."""
    return await asyncio.to_thread(get_store().put, data)


async def aexists(artifact_id: str) -> bool:
    return await asyncio.to_thread(get_store().exists, artifact_id)


async def aget(artifact_id: str) -> bytes:
    """The whole artifact. Prefer `aread_range` for anything a person
    will read: the cap is 256 MiB and the loop thread is shared."""
    return await asyncio.to_thread(get_store().get, artifact_id)


async def aread_range(artifact_id: str, offset: int = 0,
                      length: int | None = None) -> bytes:
    return await asyncio.to_thread(
        get_store().read_range, artifact_id, offset, length)


async def asize(artifact_id: str) -> int:
    return await asyncio.to_thread(get_store().size, artifact_id)
