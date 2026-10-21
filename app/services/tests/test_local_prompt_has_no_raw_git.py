"""The local profiles never tell a session to run git itself.

EVERY ONE OF THESE IS SOMEBODY ELSE'S JOB, and in a prompt they are not
untidiness -- they are instructions the agent will follow:

  `git push`     the extension pushes, with a scoped token the session
                 does not hold and a verification it cannot forge;
  `git merge` /  the catch-up does it, by SHA, under a plan a person
  `git pull`     approved -- a stray pull merges whatever the last fetch
                 happened to leave in `origin/main`, which is a trunk
                 position nobody agreed to;
  `gh pr merge`  the merge is the person's, on GitHub, and it is the
                 only gate between the branch and trunk.

PINNED ON THE PROFILES because that is where a helpful sentence gets
added. Nobody would write "then run `gh pr merge`" into the code; they
would write it into a prompt, as a convenience, and it would read as
the sanctioned route.
"""
from __future__ import annotations

import pathlib
import re

import pytest

PROFILES = pathlib.Path(__file__).resolve().parents[3] / "profiles-local"

FORBIDDEN = [
    (re.compile(r"\bgit\s+pull\b"), "git pull"),
    (re.compile(r"\bgit\s+merge\b"), "git merge"),
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\bgh\s+pr\s+merge\b"), "gh pr merge"),
]


def _files():
    return sorted(p for p in PROFILES.rglob("*")
                  if p.is_file() and p.suffix in (".toml", ".md"))


def test_the_profiles_are_actually_there():
    """A path typo would make every assertion below vacuously true."""
    assert _files(), f"no profile files under {PROFILES}"


@pytest.mark.parametrize("name", [p.name for p in _files()])
def test_no_profile_names_raw_git(name):
    path = next(p for p in _files() if p.name == name)
    text = path.read_text(encoding="utf-8")
    hits = [what for rx, what in FORBIDDEN if rx.search(text)]
    assert hits == [], (
        f"{path.name} tells the session to run {', '.join(hits)} — the "
        "extension pushes, the catch-up merges by sha under an approved "
        "plan, and the person merges the pull request")


def test_the_coding_profile_says_who_does_it_instead():
    """Silence is not the same as an instruction. An agent that is not
    told the extension pushes will try, fail at the boundary, and spend
    a turn working out why."""
    texts = " ".join(p.read_text(encoding="utf-8") for p in _files())
    assert "propose_completion" in texts, (
        "no profile names the verb an agent finishes a story with")
