"""Ec4d — the places a credential can become permanent.

`credentials.scrub` and `holds_secret` were written as "a last line, not
the design" and were wired to NOTHING. The last line did not exist. That
was survivable while E0 held no credentials; it stops being survivable
the moment a run can hold one.

TWO SURFACES OUTLIVE THE LEASE:

  * AN ARTIFACT. D2 changed the stakes here. An emitted artifact is no
    longer a file in a workspace that dies with the run: it is
    content-addressed in the store, catalogued as a goal resource with
    provenance, readable by both agents, mountable into later runs, and
    downloadable by a person over HTTP. A token that reaches
    `emit_artifact` outlives every rotation.

  * THE JOURNAL. It is the durable record of what a run did, and
    arguments and model text are exactly where an echoed token lands --
    Stage G watched agents echo tokens through `exec` and `bash`.

REFUSED vs SCRUBBED, and the difference is deliberate. An artifact is
REFUSED: silently altering the bytes would hand back a report the
executor believes it wrote and a reader believes the run produced, with
the difference invisible to both. The journal is SCRUBBED: it is a
record, nobody acts on its exact bytes, and a redacted record is better
than a refused one. In neither case is the live value touched -- the
tool still receives what the model sent.
"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]

TOKEN = "ya29.a0ARrdaM-EXAMPLE-NOT-A-REAL-TOKEN-0123456789abcdef"
PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg"


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the detector ─────────────────────────────────────────────────────────

def test_it_catches_the_shapes_a_gcp_run_would_produce():
    """`GenerateAccessToken` returns a `ya29.` token, and a service
    account key is a private key. Those are the two this build makes
    reachable."""
    from kernel.workspace.credentials import holds_secret

    assert holds_secret(TOKEN)
    assert holds_secret(PRIVATE_KEY)
    assert holds_secret(f"export TOK={TOKEN}")


def test_it_does_not_fire_on_ordinary_report_prose():
    """A guard that refuses real reports is worse than none. These are
    the shapes KER25's errands actually produced."""
    from kernel.workspace.credentials import holds_secret

    for ordinary in (
            "# Floating-Point Boundary Arithmetic Investigation Report",
            "0.1 + 0.2 = 0.30000000000000004",
            "returns HTTP 400 for overflow and 422 for inf",
            "9007199254740992 + 1.0 = 9007199254740992.0",
            'assert r.status_code == 422, f"Expected 422"'):
        assert not holds_secret(ordinary), ordinary


# ── the artifact, which is permanent ─────────────────────────────────────

def test_emit_refuses_an_artifact_holding_a_credential():
    src = _read("kernel/harness/emit.py")
    body = src[src.index("async def emit_artifact"):]
    assert "holds_secret" in body, (
        "emit_artifact does not check for a credential, and an artifact "
        "is catalogued, mountable and downloadable")
    assert "E_ARTIFACT_HOLDS_CREDENTIAL" in body
    # It must refuse BEFORE the bytes reach the store: a content-addressed
    # put is not undone by refusing afterwards.
    #
    # THE WRITE IS FOUND, NOT NAMED. This asserted against the literal
    # "get_store", and broke the day the call became `await aput(...)`
    # for the GCS binding -- with a ValueError from `.index`, not a
    # readable failure, on a property that had not changed at all. The
    # check is about ORDER, so it should locate the write by whichever
    # spelling is present and say so when it cannot find one.
    writes = [w for w in ("aput(", "get_store(", "_store_durably(")
              if w in body]
    assert writes, (
        "cannot find the store write in emit_artifact, so the ordering "
        "below is unverifiable -- if the call was renamed again, add the "
        "new spelling here")
    first_write = min(body.index(w) for w in writes)
    assert body.index("holds_secret") < first_write, (
        "the credential check runs after the store put -- the bytes are "
        "already permanent by then")


def test_the_refusal_says_what_to_do_instead():
    """A refusal the executor cannot act on costs a leg. It has to name
    the case where the finding is ABOUT a credential."""
    src = _read("kernel/harness/emit.py")
    i = src.index("E_ARTIFACT_HOLDS_CREDENTIAL")
    text = src[i:i + 900]
    assert "Nothing was stored" in text
    assert "emit again" in text


# ── the journal, which is durable ────────────────────────────────────────

def test_the_journal_scrubs_arguments_and_model_text():
    # MOVED WITH THE CODE (D2a). The journal INSERT left `loop.py`, which
    # is now a re-export facade -- and a guard reading a file its subject
    # has left does not fail, it passes on no evidence. `loop_step.py` is
    # where the two calls are; if the insert moves again, move this line
    # with it rather than widening the search to the package.
    src = _read("kernel/harness/loop_step.py")
    assert "_scrub_json(tool_args)" in src, "journal args are not scrubbed"
    assert "_scrub_text(response.text)" in src, (
        "model text is not scrubbed, and what the model SAID is where an "
        "echoed token lands")


def test_scrubbing_touches_the_record_and_not_the_value():
    """The tool must still receive what the model sent. Scrubbing the
    live value would change behaviour to protect a record."""
    from kernel.harness.loop import _scrub_json, _scrub_text

    args = {"command": f"gcloud auth print-access-token # {TOKEN}"}
    recorded = _scrub_json(args)
    assert TOKEN not in recorded
    assert "[redacted credential]" in recorded
    # The dict itself is untouched.
    assert TOKEN in args["command"]
    assert _scrub_text(None) is None
    assert _scrub_text("17/17 tests passed") == "17/17 tests passed"


def test_the_lease_directory_is_not_inside_any_repo_checkout():
    """A credential committed to a branch is permanent in a way no
    revocation reaches. The lease lives at the workspace root and repos
    are mounted under repos/, so a `git add -A` in a checkout cannot
    reach it — asserted because the two layouts are defined in different
    files and nothing else pins the relationship."""
    from kernel.workspace.credentials import LEASE_DIR
    from kernel.workspace.mounts import DOC_LAYOUT

    assert not LEASE_DIR.startswith("repos/")
    assert not LEASE_DIR.startswith(DOC_LAYOUT.split("/", 1)[0] + "/")
    assert LEASE_DIR.startswith(".agentos/")
