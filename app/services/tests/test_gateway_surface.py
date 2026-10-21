"""The fourth declaration: does the gateway OFFER what the kernel serves?

A tool is real only if it exists in four places — the kernel package
that implements it, the route set that serves it, the template allowlist
that permits it, and the gateway spec that offers it to the model. The
first three have tests that run on a laptop. The fourth had one that
could only run inside the E0 container against a mount, so where the
work happens it reported UNCHECKED and everybody read the green.

WHAT THAT COST, found while pushing Phase 5: **five tools the models
were never offered.** `list_artifacts` on BOTH surfaces — Ec2g added it
for the assistant and I3c for GO, both stopped at the template — so the
entire "an errand leaves no branch, its artifacts ARE its result, here
is how to read them" capability was unreachable by either model from the
day it was built. Plus GO's three from I5e/I5f. Both prompts described
tools that could not be called, and nothing errored, because nothing was
ever attempted.

This is the third time in this build that correct logic sat behind a
path that does not exist where anyone runs it (`test_effects_cover_
profiles`, `test_h8b_prompt`). So the comparison lives in one module
that RESOLVES the path, and both this and the E0 gate call it.
"""
from __future__ import annotations

import os

import pytest

from app.services.gateway_surface import FILES, compare, declared, specs_dir


def _dir():
    path = specs_dir()
    if path is None:
        pytest.skip("agent_os_openclaw is not checked out beside this repo")
    return path


@pytest.mark.parametrize("who", sorted(FILES))
def test_every_tool_the_kernel_serves_is_offered_to_the_model(who):
    """The direction that fails SILENTLY, which is why it kept happening.
    The model reads about the tool in its prompt, has no way to call it,
    and there is no error anywhere because nothing was attempted."""
    missing, _ = compare(_dir())[who]
    assert not missing, (
        f"the kernel serves {missing} on the {who} surface and the gateway "
        f"never offers them — the prompt promises a tool the model cannot "
        f"call, and nothing errors because nothing is attempted")


@pytest.mark.parametrize("who", sorted(FILES))
def test_every_tool_the_gateway_offers_is_actually_served(who):
    """The other direction, which fails loudly and confusingly: the model
    IS offered the tool, calls it, and gets a 404 it reads as a broken
    system rather than a tool that does not exist."""
    _, extra = compare(_dir())[who]
    assert not extra, (
        f"the gateway offers {extra} on the {who} surface and the kernel "
        f"does not serve them")


def test_the_two_surfaces_stay_apart():
    """The assistant asks and GO decides, and that is only real while the
    two hold different tools. `grant_resource` reachable by naming it
    from an assistant would make the boundary a convention.

    WHAT THE BOUNDARY IS NOW, because it moved and this test used to
    state the old one. Phase C gave GO the seven verbs that drive an
    autonomous errand -- `steer` and `ask_executor` among them -- and
    they are the assistant's own functions, shared deliberately for the
    reason `list_artifacts` and `inbox` are shared: two implementations
    of one act answer differently the first time one grows a field.

    So a SHARED TOOL IS NO LONGER A BOUNDARY VIOLATION, and the boundary
    is enforced one layer down instead: `kernel/go/drivable.go_may_drive`
    refuses GO on any story that is not an errand with a live grant. What
    still must not be shared is the tools that would make GO and the
    assistant the same agent -- GO cannot START work, and an assistant
    cannot GRANT itself anything.
    """
    path = _dir()
    a, go = declared(path, "assistant"), declared(path, "go")
    assert "grant_resource" not in a and "register_resource" not in a
    assert "propose_errand" not in a, (
        "an assistant can propose an autonomous errand, which is GO's act")
    assert "start_story" not in go, (
        "GO never starts a run: a story's assistant does, and an approved "
        "autonomous errand starts itself on its schedule")
    assert "amend" not in go, (
        "amend withdraws an approval and writes a new contract version -- "
        "the one verb in this family that changes what a person agreed to")
    # `list_artifacts` and `inbox` are on both ON PURPOSE — both are
    # goal-scoped reads, and two implementations of "what did this goal
    # produce" answer differently the first time one grows a field.
    assert "list_artifacts" in a and "list_artifacts" in go
    # And since Phase C, so are the five that drive one run. Shared
    # FUNCTION, separate AUTHORITY -- see `go_may_drive`.
    for shared in ("steer", "ask_executor", "story_progress",
                   "story_status", "cancel_story"):
        assert shared in a and shared in go, shared
    # `run_control` is GO's alone. The assistant reaches the same three
    # verbs through the BUTTONS on its thread -- a person pressing them
    # directly -- so it needs no tool for them.
    assert "run_control" in go and "run_control" not in a


def test_the_check_can_run_where_the_work_happens():
    """The actual defect. A guard that only runs in a container nobody
    starts locally is not a guard, and this is the third instance.

    THE ONE ENVIRONMENT THAT IS EXEMPT, and why the exemption is narrow.
    Cloud Build checks out ONE repository. `agent_os_openclaw` is a
    different one, so there is no sibling to find and no arrangement of
    this code can conjure one -- the comparison is not being skipped
    because it is inconvenient, it is unavailable because half its
    input is not on the machine.

    That is survivable only because the check still runs in both places
    a person changes these files: a developer checkout, where the
    sibling is right there, and the E0 container, where it is mounted at
    /app/gateway-tools. If it ever stops running in BOTH of those, this
    exemption becomes the thing its own docstring warns about.

    The flag is set explicitly in cloudbuild.yaml rather than inferred
    from the absence of the directory, because inferring it is exactly
    how the previous version of this check came to report UNCHECKED
    everywhere and be believed.
    """
    import pathlib
    import warnings

    from app.services.gateway_surface import specs_dir as resolve

    if os.environ.get("AGENTOS_SINGLE_REPO_CHECKOUT") == "1":
        pytest.skip("single-repo checkout: the gateway repository is not "
                    "here to compare against (see cloudbuild.yaml)")

    if resolve() is not None:
        return                                   # the check can run; done

    # NOT HAVING THE REPOSITORY IS NOT A FAILING TEST.
    #
    # Everything above still holds, and this does not weaken it -- but it
    # answers a question the old form could not: `specs_dir()` returns
    # None both when the sibling repository is ABSENT and when it is
    # PRESENT and the specs are not where they should be. Those are
    # different facts and only the second is anybody's bug.
    #
    # Somebody who has cloned only `agentos` -- an OSS consumer, a
    # new starter, anyone reading the code -- cannot conjure
    # `agent_os_openclaw`, is not editing gateway specs, and should not get
    # a red suite for it. Before this they did, and the only escape was
    # an environment variable documented in cloudbuild.yaml.
    #
    # THIS IS NOT THE INFERENCE THE DOCSTRING WARNS ABOUT. That one asked
    # "can I find the specs?" and answered UNCHECKED wherever it could
    # not -- including the checkouts where the repository WAS there and
    # the path was simply wrong, which is the case worth failing on. This
    # asks the narrower and far more reliable question: is the sibling
    # repository on this machine at all? If it is, a missing spec file is
    # still a hard failure, exactly as before.
    #
    # AND IT IS NOT SILENT. The old version's real sin was reporting
    # unchecked and being believed. A warning plus a skip reason is the
    # minimum that cannot be mistaken for a pass.
    here = pathlib.Path(__file__).resolve()
    root = next((p for p in here.parents
                 if (p / "kernel").is_dir() and (p / "migrations").is_dir()),
                None)
    sibling = (root.parent / "agent_os_openclaw") if root else None

    if sibling is not None and sibling.is_dir():
        raise AssertionError(
            f"`{sibling}` is here but its tool specs are not reachable, so "
            f"the gateway comparison is decoration. Expected "
            f"{sibling / 'src/agents/tools'} to hold {FILES['go']}; set "
            f"GATEWAY_TOOLS_DIR if it lives elsewhere.")

    warnings.warn(
        "gateway specs unavailable: no `agent_os_openclaw` beside this "
        "checkout, so the kernel-vs-gateway tool comparison did not run. "
        "That is expected with only agentos cloned. Clone the gateway "
        "repository as a sibling, or set GATEWAY_TOOLS_DIR, to enable it.",
        stacklevel=2)
    pytest.skip("no sibling `agent_os_openclaw`: half the comparison's input "
                "is not on this machine (warned above)")


def test_the_comparison_has_exactly_one_implementation():
    """A second copy would be a fifth declaration of the same fact, and
    the whole file is about what that costs."""
    import io
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    # The source trees, not the whole checkout: a repo-wide glob walks
    # local-development/vendor and takes two minutes, and a test that
    # slow stops being run.
    files = [f for d in ("app", "kernel", "infra", "scripts")
             for f in (root / d).glob("**/*.py")]
    for path in files:
        rel = str(path.relative_to(root)).replace("\\", "/")
        if ("__pycache__" in rel or ".venv" in rel
                or rel == "app/services/gateway_surface.py"
                or rel == "app/services/tests/test_gateway_surface.py"):
            continue
        text = io.open(path, encoding="utf-8", errors="replace").read()
        if re.search(r'kernel-(go|assistant)-tools\.ts', text) and \
                "gateway_surface" not in text:
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} compare the surfaces themselves instead of calling "
        f"app.services.gateway_surface")


# ── F8 · the check one level deeper: PARAMETERS, not just names ──────────
#
# Every non-logic defect found in the live round was a mismatch one level
# below what this file compares. The tool NAMES agreed across all four
# declaration sites; what disagreed was a parameter (`work_kind` absent
# from create_story), a column, a constant, a flag. Comparing names is
# necessary and was never sufficient.
#
# This starts with the one that cost a blocking bug. Generalising it to
# every tool needs a parameter map the CP side does not publish, so it is
# deliberately narrow rather than aspirational: a test that covered
# "every parameter" by pattern-matching would be the kind of guard that
# looks thorough and proves nothing.

def _spec(name: str) -> str:
    """One gateway tool-spec file, from wherever the specs resolved to."""
    import io
    import pathlib

    return io.open(pathlib.Path(_dir()) / name, encoding="utf-8").read()


def test_create_story_can_say_which_shape_of_work_it_is():
    """F8. `stories.work_kind` existed, `write.py` read it, `goal_state`
    reported it, and GO's prompt said "say which it is" — with no field
    to say it in. GO prefixed `[Errand]` to the title instead, and the
    errand ran on the story profile, wanting a branch and a merge for
    work that produces a report."""
    # THE DECLARED PARAMETER, not the word. The description also says
    # "PASS `work_kind`", so a substring match stayed green through a
    # mutation that deleted the field entirely — caught by red-checking.
    import re

    declared = re.findall(r"^\s{2}(\w+):\s*Type\.", _spec("story-tools.ts"),
                          re.M)
    assert "work_kind" in declared, (
        f"create_story declares {declared} and cannot mark work as an "
        f"errand, so every errand GO creates runs as a story")


def test_the_endpoint_reads_the_name_the_tool_sends():
    """THE MISMATCH THIS ALMOST SHIPPED WITH. The spec sends `work_kind`;
    the endpoint was reading `kind`. A parameter that travels the whole
    way and is dropped at the last step is worse than one that is
    missing — the call succeeds and the field vanishes."""
    import io
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    sent = set(re.findall(r"^\s{2}(\w+):\s*Type\.", _spec("story-tools.ts"),
                          re.M))
    assert "work_kind" in sent, "the spec no longer sends work_kind"

    src = io.open(root / "app/routers/stories.py", encoding="utf-8").read()
    body = src[src.index("async def internal_create_story"):]
    body = body[:body.index("@router.", 10)]
    read = set(re.findall(r'body\.get\("(\w+)"', body))
    assert "work_kind" in read, (
        f"the endpoint reads {sorted(read)} and never work_kind, so the "
        f"tool sends it and the story is created without it")


@pytest.mark.parametrize("name", ["kernel-go-tools.ts",
                                  "kernel-assistant-tools.ts"])
def test_no_spec_still_describes_the_discarded_errand_workspace(name):
    """THE COPY THAT WINS. A tool description is read at the moment the
    model decides whether to call the tool, and `list_artifacts` said "an
    errand leaves no branch and its workspace is thrown away" long after
    D1 gave it both — contradicting, word for word, the paragraph in the
    prompt written to correct it. When two sources disagree the model has
    no way to know which is current."""
    text = _spec(name).lower()
    assert "thrown away" not in text
    assert "leaves no branch" not in text


@pytest.mark.parametrize("field", ["work_kind", "land_together",
                                   "story_weight"])
def test_a_field_the_platform_reads_is_a_field_the_tool_declares(field):
    """THE BUG CLASS BEHIND F8, G5 AND THE SIZING FIELD — three instances
    of one shape, each found a migration apart.

    A column exists, the endpoint reads it off the body, the platform
    acts on it, and GO's prompt may even teach it — but no tool schema
    declares it, so the model has no way to send it and the gateway
    strips it if it guesses. Nothing errors. The story is simply created
    with the default, and the defect shows up somewhere else entirely:
    `work_kind` as an errand running the story profile, `land_together`
    as a chain that lands in separate merges, `story_weight` as every
    GO-created story rendering "Not sized".

    Undeclared is unreachable, and unreachable fails silently."""
    spec = _spec("story-tools.ts")
    assert f"{field}: Type.Optional(" in spec or f"{field}: Type." in spec, (
        f"`{field}` is read by app/routers/stories.py and declared in no "
        f"story tool schema -- GO cannot set it")


def test_the_canonical_size_field_is_the_one_recommended():
    """`estimated_effort` is free text that `app/models/story.py` calls
    "retired from active use"; `story_weight` is what the board label and
    the Taximeter derive from. Offering both without saying which wins
    reproduces the original defect with an extra step."""
    import re

    spec = _spec("story-tools.ts")
    # ON THE DECLARATION, not the first mention — the comment explaining
    # why the field is retired sits above it and would satisfy a plain
    # substring search while the description itself said nothing.
    decls = [m.start() for m in
             re.finditer(r"^\s*estimated_effort: Type\.", spec, re.M)]
    assert decls, "estimated_effort is no longer declared"
    for i in decls:
        assert "RETIRED" in spec[i:i + 400], (
            "estimated_effort is offered without marking it retired, so "
            "a model picks whichever sizing field it reads first")


@pytest.mark.parametrize("tool", ["steer", "ask_executor", "amend",
                                  "cancel_story"])
def test_every_aimable_write_can_actually_be_aimed(tool):
    """`_aim` resolves a write to one run, and when `story` is not given
    it disambiguates on `hint` — a fragment of what the person said, "the
    rename one". `kernel/assistant/context.py` states outright that
    turning what was said into a hint "is the assistant's job".

    No spec declared `hint`, so the assistant could not do that job: an
    ambiguous target was refused and the assistant had no way to resolve
    it. `cancel_story` declared no `focus` either, making the one
    undoable write the only one that could not be aimed at all."""
    spec = _spec("kernel-assistant-tools.ts")
    blk = spec[spec.index(f'name: "{tool}"'):]
    blk = blk[:blk.index("},\n  {")] if "},\n  {" in blk else blk
    for param in ("hint:", "focus:"):
        assert param in blk, (
            f"`{tool}` accepts {param.rstrip(':')} in the kernel and "
            f"declares it in no spec -- the model cannot send it")


def test_no_template_both_allows_and_denies_a_tool():
    """THE FOURTH DECLARATION SITE, and the one no other guard reads.

    `test_every_tool_the_kernel_serves_is_offered_to_the_model` compares
    what the kernel SERVES against what the gateway DECLARES — both were
    correct for `register_resource` and `write`, and both were
    unreachable anyway, because `go-v2` carried each in `tools_allow`
    AND in `tools_deny` and the gateway's matcher checks deny first.

    Each arrived that way the same honest route: an early migration
    denied the tool deliberately, a later one established the denial was
    wrong and appended to the allowlist without clearing the deny row.
    The later migration then reported success. A tool can be present in
    every place that is checked and still be denied by a row nobody
    re-read.

    Whichever side would win, a template that both allows and denies a
    tool has two answers to one question."""
    import asyncio

    import asyncpg

    async def check():
        for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                    os.environ.get("DATABASE_URL"),
                    "postgresql://agentos:agentos@localhost:5433/agentos"):
            if not dsn:
                continue
            try:
                return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
            except Exception:                                   # noqa: BLE001
                continue
        return None

    async def run():
        conn = await check()
        if conn is None:
            pytest.skip("no local database")
        try:
            return await conn.fetch(
                "SELECT id, tools_allow, tools_deny FROM templates "
                "WHERE tools_allow IS NOT NULL AND tools_deny IS NOT NULL")
        finally:
            await conn.close()

    for row in asyncio.run(run()):
        both = set(row["tools_allow"] or []) & set(row["tools_deny"] or [])
        assert not both, (
            f"template {row['id']!r} both allows and denies {sorted(both)} "
            f"-- deny wins, so the allow is a no-op that reads as a grant")


# Names a prompt table may carry that are NOT gateway tools. Kept
# deliberately short: every addition is a hole in the phantom-tool check
# below, so a name belongs here only when it is genuinely provided by
# something other than the template's allowlist.
_NOT_TOOLS = {
    # The workspace verbs the runtime gives every agent directly.
    "read", "write",
    # Memory, provided by the gateway rather than the template.
    "memory_search", "memory_get",
}


@pytest.mark.parametrize("template,prompt", [
    ("go-v2", "kernel/go/AGENTS.md"),
    ("kernel-assistant", "kernel/assistant/AGENTS.md")])
def test_the_prompt_tables_every_tool_the_template_allows(template, prompt):
    """THE FIFTH DECLARATION. The allowlist says what the model may call;
    the prompt table says what it knows how to call. They are different
    files maintained by different changes, and a tool in the first and
    not the second is one the model holds and never reaches for — the
    `list_artifacts` incident, which is why every other guard here
    exists.

    Found live, twice in one pass: GO held `write` (newly reachable, the
    allow/deny collision having hidden it until then) and the Memory
    section told it to "record the *why* after" without naming the tool
    that writes. The assistant held `read`/`write` with no row at all."""
    import asyncio
    import io
    import pathlib
    import re

    import asyncpg

    async def run():
        for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                    os.environ.get("DATABASE_URL"),
                    "postgresql://agentos:agentos@localhost:5433/agentos"):
            if not dsn:
                continue
            try:
                conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
            except Exception:                                   # noqa: BLE001
                continue
            try:
                return await conn.fetchrow(
                    "SELECT tools_allow FROM templates WHERE id = $1",
                    template)
            finally:
                await conn.close()
        return None

    row = asyncio.run(run())
    if row is None:
        pytest.skip("no local database")

    root = pathlib.Path(__file__).resolve().parents[3]
    text = io.open(root / prompt, encoding="utf-8").read()
    tabled, header = set(), None
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [c.strip().strip("*").lower()
                 for c in stripped.split("|")[1:-1]]
        if header is None:
            header = cells
            continue
        # "rung" is the sandbox-size table -- `small`/`medium`/`large`
        # in the first column, which are not tools. Skipping the TABLE
        # is better than listing its three names as exceptions: the
        # exception list is a hole in the phantom check below, and this
        # keeps it shut.
        if header and header[0] in ("field", "kind", "state", "role",
                                    "rung"):
            continue
        if set("".join(cells)) <= set("- :"):
            continue
        tabled.update(re.findall(r"`([a-z_]+)`", stripped.split("|")[1]))

    allowed = set(row["tools_allow"] or [])
    missing = sorted(allowed - tabled)
    assert not missing, (
        f"{template} is allowed {missing} and {prompt} tables none of "
        f"them -- the model holds a tool it is never told how to use")

    # ── AND THE OTHER DIRECTION, which was missing and cost a release.
    #
    # A row in the table for a tool the template does NOT allow is worse
    # than a tool with no row: the model reads its own prompt, believes
    # it has the tool, reaches for it, and finds nothing. Watched live
    # on ERR15 -- `accept_errand` was implemented, tested, documented
    # and NOT in `tools_allow`, so GO told the person "there is no tool
    # that can do this" and sent them to click the card instead.
    #
    # The check above passed throughout, because it only ever asked
    # whether every ALLOWED tool was documented. Nothing asked whether
    # every DOCUMENTED tool existed.
    #
    # `_NOT_TOOLS` are the names the table legitimately mentions that
    # are not gateway tools: parameters, states and the workspace verbs
    # the runtime provides directly.
    phantom = sorted(t for t in tabled - allowed if t not in _NOT_TOOLS)
    assert not phantom, (
        f"{prompt} tables {phantom}, which {template} does not allow -- "
        f"the model will reach for them and find nothing")
