"""P3 made STARTUP_BUDGET_S wrong, and the failure is a rebuild loop.

`STARTUP_BUDGET_S = 300` is "how long a PLACED sandbox may take to answer
before it is thrown away and built again", and it was measured against
the TWO init containers that existed when it was written: an image pull
and a clone.

P3 added a third. `workspace-setup` runs `pip install` / `pnpm install`
on the contract's image, and section 12 prices that at up to 1800s --
`JOB_DEFAULT_S`, chosen because "a cold `pnpm install` on a monorepo is
minutes rather than seconds".

So without this: the install crosses 300s, promotion declares the pod
lost, destroys it, dispatch rebuilds it, and the install starts again.
A REBUILD LOOP, and the bigger the repository the more certain it is --
the stories most likely to need a long install are the ones guaranteed
to hit it.

Q1 predicted the shape and could not close it: `PodScheduled=True` is
all a CONDITION says, and telling "cold image" from "wedged after
placement" needs the container statuses. This reads them.
"""
from __future__ import annotations

from kernel.conductor import unscheduled as u
from kernel.conductor.promote import STARTUP_BUDGET_S


class _State:
    def __init__(self, running):
        self.running = object() if running else None


class _Init:
    def __init__(self, name, running):
        self.name = name
        self.state = _State(running)


class _Status:
    def __init__(self, inits):
        self.init_container_statuses = inits
        self.conditions = []


class _Pod:
    def __init__(self, inits):
        self.status = _Status(inits)


def test_a_pod_running_the_setup_container_is_installing():
    assert u.installing(_Pod([_Init(u.SETUP_CONTAINER, True)]))


def test_a_pod_past_the_setup_container_is_not():
    """Finished is not running. A pod whose install COMPLETED and still
    will not answer is genuinely wedged, and must get the short budget
    -- otherwise the fix trades a rebuild loop for a slot held half an
    hour by a pod that is never coming up."""
    assert not u.installing(_Pod([_Init(u.SETUP_CONTAINER, False)]))


def test_a_pod_in_an_earlier_init_container_is_not():
    """A clone is not an install. `workspace-compose` is what 300s was
    measured against and it keeps that number."""
    assert not u.installing(_Pod([_Init("workspace-compose", True)]))


def test_a_pod_with_no_statuses_is_not():
    assert not u.installing(_Pod([]))
    assert not u.installing(_Pod(None))


# ── the budget arithmetic ──────────────────────────────────────────────

def _effective(verdict, default=STARTUP_BUDGET_S):
    return u.budget_for(verdict, u.ceiling_for(verdict, default))


def test_an_installing_pod_gets_the_install_budget():
    """THE FIX. 1860 = JOB_DEFAULT_S plus a minute for the pull and the
    clone ahead of it."""
    assert _effective(u.INSTALLING) == u.SETUP_BUDGET_S
    assert u.SETUP_BUDGET_S > STARTUP_BUDGET_S


def test_an_ordinary_placed_pod_is_unchanged():
    """The number 300 was right for what it was measured against, and
    this must not quietly make every slow pod patient."""
    assert _effective(None) == STARTUP_BUDGET_S


def test_a_refused_pod_is_still_shortened():
    """Order matters: lengthen for the install, then let a refusal
    shorten it again. A pod with no node is running nothing, so refused
    must win over any install ceiling."""
    assert _effective(u.REFUSED) == u.REFUSED_BUDGET_S
    assert u.budget_for(u.REFUSED, u.ceiling_for(u.INSTALLING,
                                                 STARTUP_BUDGET_S)) == \
        u.REFUSED_BUDGET_S


def test_a_caller_who_chose_less_is_not_overruled():
    """THE RULE THIS FIX HAD TO RESPECT. `budget_for`'s docstring is
    explicit that it may only ever shorten, because `promote_one` is
    called from `start` with `budget=0` behind a person pressing a
    button -- "this was your one chance". Lengthening for an install
    must not undo that, which is why `ceiling_for` is a separate
    function with a guard rather than a branch inside `budget_for`."""
    assert _effective(u.INSTALLING, default=0) == 0
    assert _effective(u.INSTALLING, default=30) == 30


def test_the_container_name_matches_the_manifest_builder():
    """Two modules naming one container. The conductor reads a pod it
    did not build, so the name is spelled in both -- and a rename in one
    would silently restore the rebuild loop."""
    import inspect

    from kernel.drivers import k8s_workspace

    src = inspect.getsource(k8s_workspace.setup_container)
    assert f'"name": "{u.SETUP_CONTAINER}"' in src
