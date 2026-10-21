"""An autonomous errand names what it needs, or it is refused.

FOUND ON KER25. Proposal 903's task read "run pytest against
multi_agent_dummy_repo on branch cal_kernel"; story `ed57dd54` was
created at approval holding NOTHING. `propose_errand` had no `resources`
parameter, and `errand_grants` created the story with a bare INSERT, so
a proposed errand's story was STRUCTURALLY incapable of holding
resources — the run would spend its legs hunting for a checkout nobody
had given it.

This is the failure GO's own prompt warns about — "a story with no
grants has no workspace, so it cannot be started at all and nobody finds
out until someone tries" — except here the second call did not exist to
be forgotten. An autonomous errand's story is created at APPROVAL, by
somebody else, possibly hours later: there is no second call, so the
proposal is the only moment the need can be stated.

THE DISTINCTION THAT MATTERS: omitted is refused, `[]` is allowed. Some
errands genuinely need no workspace (counting stories, reading the
goal's record). A silent default is what produced the bug.
"""
from __future__ import annotations

import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


class _Conn:
    """A goal with one repo, one retired doc, and one cloud identity."""

    ROWS = {
        "repo-1": {"id": "repo-1", "kind": "repo", "name": "calc",
                   "status": "active"},
        "old-1": {"id": "old-1", "kind": "document", "name": "spec.md",
                  "status": "retired"},
        "sa-1": {"id": "sa-1", "kind": "gcp_test_sa", "name": "cf-invoker",
                 "status": "active"},
    }

    async def fetchrow(self, sql, *args):
        return self.ROWS.get(args[0])

    async def fetch(self, sql, goal_id, ref=None):
        """The two queries kernel/go/resource_ref.py asks.

        `validate` now resolves a reference to an id before reading the
        row, so a resource can be named as well as keyed -- the
        catalogue is written by the control panel, which assigns opaque
        ids, while the NAME is what the board and the person use. These
        tests address by id throughout, which must keep working: an
        exact id still wins.
        """
        rows = list(self.ROWS.values())
        if ref is None:
            return [r for r in rows if r["status"] == "active"]
        low = ref.lower()
        hit = [r for r in rows
               if r["id"] == ref or r["name"].lower() == low
               or r["id"].startswith(ref)]
        hit.sort(key=lambda r: (r["id"] != ref, r["name"].lower() != low))
        return hit


def _validate(resources, goal="KER25"):
    import asyncio

    from kernel.go.errand_resources import validate

    return asyncio.run(validate(_Conn(), goal, resources))


# ── omitted is not empty ─────────────────────────────────────────────────

def test_omitting_resources_is_refused():
    from kernel.go.errand_resources import BadResources

    with pytest.raises(BadResources) as e:
        _validate(None)
    assert e.value.code == "E_NO_RESOURCES"
    # The message must say WHY there is no second chance, or the next
    # reader assumes a grant can be added later.
    assert "no second call" in e.value.message


def test_an_empty_list_is_a_real_answer():
    """Counting stories or reading the goal's record needs no workspace.
    Refusing those would push GO to invent a grant to satisfy a rule."""
    assert _validate([]) == []


# ── the shape ────────────────────────────────────────────────────────────

def test_a_good_grant_is_normalised():
    """Case-folded, and a REPO comes back writable.

    `needs` on a repo no longer survives for an errand: with every
    mount read-only `writable_scope_for` returns [] and the write tool
    refuses everywhere, so the errand cannot produce the file
    `emit_artifact` needs. See the upgrade in `errand_resources._one`.
    """
    out = _validate([{"resource_id": "repo-1", "relation": "NEEDS"}])
    assert out[0]["resource_id"] == "repo-1"
    assert out[0]["relation"] == "modifies"
    assert "writable" in out[0]["why"]


def test_a_non_repo_grant_is_normalised_and_left_alone():
    """The upgrade is repos and branches only -- a document has
    nothing to write to, and cloud reach is read-only by rule."""
    out = _validate([{"resource_id": "sa-1", "relation": "NEEDS"}])
    assert out == [{"resource_id": "sa-1", "relation": "needs", "why": ""}]


def test_an_unknown_id_is_refused_at_propose_time():
    """Not at the sweep. A proposal discovered to be unusable by the
    worker pauses a schedule a person already approved, and the refusal
    reaches a log instead of the agent that wrote it."""
    from kernel.go.errand_resources import BadResources

    with pytest.raises(BadResources) as e:
        _validate([{"resource_id": "nope", "relation": "needs"}])
    assert e.value.code == "E_NO_SUCH_RESOURCE"


def test_a_retired_resource_is_refused():
    from kernel.go.errand_resources import BadResources

    with pytest.raises(BadResources) as e:
        _validate([{"resource_id": "old-1", "relation": "needs"}])
    assert e.value.code == "E_RESOURCE_NOT_ACTIVE"


def test_a_bad_relation_is_refused():
    from kernel.go.errand_resources import BadResources

    with pytest.raises(BadResources) as e:
        _validate([{"resource_id": "repo-1", "relation": "reads"}])
    assert e.value.code == "E_BAD_RELATION"


def test_a_write_grant_needs_a_reason():
    """Same refusal `grant_resource` makes, made earlier."""
    from kernel.go.errand_resources import BadResources

    with pytest.raises(BadResources) as e:
        _validate([{"resource_id": "repo-1", "relation": "modifies"}])
    assert e.value.code == "E_NO_REASON"
    assert _validate([{"resource_id": "repo-1", "relation": "modifies",
                       "why": "the fixture lives here"}])


def test_cloud_reach_cannot_be_granted_write():
    """A run ACTS AS an identity; widening it is an IAM change a person
    makes outside this system."""
    from kernel.go.errand_resources import BadResources

    with pytest.raises(BadResources) as e:
        _validate([{"resource_id": "sa-1", "relation": "modifies",
                    "why": "x"}])
    assert e.value.code == "E_CREDENTIAL_IS_NOT_WRITABLE"
    assert _validate([{"resource_id": "sa-1", "relation": "needs"}])


# ── every surface that has to know ───────────────────────────────────────

def test_propose_errand_takes_and_stores_resources():
    src = _read("kernel/go/errands.py")
    assert "resources=None" in src, "the parameter does not exist"
    assert "_validate_resources" in src, "nothing validates it"
    assert "resources)" in src and "$10::jsonb" in src, "it is not stored"


def test_the_tool_handler_preserves_omitted():
    """`args.get("resources") or []` would turn "GO forgot" into "GO said
    none" and re-create the bug the parameter exists to fix."""
    src = _read("kernel/assistant/api.py")
    i = src.index('if tool == "propose_errand"')
    # CODE ONLY. The comment there quotes the anti-pattern in order to
    # warn about it, and a naive substring check fires on the warning --
    # a guard tripping on the safe thing, which is how guards get
    # loosened until they stop catching the dangerous one.
    body = chr(10).join(l for l in src[i:i + 1600].splitlines()
                        if not l.strip().startswith("#"))
    assert 'resources=args.get("resources")' in body
    assert 'args.get("resources") or []' not in body


def test_approval_replays_them_as_grants():
    """The story is created here and nowhere else, so this is the only
    moment its grants can be made."""
    src = _read("app/services/errand_grants.py")
    assert "_grant_proposed_resources" in src
    # Anchored on the LAST story INSERT rather than the first, and with
    # room for the clash fallback between them. `short_title` added a
    # second INSERT -- the retry that drops the label when a goal
    # already has one by that name -- so a window from the first insert
    # now ends before the grants. What this guards is unchanged: the
    # grants happen in the same function, right after the story exists.
    i = src.rindex("INSERT INTO stories")
    assert "_grant_proposed_resources" in src[i:i + 1600], (
        "the grants are not made where the story is created")
    # And a failed grant must not lose the approval a person just gave.
    j = src.index("async def _grant_proposed_resources")
    assert "except Exception" in src[j:]


def test_gos_prompt_says_there_is_no_second_call():
    """GO knows `create_story` + `grant_resource` as two acts. This one
    is different and the prompt has to say so, or it will assume it can
    grant later — which is exactly what happened."""
    text = " ".join(_read("kernel/go/AGENTS.md").split()).lower()
    assert "resources" in text
    assert "no second call" in text
    assert "omitting it is refused" in text


def test_the_gateway_declares_every_parameter_the_kernel_reads():
    """THE SIXTH SURFACE, and the one I said did not need changing.

    The gateway does not merely allowlist tool NAMES -- it declares a
    TypeBox schema per tool, and a parameter the schema omits is
    STRIPPED before the call reaches the kernel. So `propose_errand`
    gained `resources` on the backend, the schema did not, and GO's
    grants vanished in transit while both halves looked correct. GO
    diagnosed it from the outside before anyone here noticed.

    H10 already compares the tools the kernel SERVES against the tools
    the gateway DECLARES -- by name. This is the same comparison one
    level down, because the name matching proves nothing about whether
    the call can carry its arguments.

    Skips when the sister repo is not checked out: a single-repo
    checkout cannot see the gateway half, and a guard that fails for
    being unable to look is one people delete."""
    import re

    from app.services import gateway_surface

    # RESOLVED, not assumed. Hardcoding the sibling-checkout path meant
    # this skipped inside the E0 container, where the specs are mounted
    # at /app/gateway-tools -- so in the place the work actually happens
    # it reported "not checked out" and everybody read the green.
    # gateway_surface.specs_dir() is the one implementation that knows
    # the three places to look.
    specs = gateway_surface.specs_dir()
    if specs is None:
        pytest.skip("gateway tool specs are not reachable from here")
    spec = specs / gateway_surface.FILES["go"]

    text = io.open(spec, encoding="utf-8").read()
    i = text.index('name: "propose_errand"')
    block = text[i:i + 8000]

    # What the kernel handler actually reads out of `args` — bounded to
    # THIS tool's block. A fixed window spills into the handlers that
    # follow and reports their parameters as missing, which is a guard
    # that cries wolf and therefore gets deleted.
    api = _read("kernel/assistant/api.py")
    j = api.index('if tool == "propose_errand"')
    nxt = api.find('if tool == "', j + 10)
    handler = api[j:nxt if nxt > 0 else j + 2000]
    reads = set(re.findall(r'args\.get\("(\w+)"\)', handler))
    assert reads, "could not find the handler's argument reads"

    declared = set(re.findall(r'^\s{6}(\w+):', block, flags=re.M))

    # DELIBERATELY NOT EXPOSED. Narrowing the gateway surface is a real
    # and useful thing to do — but it must be a named decision, not a
    # silence. `domain` has a safe server-side default ("ops") and every
    # proposed errand is an ops errand today; `resources` had NO safe
    # default, which is exactly why its omission was a bug and this
    # one is not. Anything added here needs the same sentence.
    NARROWED = {"domain"}

    missing = sorted(reads - declared - NARROWED)
    assert not missing, (
        f"the kernel reads {missing} and the gateway schema does not "
        f"declare them, so they are stripped before the call arrives. "
        f"Either declare them in kernel-go-tools.ts or add them to "
        f"NARROWED with a reason they are safe to drop.")


def test_a_scheduled_run_inherits_its_story_resources():
    """THE STRUCTURAL GAP, and the one that made every earlier fix look
    like it had not worked.

    `inherit` copies a story's grants into `execution_mounts`, and
    `mounts_for` — which the runner uses to build the workspace — reads
    only that table. Its single caller was the assistant's `start_story`.

    A scheduled errand is created by the sweep and never goes through
    that path, so it got ZERO mounts however correct its grants were:
    the proposal named the repository, the story held it, the mount
    query returned it, and the run still opened with an empty directory.
    Its executor's plan said "clone the repository" three times running,
    which read like a model ignoring its context and was an accurate
    report of what it had been handed.

    An autonomous errand is an ordinary errand that GO supervises
    instead of a person. It has to be provisioned like one."""
    src = _read("kernel/worker/schedules.py")
    assert "from kernel.workspace.mounts import inherit" in src, (
        "the sweep cannot provision what it creates")
    i = src.index("execution_id = await create_execution(")
    after = src[i:i + 2600]
    assert "await inherit(" in after, (
        "the sweep creates the run and never copies the story's grants "
        "onto it, so the workspace comes up empty")
    # And a failure there must not cost the schedule its tick.
    j = src.index("await inherit(")
    assert "except Exception" in src[j:j + 700]


def test_only_two_paths_provision_a_run_and_both_are_named():
    """`inherit` is the one place `execution_mounts` is written, and every
    way of starting a run has to go through it. Naming the callers here
    means a third path cannot appear without this test noticing — which
    is how the scheduled path went a whole build without one."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    files = list((root / "kernel").rglob("*.py"))
    files += list((root / "app").rglob("*.py"))
    callers = set()
    for f in files:
        if "/tests/" in f.as_posix() or f.name.startswith("test_"):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if "inherit(conn" in text and "def inherit(conn" not in text:
            callers.add(f.relative_to(root).as_posix())
    assert callers == {"kernel/assistant/write.py",
                       "kernel/worker/schedules.py"}, (
        "the set of paths that provision a run changed: %s. Every one of "
        "them must call `inherit`, or runs started that way come up with "
        "an empty workspace." % sorted(callers))


def test_a_scheduled_run_says_its_story_is_running():
    """A normal errand is IMPORTED, and the import moves its story
    `accepted -> in_progress`. A scheduled one is never imported, so its
    story sat at `accepted` for the whole life of every run — the board
    said "not started" while the work was in flight. That is the
    difference a person actually notices, and it is what made these look
    broken long after they worked."""
    src = _read("kernel/worker/schedules.py")
    i = src.index("execution_id = await create_execution(")
    after = src[i:i + 4000]
    assert "state = 'in_progress'" in after, (
        "the sweep starts a run and leaves the story saying it never began")
    # Guarded like the import is, or a re-run drags a moved story back.
    assert "state IN ('proposed', 'accepted')" in after
    # And it must not be able to fail the tick.
    j = src.index("state = 'in_progress'")
    assert "except Exception" in src[j:j + 800]


def test_gos_prompt_says_a_lane_pins_the_branch_for_a_read():
    """GO granted the bare repo when asked to summarise `cal_kernel`, and
    the run checked out `main` — a one-commit branch — then wrote a
    confident report about the wrong code. Nothing failed and nothing
    said so.

    Its prompt described lanes only as landing targets for stories, so
    for a read-only errand `repo` looked like the natural choice."""
    text = " ".join(_read("kernel/go/AGENTS.md").split()).lower()
    assert "pin a branch for a read" in text
    assert "default branch" in text, (
        "the prompt does not say what a bare repo grant actually checks out")
    assert "grant that lane with `needs`" in text


def test_approval_provisions_the_operator_workspace():
    """WHAT THE ASSISTANT PANEL ACTUALLY READS.

    `stories.workspace_path` holds the OPERATOR workspace on the gateway
    HOST — `…/workspace-<assistant>-<GOAL>/stories/<id>` — not the
    kernel's `/workspace/exec-N`. So the panel was empty for every
    scheduled errand, and writing the container path into that field
    would have put a host-shaped value where a container path is not
    valid and broken the operator flow.

    `provision_story_workspace` is the function import uses, and it also
    calls `reconcile` — which is why imported stories get a git node and
    scheduled ones did not. One call fixes both."""
    src = _read("app/services/errand_grants.py")
    assert "_schedule_operator_workspace" in src
    i = src.index("_open_story_thread(")
    assert "_schedule_operator_workspace" in src[i:i + 2000], (
        "the workspace is not provisioned where the thread is opened")


def test_that_provisioning_cannot_cost_the_approval():
    """It registers the agent on the gateway, and that is the call that
    timed out at 30s while the gateway was busy archiving. A person's
    approval must not fail because provisioning was slow.

    THE WINDOW USED TO BE A BYTE COUNT -- `src[i:i + 2200]` -- and it
    went red the first time the function grew a longer docstring, which
    is a change to prose that cannot affect whether provisioning blocks.
    A guard that fails on a comment teaches people to loosen guards. The
    function's own source is the honest window.
    """
    import inspect
    from app.services import errand_grants
    body = inspect.getsource(errand_grants._schedule_operator_workspace)
    assert "create_task" in body, "provisioning blocks the approval"
    assert "except Exception" in body
    # And no event loop must not raise either — approve() is called from
    # sync contexts in tests.
    assert "RuntimeError" in body


def test_only_import_and_errand_approval_provision_workspaces():
    """Pinning the blast radius. `provision_story_workspace` does real
    work — gateway registration, and a CLONE for a non-kernel assistant —
    so a third caller appearing silently is how a working operator setup
    would break."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    callers = set()
    for f in list((root / "app").rglob("*.py")) + list((root / "kernel").rglob("*.py")):
        if "/tests/" in f.as_posix() or f.name.startswith("test_"):
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        if "provision_story_workspace(" in t and "async def provision_story_workspace" not in t:
            callers.add(f.relative_to(root).as_posix())
    assert callers == {"app/routers/assistant.py",
                       "app/services/errand_grants.py"}, (
        f"who provisions operator workspaces changed: {sorted(callers)}")


def test_a_kernel_assistant_workspace_holds_no_clone():
    """Why adding this caller is cheap and safe. A kernel assistant's
    story workspace is documents and plans — the work happens in a
    sandbox with its own checkout — so no clone, token or credential
    helper is created on the gateway host."""
    src = _read("app/services/operator_provisioning.py")
    assert "docs_only = await is_kernel_assistant" in src
    assert "WORKSPACE_DOCS if docs_only else WORKSPACE_CLONE" in src
