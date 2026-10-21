"""The kernel's prompts, all the way to the agents already running.

THE PROPAGATION GAP. `kernel.assistant.prompts.sync_all` reconciles the
on-disk AGENTS.md / SOUL.md into `template_files` on every boot, and
stops there. `template_files` is read when an agent is CREATED, so:

    a new goal          gets the current prompt
    a live GO/assistant gets the prompt it was created with, for ever

Which means every prompt fix written during a build reaches exactly the
goals created after it — and the goal being tested, which is always an
existing one, keeps the old text. Observed repeatedly on KER25: prompt
sections added to teach a newly-built capability, and the live agent
behaving as though they did not exist. They did not, for it.

`app/services/template_sync.push_template_file_to_instances` already
knows how to push a file to every live instance of a template, including
instances of a per-user template whose PARENT is the kernel one. Nothing
called it for the kernel's own prompts.

WHY THIS IS APP-SIDE AND NOT IN `sync_all`. `kernel/` must never import
from `app/` — E0's container ships `kernel/` and no `app/` at all — so
the kernel can reconcile the database and only the control panel can
reach the gateway. The two halves meet here.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# The kernel-authored templates and where their files live. Mirrors
# `kernel.assistant.prompts.SYNCED_TEMPLATES`, read from it rather than
# restated so a third kernel template is one row there, not two.
# The platform_settings row that decides whether this runs at boot.
# ABSENT MEANS OFF, which is how it is switched off without a migration
# -- `platform_settings` reads a missing row as False.
#
# WHY OFF. On this gateway `session.reset` is idle/86400, so a session
# never rolls over on its own, so a pushed prompt is read by nothing
# until the gateway process restarts. Running this every boot wrote
# identical bytes, logged nothing useful, and churned the mtimes of the
# files a person reads to find out who wrote them last. The deliberate
# push lives at /settings/agents/refresh, which also does tools and
# skills and tells you to restart.
#
# WORTH SWITCHING BACK ON IF unplanned gateway restarts are common --
# an openclaw deploy done for its own reasons activates whatever is on
# disk, and this is what keeps that current without anybody remembering.
RESYNC_ON_BOOT_KEY = "kernel_prompt_resync_on_boot"

FILES = ("AGENTS.md", "SOUL.md")


def _templates() -> tuple:
    from kernel.assistant.prompts import SYNCED_TEMPLATES

    return tuple(t for t, _root in SYNCED_TEMPLATES)


async def resync(conn, *, force: bool = False, gateway=None) -> dict:
    """Reconcile the prompts from disk, then push what moved to live agents.

    `force` pushes every file regardless of whether this boot changed it
    — which is what a run needs after the files were reconciled by some
    OTHER process (the E0 assistant-api does it too), leaving the
    database already correct and the live agents still stale. Without it
    the common case is "nothing changed, so nothing was pushed", and the
    agent that needed the update is precisely the one that does not get
    it.

    Never raises. A prompt that could not be pushed leaves the agent on
    its previous text, which is serviceable; a control panel that will
    not start is not.
    """
    from kernel.assistant.prompts import sync_all

    out: dict = {"reconciled": {}, "pushed": [], "failed": []}
    try:
        out["reconciled"] = await sync_all(conn)
    except Exception:                                       # noqa: BLE001
        _log.exception("kernel prompt reconcile failed; DB copy left as-is")
        return out

    for template_id in _templates():
        changed = set((out["reconciled"].get(template_id) or {})
                      .get("changed_files") or ())
        for filename in FILES:
            if not force and filename not in changed:
                continue
            content = await conn.fetchval(
                "SELECT content FROM template_files "
                "WHERE template_id = $1 AND filename = $2",
                template_id, filename)
            if not content:
                continue
            try:
                await _push(template_id, filename, content, gateway=gateway)
                out["pushed"].append(f"{template_id}/{filename}")
            except Exception as exc:                        # noqa: BLE001
                _log.warning("kernel prompt push %s/%s failed: %s",
                             template_id, filename, exc)
                out["failed"].append(f"{template_id}/{filename}")

    if out["pushed"] or out["failed"]:
        _log.info("kernel prompt resync: pushed=%s failed=%s",
                  out["pushed"], out["failed"])
    return out


async def _push(template_id: str, filename: str, content: str,
                gateway=None) -> None:
    from app.services.template_sync import push_template_file_to_instances

    await push_template_file_to_instances(
        template_id, filename, content, gateway=gateway)


async def live_targets() -> list:
    """Which agents a resync would actually reach.

    THE POINT OF THIS FUNCTION is that "I pushed the prompt" and "the
    agent has the prompt" are different claims, and the second one needs
    a list. A push reporting success against zero instances is the
    failure mode that reads exactly like a success.
    """
    from app.services.template_sync import _live_instances_for_template

    out = []
    for template_id in _templates():
        try:
            for inst in await _live_instances_for_template(template_id):
                out.append({"template": template_id,
                            "gateway_agent_id": inst.get("gateway_agent_id"),
                            "goal_id": inst.get("goal_id")})
        except Exception as exc:                            # noqa: BLE001
            _log.warning("cannot list instances of %s: %s", template_id, exc)
    return out
