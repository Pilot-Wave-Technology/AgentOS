"""The kernel's prompts have to reach the agents already running.

THE GAP THIS CLOSES. `kernel.assistant.prompts.sync_all` reconciles the
on-disk AGENTS.md / SOUL.md into `template_files` every boot and stops
there. `template_files` is read when an agent is CREATED, so a live GO or
assistant keeps the prompt it was born with, for ever. Every prompt fix
written during a build therefore reached goals created afterwards, and
missed the goal being tested with it — which is always an existing one.

THREE THINGS HAVE TO BE TRUE, and each was false at some point today:

  1. The database matches the repository.        (sync_all — was true)
  2. Every LIVE instance is sent the new file.   (was never done)
  3. The gateway actually re-reads it.           (it does not, by itself)

(3) is the one that looks done and is not. `agent_os_openclaw/src/agents/
bootstrap-cache.ts` caches the bootstrap files PER SESSION KEY in
gateway memory, cleared only on session rollover — so a pushed file sits
correctly on disk while the running agent keeps serving the copy it
loaded at session start. Writing the file is necessary and not
sufficient; the gateway has to be restarted (sessions persist as .jsonl,
so nothing is lost) or the session rolled over.

That is recorded here because it is invisible from our side of the wire:
every push returns success, the file on disk is provably correct, and
the agent still behaves as though the prompt never changed.
"""
from __future__ import annotations

import asyncio
import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


class _Conn:
    """Just enough connection to drive `resync` without a database."""

    def __init__(self, content="# prompt\n"):
        self.content = content

    async def fetchval(self, sql, *args):
        if "template_files" in sql:
            return self.content
        return 1

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        return "UPDATE 1"


def _run(coro):
    return asyncio.run(coro)


# ── the reconcile reports what moved, by name ────────────────────────────

def test_sync_reports_bare_filenames():
    """`changed` carries a human sentence with byte counts in it. The
    push needs the NAMES, and parsing one back out of
    "AGENTS.md (1234b -> 1240b)" works until somebody improves the
    message."""
    src = _read("kernel/assistant/prompts.py")
    assert "changed_files" in src
    assert '"changed_files": changed_files' in src


# ── the push happens, and reaches live instances ─────────────────────────

def test_resync_pushes_every_kernel_template_when_forced():
    from app.services import kernel_prompt_resync as m

    pushed = []

    async def fake_push(template_id, filename, content, gateway=None):
        pushed.append(f"{template_id}/{filename}")

    original = m._push
    m._push = fake_push
    try:
        out = _run(m.resync(_Conn(), force=True))
    finally:
        m._push = original

    assert "go-v2/AGENTS.md" in pushed, "GO's prompt was not pushed"
    assert "kernel-assistant/AGENTS.md" in pushed, (
        "the assistant's prompt was not pushed")
    assert "go-v2/SOUL.md" in pushed and "kernel-assistant/SOUL.md" in pushed
    assert not out["failed"]


def test_a_failed_push_does_not_stop_the_others():
    """One unreachable agent must not cost every other agent its prompt.
    A stale `agent_instances` row whose gateway_agent_id no longer exists
    is ordinary — there is one on KER25 right now."""
    from app.services import kernel_prompt_resync as m

    pushed = []

    async def flaky(template_id, filename, content, gateway=None):
        if filename == "AGENTS.md" and template_id == "go-v2":
            raise RuntimeError("unknown agent id")
        pushed.append(f"{template_id}/{filename}")

    original = m._push
    m._push = flaky
    try:
        out = _run(m.resync(_Conn(), force=True))
    finally:
        m._push = original

    assert "go-v2/AGENTS.md" in out["failed"]
    assert "kernel-assistant/AGENTS.md" in pushed, (
        "one failure stopped the rest")


def test_a_reconcile_failure_is_survivable():
    """An assistant on a stale prompt beats no control panel."""
    from app.services import kernel_prompt_resync as m

    class Broken(_Conn):
        async def fetchval(self, sql, *args):
            raise RuntimeError("database went away")

    out = _run(m.resync(Broken(), force=True))
    assert out["pushed"] == [] and out["reconciled"] == {}


# ── the startup path uses it, and forces ─────────────────────────────────

def test_startup_resyncs_when_switched_on_and_still_forces():
    """THE STARTUP PUSH IS NOW BEHIND A SWITCH, off by default.

    It was unconditional, and on this gateway that achieved nothing
    observable: `session.reset` is idle/86400, so a session never rolls
    over, so a pushed prompt is read by NOTHING until the gateway
    restarts. Every boot rewrote identical bytes and churned the mtimes
    of the files a person reads to work out who wrote them last.

    The code is kept, not deleted -- it is the only thing that makes an
    UNPLANNED gateway restart pick up current prompts. Switch it on from
    /settings/agents/refresh.

    `force=True` still matters WHEN it runs, for the original reason:
    two processes reconcile these files, whichever boots first absorbs
    the change, and "push only what I changed" would then push nothing.
    """
    src = _read("app/main.py")
    assert "kernel_prompt_resync" in src, (
        "startup reconciles template_files and never pushes to live agents")
    # Anchored on the IMPORT, not the first mention: the first mention is
    # prose explaining why the switch exists, and prose grows.
    i = src.index("from app.services.kernel_prompt_resync import")
    block = src[i:i + 1400]
    assert "RESYNC_ON_BOOT_KEY" in block, (
        "the push must be gated on the switch rather than unconditional")
    assert "force=True" in block, (
        "TWO processes reconcile these files -- this one and E0's "
        "assistant-api. Whichever boots first absorbs the change, so "
        "'push only what I changed' means the other pushes nothing and "
        "the live agents keep the old prompt.")


def test_the_template_list_is_not_restated_here():
    """A third kernel template should be one row in
    `SYNCED_TEMPLATES`, not two lists to keep in step."""
    src = _read("app/services/kernel_prompt_resync.py")
    assert "SYNCED_TEMPLATES" in src
    assert '"go-v2"' not in src and "'go-v2'" not in src


def test_the_kernel_half_does_not_import_the_app():
    """E0's container ships `kernel/` and no `app/` at all, so the
    kernel can reconcile the database and only the control panel can
    reach the gateway. That is why this module exists on this side."""
    src = _read("kernel/assistant/prompts.py")
    assert "from app." not in src and "import app" not in src


# ── the part that is invisible from our side ─────────────────────────────

def test_the_gateway_cache_is_recorded_as_a_step():
    """A pushed file sits correctly on disk while the running agent
    serves the copy it loaded at session start. Nothing on our side can
    observe that, so it is written down where the next person will be
    standing when they hit it."""
    src = _read("app/services/kernel_prompt_resync.py")
    assert "live_targets" in src, (
        "nothing lists WHICH agents a push reached; a push reporting "
        "success against zero instances reads exactly like a success")
