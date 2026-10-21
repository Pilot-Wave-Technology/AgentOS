"""A namespace refusal must arrive as itself, not as "the pod never started".

WHAT WAS TRUE BEFORE. `promote.settle` asked one question about a
sandbox that is not answering — how long has it been — and had one
budget, `STARTUP_BUDGET_S = 300`. So a pod Kubernetes had REFUSED and a
pod pulling a cold image produced the same two sentences, five minutes
apart: "waiting", then "no answer in 300s (budget 300s)". The second is
actively misleading. Nothing was waited out; the namespace declined the
pod at once and would decline it for ever.

THE TRAP THIS IS LAID AGAINST (D5, 2026-09-17). `admit.SYSTEM_CEILING`
counts EXECUTIONS and is blind to tier. Sixteen `small` executions are
28 vCPU and 104 GiB against a 128 CPU / 512Gi namespace quota —
comfortable. Sixteen `xlarge` are 132 vCPU and 520 GiB — over on BOTH
axes. Nothing admits at `xlarge` today, so this has never fired; the day
something does, the operator is told the wrong thing for five minutes
per pod and goes looking at images, init containers and the registry.

HOW A CONDITION NOBODY CAN REPRODUCE ON A LAPTOP IS TESTED. Four ways,
and none of them is a cluster:

  the READER is a pure function over the shape the API returns
  (`scheduling_verdict`), so the conditions Kubernetes writes can simply
  be handed to it — including the exact `FailedScheduling` and
  `exceeded quota` wordings;

  the PASS is driven with the reader stubbed, which proves the wiring
  that matters — the pass asks, the answer reaches `stall_reason`, the
  budget shortens, and a reason that stopped being true comes back off;

  THE PARTS THAT WOULD MAKE IT A SILENT NO-OP are asserted directly,
  because that is the failure mode this module is most exposed to — a
  feature that cannot be reproduced locally is also a feature whose
  absence cannot be noticed locally. Two of them are real: the driver
  gate (`is_pod`, not the `.name` idiom three neighbouring sweeps use,
  which is always true because no driver defines `.name`), and the
  source of the API client (the driver's own, not a fresh `CoreV1Api()`
  pointed at whatever `Configuration` is the process default);

  the ARITHMETIC is reproducible everywhere. `test_the_ceiling_is_
  priced_against_the_real_quota` multiplies the real tiers by the real
  ceiling and compares them with the quota parsed out of the real
  `gke_cluster.sh`. That is the part that decides whether the refusal
  ever happens, and it needs no cluster at all.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from kernel.conductor import promote
from kernel.conductor.unscheduled import (REFUSED, SCHEDULING_GRACE_S,
                                          UNSCHEDULED, budget_for, classify,
                                          scheduling_verdict, sentence,
                                          why_pending)

# The two sentences Kubernetes actually writes, kept verbatim. The whole
# module turns on telling them apart, so paraphrasing them here would
# test the paraphrase.
QUOTA_MSG = ('pods "exec-42" is forbidden: exceeded quota: '
             "agentos-staging-quota, requested: requests.cpu=8250m, "
             "used: requests.cpu=124, limited: requests.cpu=128")
NO_ROOM_MSG = "0/3 nodes are available: 3 Insufficient cpu."


def _pod(*conditions, phase="Pending"):
    """The shape the Kubernetes client returns, and nothing more."""
    return SimpleNamespace(status=SimpleNamespace(
        phase=phase,
        conditions=[SimpleNamespace(type=t, status=s, reason=r, message=m)
                    for t, s, r, m in conditions]))


# ── the reader ───────────────────────────────────────────────────────

def test_a_quota_refusal_is_read_as_a_refusal_and_keeps_the_numbers():
    verdict = scheduling_verdict(
        _pod(("PodScheduled", "False", "Unschedulable", QUOTA_MSG)))
    assert verdict == (REFUSED, QUOTA_MSG)
    # THE NUMBERS SURVIVE. "which resource, and against what limit" is
    # the only actionable part, and it exists solely in the message
    # Kubernetes wrote — a headline that summarised it would drop it.
    said = sentence(*verdict)
    for actionable in ("requests.cpu", "used", "limited: requests.cpu=128"):
        assert actionable in said, said
    assert "the namespace refused this pod" in said


def test_no_node_yet_is_not_a_refusal():
    # Autopilot has no node pools: Google adds a node to fit an admitted
    # pod and that takes a minute or two, during which a perfectly
    # healthy pod is Unschedulable. Reading this as a refusal would
    # destroy and rebuild pods that were about to start.
    assert scheduling_verdict(
        _pod(("PodScheduled", "False", "Unschedulable", NO_ROOM_MSG))
    ) == (UNSCHEDULED, NO_ROOM_MSG)


def test_a_placed_pod_has_nothing_to_say_and_keeps_the_full_budget():
    # The ordinary case, and the one `STARTUP_BUDGET_S` was measured
    # for: scheduled, and now pulling an image or cloning repositories.
    assert scheduling_verdict(
        _pod(("PodScheduled", "True", None, None),
             ("Ready", "False", "ContainersNotReady", "not ready"))) is None


def test_a_pod_the_scheduler_has_not_reached_is_not_judged():
    # No PodScheduled condition at all: the object exists and nothing
    # has looked at it. SCHEDULING_GRACE_S covers that window; guessing
    # here would report a refusal on every pod's first second.
    assert scheduling_verdict(_pod()) is None
    assert scheduling_verdict(SimpleNamespace(status=None)) is None


def test_an_unrecognised_message_is_given_the_patient_answer():
    # A Kubernetes release that rewords something must not start tearing
    # down healthy pods. UNSCHEDULED is the default for the same reason
    # readiness.py reads an unknown error as "not yet".
    assert classify("something new nobody has seen") == UNSCHEDULED
    assert classify("") == UNSCHEDULED
    assert classify("exceeded quota: whatever") == REFUSED


# ── the budgets ──────────────────────────────────────────────────────

def test_only_a_refusal_shortens_the_budget():
    # ONE number could not tell a refused pod from a cold image, so one
    # of the two got its own. Exactly one: a REFUSAL is a ceiling that
    # waiting does not move, and nothing else here is.
    assert budget_for(None, promote.STARTUP_BUDGET_S) == 300.0
    assert budget_for(REFUSED, promote.STARTUP_BUDGET_S) == 30.0
    assert budget_for(REFUSED, 300.0) < budget_for(UNSCHEDULED, 300.0)


def test_waiting_for_a_node_does_not_shorten_the_budget_at_all():
    """A draft of this gave UNSCHEDULED 120s. That was wrong three ways.

    IT BOUGHT THE OPERATOR NOTHING: the sentence lands on `stall_reason`
    at `SCHEDULING_GRACE_S` whatever the budget is, so a shorter budget
    only destroys the pod sooner — it tells nobody anything.

    IT COULD CANCEL THE THING IT WAS WAITING FOR: `agentos` is GKE
    Autopilot, where the Pending pod IS the scale-up request. Destroying
    it at 120s cancels the node and recreating starts the wait over, so
    a tier whose node takes longer than the budget never converges —
    and the large tiers are exactly what item D5 is about.

    AND THE CASE IT WAS AIMED AT IS NOT FIXED BY ANY NUMBER:
    `p3-cloud-workspace.md` records a live pod that "stays Pending for
    ever" (a `nodeSelector` no Autopilot node carries). That loops at
    120s and loops at 300s; what ends it is a person reading the
    scheduler's sentence.
    """
    assert budget_for(UNSCHEDULED, promote.STARTUP_BUDGET_S) == 300.0
    assert budget_for(UNSCHEDULED, 77.0) == 77.0


def test_a_verdict_may_shorten_a_budget_and_may_never_lengthen_one():
    # `start()` calls promote_one with budget=0 — "this was your one
    # chance", behind a person watching. A table lookup rather than a
    # `min` would silently give that caller thirty seconds of patience
    # it explicitly refused.
    assert budget_for(REFUSED, 0) == 0
    assert budget_for(UNSCHEDULED, 0) == 0
    assert budget_for(UNSCHEDULED, 10.0) == 10.0


# ── who is asked, and with what ───────────────────────────────────────

@pytest.mark.asyncio
async def test_the_question_is_asked_with_the_drivers_own_client():
    """A fresh `CoreV1Api()` is the bug that would have hidden itself.

    The first draft built one from `kernel.drivers.kubernetes._api()`.
    That constructor inherits whatever `Configuration` is the process
    default, and `KubernetesDriver._core_api` is the ONLY thing in this
    tree that calls `load_incluster_config()` — so a bare client works
    only once the driver happens to have built one, and otherwise points
    at `http://localhost`, where `quiet` swallows the failure at WARNING
    and returns None. The whole feature would have been a no-op
    indistinguishable from a cluster with nothing to report, which is
    precisely the failure this module's docstring warns about elsewhere.

    Asserted on the CLIENT THAT GETS USED and not on the import list:
    the module may import whatever it likes, and what matters is which
    object the call is made against.
    """
    used = []

    class _Api:
        def read_namespaced_pod(self, **kw):
            used.append(kw)
            return _pod(("PodScheduled", "False", "Unschedulable", QUOTA_MSG))

    api = _Api()
    driver = SimpleNamespace(WORKSPACE_MODEL="pod", namespace="agentos-staging",
                             _core_api=lambda: api)
    assert await why_pending(driver, "exec-42") == (REFUSED, QUOTA_MSG)
    assert used and used[0]["name"] == "exec-42"
    assert used[0]["namespace"] == "agentos-staging"


@pytest.mark.asyncio
async def test_a_pod_driver_that_cannot_hand_over_a_client_is_not_asked():
    # `getattr`-guarded: several doubles in this tree are pod drivers for
    # create/destroy and nothing else, and an AttributeError inside a
    # gather would be charged to the pod rather than to the double.
    assert await why_pending(
        SimpleNamespace(WORKSPACE_MODEL="pod"), "exec-42") is None


@pytest.mark.asyncio
async def test_a_control_plane_that_cannot_be_reached_costs_nothing():
    # DEGRADING TO THE OLD CLOCK-ONLY BEHAVIOUR IS THE CORRECT FAILURE.
    # `promote_one` calls this with no `gather` around it, so a driver
    # whose config load throws would have failed the promotion of a
    # sandbox that was answering perfectly well. None means "could not
    # say", and the caller already knows what to do with that.
    def _boom():
        raise RuntimeError("kubeconfig is not readable")

    assert await why_pending(
        SimpleNamespace(WORKSPACE_MODEL="pod", _core_api=_boom),
        "exec-42") is None


@pytest.mark.asyncio
async def test_a_driver_with_no_scheduler_is_never_asked():
    # Docker, and every test double. Asked with the `getattr(driver,
    # "name", "") != "kubernetes"` idiom three sweeps in this package
    # use, this would ALSO return None — but for a pod driver, where it
    # must not, since no driver in this tree defines `.name`. The
    # asserts below are what distinguish the working gate from that one.
    assert await why_pending(SimpleNamespace(), "exec-42") is None
    from kernel.workspace.podfs_client import is_pod
    assert is_pod(SimpleNamespace(WORKSPACE_MODEL="pod"))
    assert not is_pod(SimpleNamespace(WORKSPACE_MODEL="local"))
    # Asserted on the GATE and not on the whole file: the module
    # docstring quotes the broken idiom on purpose, to say why it is not
    # used here.
    src = Path("kernel/conductor/unscheduled.py").read_text(encoding="utf8")
    gates = [line.strip() for line in src.splitlines()
             if line.strip().startswith(("if ", "return "))
             and "driver" in line]
    assert any("is_pod(driver)" in g for g in gates), gates
    assert not any('getattr(driver, "name"' in g for g in gates), (
        "the `.name` gate is always true — no driver defines it, so the "
        "code behind it never runs")


# ── the pass ─────────────────────────────────────────────────────────

class _Row(dict):
    pass


class _Conn:
    """One DISPATCHING row, and the statements this pass makes on it."""

    def __init__(self, waited=60.0):
        self.row = {"id": 42, "container_id": "exec-42", "waited": waited,
                    "state": "DISPATCHING", "stall_reason": None,
                    "stalled_at": None, "generation": 3}
        self.sql: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return [_Row(self.row)]

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return _Row(self.row)

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return None

    async def execute(self, sql, *args):
        self.sql.append(sql)
        if "SET stall_reason = NULL, stalled_at = NULL" in sql:
            # THE DOUBLE APPLIES THE `LIKE ANY` RATHER THAN IGNORING IT.
            # The filter is the whole safety of `unnote` — it is what
            # stops the clear from erasing a dispatch failure — so a
            # double that cleared unconditionally would pass the test
            # that exists to prove it does not.
            reason = self.row["stall_reason"] or ""
            if any(reason.startswith(pat[:-1]) for pat in args[2]):
                self.row["stall_reason"] = None
            return "UPDATE 1"
        if "SET stall_reason = $2" in sql:
            self.row["stall_reason"] = args[1]
        if "SET container_id = NULL" in sql:
            self.row["stall_reason"] = args[1]
            self.row["container_id"] = None
        return "UPDATE 1"


class _Driver:
    """A pod substrate that never answers — the case under test."""

    WORKSPACE_MODEL = "pod"

    def __init__(self):
        self.destroyed: list[str] = []

    async def resolve_bridge_port(self, container_id):
        return 9201

    async def await_ready(self, container_id, timeout=None, bridge_port=None):
        raise TimeoutError(f"{container_id} is not answering yet")

    async def destroy(self, container_id, stop_timeout=10):
        self.destroyed.append(container_id)


def _answers(monkeypatch, verdict):
    """Stand in for the one call that needs a control plane."""
    asked = []

    async def _why(driver, container_id):
        asked.append(container_id)
        return verdict

    monkeypatch.setattr(promote, "why_pending", _why)
    return asked


@pytest.mark.asyncio
async def test_a_refused_pod_says_so_on_the_row_long_before_the_budget(
        monkeypatch):
    # §7b's invariant — "nothing waits without a stall_reason" — applied
    # to the loop that did not have it. Before this the row held NULL
    # for the whole 300 seconds and the only record was an in-memory
    # action nobody keeps.
    conn, driver = _Conn(waited=20.0), _Driver()
    _answers(monkeypatch, (REFUSED, QUOTA_MSG))
    actions = await promote.promote_ready(conn, driver)

    assert [a["action"] for a in actions] == ["waiting"]
    assert "the namespace refused this pod" in conn.row["stall_reason"]
    assert "limited: requests.cpu=128" in conn.row["stall_reason"]
    assert "limited: requests.cpu=128" in actions[0]["detail"]
    assert driver.destroyed == [], "20s in, this is still a wait"


@pytest.mark.asyncio
async def test_the_row_is_not_reclaimed_on_the_clock_it_used_to_be(
        monkeypatch):
    # 60 seconds: under the old single budget the row sits for another
    # four minutes and is then reported as "no answer in 300s". A
    # refusal is decided at once, so the pod stops squatting on the
    # quota it is queued for — and the reason travels with it.
    conn, driver = _Conn(waited=60.0), _Driver()
    _answers(monkeypatch, (REFUSED, QUOTA_MSG))
    actions = await promote.promote_ready(conn, driver)

    assert [a["action"] for a in actions] == ["reclaimed"]
    assert driver.destroyed == ["exec-42"]
    assert "exceeded quota" in conn.row["stall_reason"]
    assert "no answer in" not in conn.row["stall_reason"], (
        "the clock is not the reason and must not be reported as one")


@pytest.mark.asyncio
async def test_a_pod_waiting_for_a_node_is_given_longer_than_a_refused_one(
        monkeypatch):
    # Same 60 seconds, different verdict, opposite action. Autopilot is
    # adding a node and destroying the pod would restart that wait.
    conn, driver = _Conn(waited=60.0), _Driver()
    _answers(monkeypatch, (UNSCHEDULED, NO_ROOM_MSG))
    actions = await promote.promote_ready(conn, driver)

    assert [a["action"] for a in actions] == ["waiting"]
    assert driver.destroyed == []
    assert "no node has room" in conn.row["stall_reason"]


@pytest.mark.asyncio
async def test_a_placed_pod_still_gets_the_full_five_minutes(monkeypatch):
    # The regression this must not cause. An init container cloning a
    # monorepo is the case STARTUP_BUDGET_S was measured for, and a
    # shorter budget for everybody would turn a slow clone into a
    # rebuild loop.
    conn, driver = _Conn(waited=200.0), _Driver()
    _answers(monkeypatch, None)                      # scheduled; no verdict
    actions = await promote.promote_ready(conn, driver)
    assert [a["action"] for a in actions] == ["waiting"]
    assert driver.destroyed == [] and conn.row["stall_reason"] is None
    assert promote.STARTUP_BUDGET_S == 300.0


@pytest.mark.asyncio
async def test_a_pod_younger_than_the_grace_is_not_asked_about(monkeypatch):
    # One API call per unready row per tick, for a condition every pod
    # passes through in its first second, is a cost and a false alarm.
    conn, driver = _Conn(waited=SCHEDULING_GRACE_S - 1), _Driver()
    asked = _answers(monkeypatch, (REFUSED, QUOTA_MSG))
    await promote.promote_ready(conn, driver)
    assert asked == [], "asked about a pod the scheduler has not seen yet"


@pytest.mark.asyncio
async def test_the_wait_clock_is_not_reset_by_writing_the_reason():
    # THE TRAP THIS WHOLE CHANGE COULD HAVE WALKED INTO. The pass
    # measures the wait as `now() - updated_at`. `note` runs every tick
    # for as long as the pod is Pending, so stamping `updated_at` — the
    # thing every other write in this file does — would hold `waited`
    # near zero for ever and no budget would ever expire.
    from kernel.conductor import promote_row
    conn = _Conn()
    await promote_row.note(conn, 42, "exec-42", "because")
    note_sql = [s for s in conn.sql if "stall_reason = $2" in s]
    assert note_sql and "updated_at" not in note_sql[0], note_sql
    # And level-triggered: the same sentence is not rewritten each tick.
    assert "IS DISTINCT FROM" in note_sql[0]


@pytest.mark.asyncio
async def test_a_promoted_row_does_not_keep_the_excuse(monkeypatch):
    # A run that is RUNNING while the board still explains why it could
    # not start is worse than no explanation. Cleared in the same
    # statement as the CAS, so the two can never disagree.
    from kernel.conductor import promote_row
    conn = _Conn()
    conn.fetchval = _cas(conn)
    await promote_row.claim(conn, 42, "exec-42", None)
    cas = [s for s in conn.sql if "SET state = 'RUNNING'" in s][0]
    assert "stall_reason = NULL" in cas and "stalled_at = NULL" in cas


def _cas(conn):
    async def _fetchval(sql, *args):
        conn.sql.append(sql)
        return 3
    return _fetchval


@pytest.mark.asyncio
async def test_a_dead_sandbox_is_not_asked_why_it_has_not_been_scheduled(
        monkeypatch):
    # GONE is a verdict, not a delay: `settle` reclaims the row before it
    # ever looks at `why`. Asking anyway spends a round trip whose answer
    # is discarded, and the answer is a 404 that `quiet` reports at
    # WARNING — a warning on the ordinary path is how a log stops being
    # read. The first draft filtered `!= READY`, which included this.
    from kernel.conductor import readiness

    async def _gone(driver, container_id, timeout=None):
        return readiness.GONE, "pod exec-42 reached Failed before ready"

    monkeypatch.setattr(promote, "probe", _gone)
    conn, driver = _Conn(waited=60.0), _Driver()
    asked = _answers(monkeypatch, (REFUSED, QUOTA_MSG))
    actions = await promote.promote_ready(conn, driver)

    assert [a["action"] for a in actions] == ["reclaimed"]
    assert asked == [], "asked the control plane about a pod that is gone"


@pytest.mark.asyncio
async def test_a_reason_that_stopped_being_true_comes_off_the_row(
        monkeypatch):
    # The pod was Unschedulable, said so, and has now been PLACED — it is
    # pulling an image for the next two minutes. `claim` clears the note,
    # but only at RUNNING, so without this the board reads "no node has
    # room for this pod yet" about a pod that has a node.
    conn, driver = _Conn(waited=60.0), _Driver()
    _answers(monkeypatch, (UNSCHEDULED, NO_ROOM_MSG))
    await promote.promote_ready(conn, driver)
    assert "no node has room" in conn.row["stall_reason"]

    _answers(monkeypatch, None)                      # scheduled now
    await promote.promote_ready(conn, driver)
    assert conn.row["stall_reason"] is None, (
        "a run that is no longer refused must stop saying it is")


@pytest.mark.asyncio
async def test_it_only_takes_back_the_sentences_it_wrote(monkeypatch):
    # A DISPATCHING row can carry a dispatch failure or a contract
    # refusal. A blanket `stall_reason = NULL` here would erase those
    # every tick — silencing two other loops to fix the silence in this
    # one. The patterns are generated from the headlines `sentence()`
    # writes, so the match cannot drift from what was written.
    from kernel.conductor import unscheduled
    conn, driver = _Conn(waited=60.0), _Driver()
    conn.row["stall_reason"] = "dispatch failed: ApiException: (403)"
    _answers(monkeypatch, None)
    await promote.promote_ready(conn, driver)
    assert conn.row["stall_reason"] == "dispatch failed: ApiException: (403)"

    assert all(p.endswith(":%") for p in unscheduled.NOTE_PATTERNS)
    for verdict in (REFUSED, UNSCHEDULED):
        written = sentence(verdict, "anything")
        assert any(written.startswith(p[:-1])
                   for p in unscheduled.NOTE_PATTERNS), written


@pytest.mark.asyncio
async def test_nothing_is_written_about_a_pod_nobody_has_asked_about():
    # The clear is gated on the same grace as the question. Below it
    # there is no note to take back, and an UPDATE per waiting row per
    # tick on every Docker run is a cost with no reader.
    conn, driver = _Conn(waited=SCHEDULING_GRACE_S - 1), _Driver()
    await promote.promote_ready(conn, driver)
    assert not [s for s in conn.sql if "stall_reason" in s]


# ── the arithmetic that decides whether any of this ever fires ───────

def test_the_ceiling_is_priced_against_the_real_quota():
    """The worst load the POOLS can serve, against the quota.

    THE MODEL CHANGED WITH THE ARCHITECTURE, and the old one would now
    mislead. It priced SYSTEM_CEILING x the largest tier -- sixteen
    `large` stories at once -- which was the right question while tiers
    were packed several to a node and any mix was reachable. Under
    one-story-per-machine it is not reachable and never will be: a story
    occupies a whole machine, and scripts/infra/gke_standard_pools.sh
    caps `large` at four of them. Sixteen large stories is not a load to
    protect against, it is a load the cluster cannot express.

    So the pools are the capacity control and this quota is a backstop
    against a bug. What has to hold is that the backstop sits ABOVE the
    worst legitimate load -- otherwise it refuses real work, and it
    refuses it by leaving pods Pending, which reads as slowness.

    Parsed from both scripts rather than copied, so changing a pool
    maximum or the quota moves this test with it. That is how admit.py's
    own cost comment went stale once already.
    """
    from kernel.conductor.admit import SYSTEM_CEILING
    from kernel.drivers.k8s_sqlproxy import CPUS, MEMORY_MB
    from kernel.sizes import SIZES

    quota = Path("scripts/infra/gke_cluster.sh")
    pools = Path("scripts/infra/gke_standard_pools.sh")
    if not (quota.exists() and pools.exists()):        # pragma: no cover
        pytest.skip("the infra scripts are not in this checkout")

    text = quota.read_text(encoding="utf8")
    quota_cpu = float(re.search(r'requests\.cpu:\s*"(\d+)"', text).group(1))
    quota_gib = float(re.search(r"requests\.memory:\s*(\d+)Gi",
                                text).group(1))

    # `pool <name> <machine> <min> <max>`. `platform` is excluded: it
    # holds the conductor and the verifier's Deployment, not stories.
    #
    # THE MIN IS \S+ AND NOT \d+ because it stopped being a literal:
    # next keeps one warm node and every other environment floors at
    # zero, so `small` passes "$SMALL_MIN". Only the MAXIMUM bounds the
    # worst legitimate load this test prices, so the floor being a
    # variable is not something it should care about -- and caring cost
    # it the whole `small` row, which failed as "every tier needs a
    # pool to land on".
    capacity = {m.group(1): int(m.group(2)) for m in re.finditer(
        r"^pool\s+(\w+)\s+\S+\s+\S+\s+(\d+)\s*$",
        pools.read_text(encoding="utf8"), re.M)}
    capacity.pop("platform", None)
    assert set(capacity) == set(SIZES), (
        f"every tier needs a pool to land on; tiers={sorted(SIZES)} "
        f"pools={sorted(capacity)}")

    proxy_cpus = float(CPUS.rstrip("m")) / 1000

    # The worst legitimate load: fill from the most expensive tier down,
    # bounded by each pool's own maximum, until the admission ceiling is
    # reached. Anything beyond this is a bug, which is what a backstop
    # is for.
    budget, worst_cpu, worst_gib = SYSTEM_CEILING, 0.0, 0.0
    for name in sorted(SIZES, key=lambda n: -SIZES[n].pod_cpus):
        size = SIZES[name]
        taken = min(budget, capacity[name])
        budget -= taken
        worst_cpu += taken * (size.pod_cpus + proxy_cpus)
        worst_gib += taken * (size.pod_memory_mb + MEMORY_MB) / 1024

    assert worst_cpu <= quota_cpu, (
        f"the pools can legitimately ask for {worst_cpu:.1f} vCPU and "
        f"the namespace quota allows {quota_cpu}. The excess does not "
        f"fail -- it leaves pods Pending, which reads as the fleet being "
        f"slow. Raise requests.cpu in gke_cluster.sh, or lower a pool "
        f"maximum in gke_standard_pools.sh.")
    assert worst_gib <= quota_gib, (
        f"the pools can legitimately ask for {worst_gib:.0f} GiB and the "
        f"namespace quota allows {quota_gib}. See the CPU message above "
        f"for why this is worse than an error.")

    # AND THE DEFAULT TIER MUST FIT AT THE CEILING, which is the one part
    # of the old model that still holds: `small` is what an unconfigured
    # story gets, and sixteen of them IS reachable -- the small pool goes
    # to twenty.
    small = SIZES["small"]
    at_ceiling = SYSTEM_CEILING * (small.pod_cpus + proxy_cpus)
    assert at_ceiling <= quota_cpu, (
        f"the DEFAULT tier no longer fits the namespace quota at "
        f"SYSTEM_CEILING={SYSTEM_CEILING}: {at_ceiling:.1f} vCPU of "
        f"{quota_cpu}. Admission would fill the namespace and pods "
        f"would be refused rather than run.")
