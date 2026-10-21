"""F5 — a transient GitHub 5xx must not arrive as "Repository not found".

FOUND ON TWO LIVE RUNS. A 500 on `app/installations/*/access_tokens`
produced no usable token, the clone proceeded unauthenticated, and
GitHub answered:

    remote: Repository not found.
    fatal: repository 'https://github.com/<owner>/<repo>.git/' not found

Which is an AUTH FAILURE WEARING A MISSING-REPO MESSAGE. GitHub returns
`not found` for "you may not see this" as well as "this is not here",
deliberately, so that a 404 does not confirm a private repo exists. That
is right for GitHub and useless for the person reading our log: they go
looking for a deleted repository that was there the whole time. A 502 on
the same endpoint stranded a branch (F1) the same way.

Two fixes, because there are two layers that can swallow it:

  * the MINT retries 5xx and transport errors, and nothing else;
  * the CLONE, which knows something GitHub does not -- whether a
    credential helper was configured -- says so when the message is
    ambiguous.
"""
from __future__ import annotations

import asyncio
import io
import pathlib
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the mint retries what is worth retrying ──────────────────────────────

class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "{}"

    def json(self):
        return {"token": "ghs_x", "expires_at": "2026-01-01T00:00:00Z"}


class _Client:
    """Replays a scripted sequence of statuses / raised transport errors."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)


class _Settings:
    github_app_installation_id = "${GITHUB_INSTALLATION_ID}"


def _run(client):
    """Drive the retry coroutine with the backoff sleep neutralised."""
    from app import github_tokens

    async def _nap(_seconds):
        return None

    with patch.object(github_tokens.asyncio, "sleep", _nap):
        return asyncio.run(
            github_tokens._post_access_token(client, _Settings(), "jwt", {}))


def test_a_transient_500_is_retried_and_succeeds():
    client = _Client([500, 502, 201])
    resp = _run(client)
    assert resp.status_code == 201
    assert client.calls == 3, "the 5xx responses were not retried"


def test_a_transport_error_is_retried():
    """The case `--retry` alone does not cover: a connection reset is not
    a status code, and it is the one that actually happened."""
    import httpx

    client = _Client([httpx.ConnectError("reset"), 201])
    resp = _run(client)
    assert resp.status_code == 201
    assert client.calls == 2


def test_a_403_is_not_retried():
    """A 403 is an ANSWER -- the app is not installed on that repo.
    Retrying it delays a correct failure by three seconds and teaches
    nobody anything."""
    client = _Client([403])
    resp = _run(client)
    assert resp.status_code == 403
    assert client.calls == 1, "a permanent refusal was retried"


def test_it_gives_up_rather_than_looping():
    client = _Client([500, 500, 500])
    resp = _run(client)
    assert resp.status_code == 500, "the caller must still see the failure"
    assert client.calls == 3


# ── the broker still fails closed ────────────────────────────────────────

def test_the_broker_refuses_to_serve_an_empty_token():
    """The retry reduces how often this happens; it must not become the
    reason nobody notices when it does. An empty password line is a
    silent unauthenticated clone."""
    src = _read("app/routers/git_credential.py")
    i = src.index('token = result.get("token") or ""')
    assert "raise HTTPException" in src[i:i + 400]
    assert "502" in src[i:i + 400]


# ── the clone explains itself ────────────────────────────────────────────

def test_an_ambiguous_failure_on_a_private_repo_names_the_broker():
    from kernel.workspace.bindings import _clone_failure

    msg = _clone_failure("remote: Repository not found.",
                         "https://github.com/o/r", had_helper=True)
    assert "token broker" in msg
    assert "not that https://github.com/o/r is missing" in msg
    # The original stderr survives -- we are adding a reading, not
    # replacing the evidence.
    assert "Repository not found" in msg


def test_a_public_clone_failure_is_left_alone():
    """With no helper there was no token to fail, so `not found` means
    what it says and a paragraph about the broker is noise."""
    from kernel.workspace.bindings import _clone_failure

    msg = _clone_failure("remote: Repository not found.",
                         "https://github.com/o/r", had_helper=False)
    assert "token broker" not in msg
    assert msg == "clone failed: remote: Repository not found."


def test_an_unambiguous_failure_is_left_alone():
    """A DNS failure is not an auth problem, and saying it might be sends
    the reader to the wrong place -- which is the whole complaint F5 is
    about, inverted."""
    from kernel.workspace.bindings import _clone_failure

    msg = _clone_failure("fatal: unable to access: Could not resolve host",
                         "https://github.com/o/r", had_helper=True)
    assert "token broker" not in msg


def test_the_helper_retries():
    """The helper is a shell one-liner, so this is the only place the
    retry can be asserted at all."""
    src = _read("kernel/workspace/bindings.py")
    i = src.index("def _credential_helper")
    body = src[i:src.index("def _clone_failure")]
    assert "--retry 3" in body
    assert "--retry-all-errors" in body, (
        "without this curl retries statuses but not a connection reset")
    assert "--max-time" in body, "a hung broker would hang the clone"
