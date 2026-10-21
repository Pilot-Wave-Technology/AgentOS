"""Phase 2 · the prompts say what the profile actually does.

`kernel/assistant/AGENTS.md` carried a section called "An errand is not a
small story" that was true at Eb3 and false in every clause by Ec2. It
said an errand has *"no plan and no approval — it will never produce a
card for a person"*, *"no contract, no criteria, no verifier"*, *"no
commit and no workspace — bash only; it cannot write files, cannot push,
has no web access"*, and *"It runs and it finishes."*

Meanwhile `profiles/errand-ops/profile.toml` says `requires_plan = true`,
`allows_completion = true`, and grants `write`, `edit`, `env_setup`,
`env_install`, `web_fetch`, `emit_artifact`, `register_plan` and
`propose_completion`.

**THE COST WAS NOT MISSING INFORMATION, IT WAS CONFIDENT WRONG
INFORMATION.** An assistant obeying that section reads a legitimate plan
approval as an anomaly, and routes every report, investigation and log
read to a story — the exact inverse of what errands are for.

So these tests are not prose review. They score the prompt against the
profile as loaded, which means the next person who changes the profile
finds out here rather than by watching an agent make bad choices.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


ASSISTANT = ("kernel/assistant/SOUL.md", "kernel/assistant/AGENTS.md")
GO = ("kernel/go/SOUL.md", "kernel/go/AGENTS.md")
ALL_PROMPTS = ASSISTANT + GO


@pytest.fixture(scope="module")
def errand():
    from kernel.profiles.loader import load_all

    return load_all()[("errand", "ops")]


def _text(files) -> str:
    """The prompts as one lowercased string, with whitespace collapsed.

    COLLAPSED BECAUSE THESE ASSERTIONS ARE ABOUT WORDS, NOT LAYOUT. Every
    check in this file looks for a phrase, and a phrase that happens to
    straddle a line break stops matching — so re-wrapping a paragraph
    turns a correct prompt into a failing test, and the obvious way to
    "fix" that is to weaken the assertion until it passes.

    Observed while rewriting the errand doctrine for D1: "do not send the
    email" was present, correct, and split across two lines.
    """
    import re as _re

    joined = "\n".join(_read(f) for f in files)
    return _re.sub(r"\s+", " ", joined).lower()


# ── the falsified claims, one test each ──────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "no plan and no approval",
    "it will never produce a card",
    "no contract, no criteria, no verifier",
    "no commit and no workspace",
    "bash only",
    "has no web access",
    "it runs and it finishes",
    "errands are the exception",
    "which have nothing to review",
    "nothing to review",
])
def test_a_falsified_claim_is_gone_from_every_prompt(phrase):
    """Each of these was true at Eb3 and is false now. They are listed
    individually so a partial revert is caught as precisely as a full
    one."""
    for path in ALL_PROMPTS:
        assert phrase not in _read(path).lower(), f"{path} still says it"


def test_the_prompt_does_not_contradict_the_profile_on_planning(errand):
    assert errand.requires_plan is True
    text = _text(ASSISTANT)
    assert "plans first" in text or "it plans first" in text, (
        "the profile requires a plan and the prompt never says so")


def test_the_prompt_does_not_contradict_the_profile_on_completion(errand):
    assert errand.allows_completion is True
    assert "propose_completion" in _text(ASSISTANT)


@pytest.mark.parametrize("tool", ["write", "edit", "env_setup",
                                  "env_install", "web_fetch",
                                  "emit_artifact"])
def test_the_prompt_knows_the_errand_holds_it(errand, tool):
    """The old section denied four of these outright. A model told it
    cannot write files will not try, whatever the surface says."""
    assert tool in errand.tools, f"{tool} is no longer on the surface"
    assert tool in _text(ASSISTANT), (
        f"the errand holds {tool} and the assistant is never told")


def test_the_prompt_says_an_errands_work_never_lands(errand):
    """D1 REPLACED THIS TEST'S SUBJECT, and the replacement is stricter.

    It used to assert the workspace was DISCARDED (`workspace_driver ==
    "none"`), which was the old model: an errand had a scratch tree and
    no repository. That produced two defects and a workspace panel that
    was empty for the entire life of every errand ever run, because the
    `none` driver never sets `workspace_path` and the panel resolves its
    tree from it.

    An errand now gets what a story gets. The consequential fact is no
    longer "your files are thrown away" — it is "your commits never reach
    trunk, so the artifact is the deliverable". An assistant that does not
    know THAT will let a run finish having committed a perfect change
    nobody will ever see.
    """
    assert errand.workspace_driver != "none", (
        "an errand with no workspace driver gets no workspace_path, and "
        "the workspace panel resolves its tree from workspace_path")
    text = _text(ASSISTANT)
    assert "never does" in text or "never land" in text, (
        "the assistant is not told an errand's work does not land")
    assert "list_artifacts" in text


def test_the_prompt_says_an_errand_has_no_verifier_because_that_is_true(errand):
    """One of the four old claims IS still true, and deleting it with the
    others would be the opposite error."""
    assert errand.has_verifier is False
    assert "no verifier" in _text(ASSISTANT)


# ── the decision rule ────────────────────────────────────────────────────

# D1 — THE RULE'S SUBJECT MOVED. It was "a story produces a branch that
# merges", which invited the reading that an errand gets no branch. It
# gets one; what it never gets is a landing. Both agents must carry the
# SAME sentence, which is what this constant is for: two agents choosing
# between the same two shapes by different words is how one creates a
# story the other would have made an errand.
RULE = "a story's work lands on trunk"


@pytest.mark.parametrize("files,who", [(ASSISTANT, "assistant"), (GO, "GO")])
def test_both_agents_carry_the_same_rule(files, who):
    """The same words in both, deliberately. Two agents choosing between
    the same two shapes by different rules is how one creates a story the
    other would have made an errand."""
    text = _text(files)
    assert RULE in text, f"{who} does not carry the rule"
    # The other half of the same sentence. Without it the rule reads as
    # "stories land" with nothing said about errands at all.
    assert "never does" in text, f"{who} does not say an errand never lands"
    assert "size is not the test" in text


@pytest.mark.parametrize("files,who", [(ASSISTANT, "assistant"), (GO, "GO")])
def test_the_rule_names_the_shapes_that_make_an_errand_recognisable(files, who):
    """"short bounded work with nothing to review" misrouted the whole
    catalogue — a log investigation, a cost breakdown, a cohort analysis
    are substantial work with a deliverable, which under that wording
    reads as a story."""
    text = _text(files)
    for shape in ("log", "investigat", "report", "measurement", "analys"):
        assert shape in text, f"{who} never mentions {shape!r}"


@pytest.mark.parametrize("files,who", [(ASSISTANT, "assistant"), (GO, "GO")])
def test_the_rule_says_what_to_do_when_it_is_both(files, who):
    """The commonest real case. Run it as one story and the
    investigation's findings live only in a branch nobody merges."""
    assert "split it" in _text(files), f"{who} does not say to split it"


# ── GO's two carve-outs ──────────────────────────────────────────────────

def test_go_is_told_an_errand_does_not_land():
    """`goal_state` reports `landed` and counts `done_not_landed`, and
    GO is told never to announce delivery until the landing wake arrives.
    An errand has no branch, so that wake never comes — GO would withhold
    every finished errand's answer for ever."""
    text = _text(GO)
    assert "done ≠ landed" in text or "done is not landed" in text, (
        "the rule this carves out from is gone; check the carve-out too")
    assert "delivered the moment it is accepted" in text


def test_go_is_told_an_errand_is_granted_like_a_story():
    """REVERSED BY D1, deliberately. This asserted "an errand with no
    grants is normal" — the old model, where an errand had a scratch tree
    and needed no repository.

    An errand is now allocated exactly what a story is allocated,
    including trunk when a check can only run there, and including WRITE
    when the work needs it: installing a dependency or flipping a config
    to run the thing it was sent to measure is the work, not a violation.
    GO withholding access to protect trunk would be protecting the wrong
    thing — the platform enforces the destination."""
    text = _text(GO)
    assert "granted exactly what a story is granted" in text
    assert "never gets is a landing" in text, (
        "GO is not told what the actual constraint is, so it will infer "
        "one — and the one it infers will be about access")


def test_go_is_told_what_a_grant_buys_an_errand():
    """STILL TRUE UNDER D1, and worth keeping for that reason. An errand
    now gets a workspace like a story, so the interesting thing a grant
    buys is no longer the tree — it is REACH. A host is reachable only
    when the work holds a resource naming it, which is how "run the
    report but do not send the email" is enforced rather than promised.

    D1 widened what may be granted; it did not make grants decorative."""
    text = _text(GO)
    assert "grants still buy reach" in text, (
        "the reach rule was lost when the errand-grants paragraph was "
        "rewritten for D1")
    assert "do not send the email" in text or "not send the email" in text


# ── the surfaces stay consistent ─────────────────────────────────────────

def test_no_prompt_promises_a_tool_its_agent_does_not_hold():
    """The general form of the `list_artifacts` incident, checked for
    both agents at once rather than only in the container gate."""
    from kernel.assistant import TOOLS as A_TOOLS
    from kernel.go import TOOLS as GO_TOOLS

    for files, held, who in ((ASSISTANT, A_TOOLS, "assistant"),
                             (GO, GO_TOOLS, "GO")):
        text = "\n".join(_read(f) for f in files)
        missing = [t for t in held if f"`{t}`" not in text]
        assert not missing, f"{who} holds and is never told about: {missing}"


# ── the count in the first line ──────────────────────────────────────────

# GENERATED, NOT LISTED. This was a hand-written dict that stopped at
# twenty-eight, and the twenty-ninth tool failed the test with "unmapped
# number word" -- a guard against a hand-maintained number going stale,
# itself stale for the same reason. It now covers anything a prompt
# could plausibly say.
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty",
         "seventy", "eighty", "ninety"]
WORDS = {w: n for n, w in enumerate(_ONES) if w}
for _t in range(2, 10):
    WORDS[_TENS[_t]] = _t * 10
    for _o in range(1, 10):
        WORDS[f"{_TENS[_t]}-{_ONES[_o]}"] = _t * 10 + _o


@pytest.mark.parametrize("rel", ["kernel/go/AGENTS.md",
                                 "kernel/assistant/AGENTS.md"])
def test_the_stated_tool_count_matches_the_table(rel):
    """A HAND-MAINTAINED NUMBER DRIFTS, and this one drifted in both
    directions at once: GO's prompt opened with "Twenty-one tools" while
    the kernel served 20 and the table listed 25; the assistant said
    "Seventeen", which was the kernel's count and not the table's 23.

    The number is not decoration — it is the model's check that it has
    the whole surface, and it says "everything you need is on this page"
    in the same breath. A wrong one invites a model that counts the rows
    to go looking for the tools it thinks are missing."""
    import re

    text = _read(rel)
    word = re.search(r"^([A-Za-z-]+) tools, no skills", text, re.M)
    assert word, "the first line no longer states a tool count"
    stated = WORDS.get(word.group(1).lower())
    assert stated is not None, f"unmapped number word {word.group(1)!r}"
    # DISTINCT names. The assistant prompt tables `story_status`,
    # `goal_state` and `goal_knowledge` twice — once in the orientation
    # table near the top and again in the full reference — and counting
    # rows would make the honest number 23 for 20 tools.
    # ONLY THE TOOL TABLES, and every name in a leading cell.
    #
    # Two ways to miscount, and the first version of this guard made
    # both. `| `memory_search` / `memory_get` |` is one row documenting
    # TWO tools, so anchoring on the row start saw one. And the
    # assistant's page carries a `| field | means |` table whose rows
    # are `writable` and `can_write` -- RESPONSE FIELDS of
    # `list_resources`, not tools -- which a blind sweep counts as two
    # more tools that do not exist.
    rows, header = set(), None
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [c.strip().strip("*").lower() for c in stripped.split("|")[1:-1]]
        if header is None:
            header = cells                      # first row of a new table
            continue
        # NOT A TOOL TABLE. `field` is `list_resources`'s response
        # fields; `rung` is the machine-size ladder, whose rows are
        # `small`/`medium`/`large` in backticks and would otherwise count
        # as three tools that do not exist.
        if header and header[0] in ("field", "kind", "state", "role",
                                    "rung"):
            continue
        if set("".join(cells)) <= set("- :"):
            continue                            # the --- separator
        rows.update(re.findall(r"`([a-z_]+)`", stripped.split("|")[1]))
    assert stated == len(rows), (
        f"{rel} claims {stated} tools and tables {len(rows)}: "
        f"{sorted(rows)}")


def test_a_story_update_assigns_each_column_once():
    """POSTGRES 42601, and it took out every `update_story` that carried
    `depends_on`. The field was added to the shared loop when
    `land_together` joined it, and the standalone block that had handled
    it before was left in place — so the statement read
    `SET depends_on = $2, ..., depends_on = $5` and the whole UPDATE was
    refused. Nothing caught it because the loop and the block are eleven
    lines apart and each is correct on its own."""
    import collections
    import re

    src = _read("app/routers/stories.py")
    body = src[src.index("async def internal_update_story"):]
    body = body[:body.index("\nasync def ", 10)]
    assigned = re.findall(r'updates\.append\(f?"(\w+) = \$', body)
    loop = re.search(r'for field in \(([^)]*)\)', body, re.S)
    if loop:
        assigned += re.findall(r'"(\w+)"', loop.group(1))
    dupes = [c for c, n in collections.Counter(assigned).items() if n > 1]
    assert not dupes, (
        f"{dupes} would be assigned twice in one UPDATE -- Postgres "
        f"refuses the statement with 42601")


def test_every_name_a_kernel_package_exports_resolves():
    """`__all__ = ["Refused", "TOOLS", *TOOLS]` re-exported four names the
    module never imported, so `from kernel.assistant import *` raised
    AttributeError. The surface assertion did not see it: it compares
    `set(ROUTES) == set(TOOLS)` — NAMES against names — and never
    resolves one to the function behind it."""
    import importlib

    for pkg in ("kernel.assistant", "kernel.go"):
        mod = importlib.import_module(pkg)
        missing = [n for n in getattr(mod, "__all__", ())
                   if not hasattr(mod, n)]
        assert not missing, f"{pkg}.__all__ exports unresolvable {missing}"
