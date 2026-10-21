"""The background loops, in one list, owned by one service.

ONE LIST, TWO POSSIBLE HOSTS. `app/main.py` used to start each of these
with its own `create_task` in a 140-line startup block, so "which loops
exist" was a thing you established by reading. Now it is this tuple, and
both the scheduler worker and the control panel read it -- which is what
makes moving a loop between them a one-line change rather than a merge.

WHY THEY MOVED OUT OF THE WEB TIER. Two reasons, both measured:

  CLOUD RUN THROTTLES CPU BETWEEN REQUESTS. A background loop only
  advances while the instance is serving something. The control panel is
  kept warm by browser WebSockets, so these ran fine while somebody was
  watching and stalled overnight -- when reapers and backstops are
  exactly what should be running. The git engine, which has no inbound
  traffic at all, managed 2 ticks in 10 minutes against the same service
  in prod doing 30.

  EVERY INSTANCE RAN EVERY LOOP. maxScale was 100. Coordination was
  per-loop and patchy: some took advisory locks, several took nothing.
  A reaper running in N copies is the shape that deletes a thing twice.

THE TEST FOR WHETHER A LOOP BELONGS HERE: does running two of them do
the work twice, or half each? A reaper does it twice -- it belongs here,
behind one leader. The chat dispatcher does half each: it shards by
thread with an advisory lock and WANTS to be horizontal. It stays in the
web tier, where it is also kept warm for free by the user whose turn it
is serving.

DELIBERATELY LEFT IN THE WEB TIER:
  chat Dispatcher      shards per thread; wants N instances
  unread listener      forwards to that instance's own WebSockets
  security dispatcher  flag-gated, hosted by council-worker
"""
from __future__ import annotations

from app.services.scheduler.supervisor import Loop


def _loops() -> tuple[Loop, ...]:
    """Built in a function so the imports stay lazy.

    Every entry below imports inside its factory, exactly as the
    call sites in main.py did. Importing twenty subsystems at module
    load would make this file's import order load-bearing, and the
    startup block it replaces was careful not to be.
    """
    from app.services import (awaiting_merge_janitor, dag_janitor,
                              slack_membership_reconcile,
                              story_review_delivery, token_refresh)
    from app.services.credential_brokers import watchdog as cred_watchdog
    from app.services.dashboard_refresh import run_loop as dashboard_refresh_loop
    from app.services.kernel_park_cards import run_forever as park_cards

    return (
        Loop("park_cards", park_cards,
             "stall, verdict, question and plan cards into threads"),
        Loop("dag_janitor", dag_janitor.run_loop,
             "rolls up claude-code phase DAGs that stalled silently"),
        # REGISTERED HERE AS WELL AS IN main.py, like `dag_janitor`
        # above, and the pair is the point: an environment with a
        # scheduler beside the panel sets AGENTOS_SCHEDULER_LOOPS=0,
        # and anything registered ONLY in main.py then runs nowhere
        # at all. That is what dev does.
        Loop("awaiting_merge_janitor", awaiting_merge_janitor.run_loop,
             "releases pods and local checkouts held by a merge that "
             "never came"),
        Loop("token_refresh", token_refresh.run_loop,
             "GitHub installation tokens for active operator stories"),
        # A reaper, by the test in this module's docstring: two copies
        # would do the work twice, so it belongs behind one leader.
        Loop("slack_membership_reconcile", slack_membership_reconcile.run_loop,
             "removes people from Slack channels whose thread they are not "
             "in — Slack cannot stop a member inviting a member, so the "
             "rule has to be enforced rather than only set at creation"),
        # THE BACKSTOP FOR A LOST NOTIFY. LISTEN/NOTIFY has no
        # redelivery: if the listening connection is down for the
        # instant the announce fires, the review is never delivered and
        # nothing anywhere records that it was not. A reaper, so one
        # leader runs it.
        Loop("story_review_backstop", story_review_delivery.run_loop,
             "delivers story reviews whose announce was missed, so a "
             "story cannot reach `reviewed` with the GO never told"),
        Loop("credential_watchdog", cred_watchdog.run_loop,
             "revokes broker handles the inline hook missed"),
        # MOVED HERE IN THE TAXIMETER WORK, and it passes this file's own
        # test outright: rebuilding a materialized view in N instances
        # does the work N times, and they contend for the same locks
        # while doing it. It was started with a bare `create_task` in
        # main.py, so every web instance refreshed every view.
        Loop("dashboard_refresh", dashboard_refresh_loop,
             "rebuilds the dashboard materialized views and the gateway "
             "cache on their two cadences"),
    )


# THE ONES THAT MOVED, AND THE ONES THAT HAVE NOT YET.
#
# Four to begin with, chosen because each is self-contained, already
# safe to run alone, and easy to see working: park_cards posts a card,
# dag_janitor rolls up a DAG, token_refresh mints a token,
# credential_watchdog revokes a handle.
#
# The rest stay in `app/main.py` for now and move in a second pass:
# _ia_instance_reaper, _gateway_orphan_reconciler,
# _catalog_sync_loop, _deleted_goal_session_reaper, _local_checkout_reaper,
# _go_provisioning_repair, _model_registry_self_heal_loop,
# _provision_retry_backstop, the platform-council trio,
# _webhook_deliveries_prune_loop, _go_wakes, and branch_ready_listener.
#
# TWO PASSES ON PURPOSE. Moving twenty loops in one change means a
# failure in any of them is a failure in "the change"; moving four means
# a failure names itself. The second pass is cheaper for having done the
# first, because the machinery will already have run in production.
def loops() -> tuple[Loop, ...]:
    return _loops()
