"""The installed model SDK can carry what the models require.

THE FAILURE, AND WHY NOTHING CAUGHT IT. Every leg of executions 10 and
11 made exactly one tool call and died on the second request:

    400 INVALID_ARGUMENT: Function call is missing a thought_signature
    in functionCall parts ... `default_api:list_dir`, position 2

`gemini-3` issues an opaque `thought_signature` with every function call
and requires it back on replay. The adapter already re-emits the model's
own turn verbatim for exactly this reason. The installed SDK could not
see the field:

    google-genai 1.2.0   part keys = [function_call]
    google-genai 2.23.0  part keys = [function_call, thought_signature]

NOBODY PINNED 1.2.0. `requirements.txt` asked for `google-genai>=1.0.0`
and separately pinned `pydantic==2.9.2`. Current google-genai requires
`pydantic>=2.12.5`, so pip did what it is supposed to do -- it
BACKTRACKED the SDK to the newest version compatible with the pin -- and
reported nothing. A hard pin on one package silently chose the version
of another, and the build was green throughout.

IT WORKED LOCALLY because a developer's environment resolves fresh and
gets a current SDK. Same shape as the rest of this class: a fact the
local environment has and the deployed one does not, invisible until it
is expensive.

SO THE CHECK IS ON THE CAPABILITY, NOT THE VERSION STRING. A version
floor in requirements.txt is a claim; whether the installed library can
represent the field is the fact. This asserts the fact, so the day a
transitive pin drags the SDK backwards again, the suite says so instead
of a run dying on its second tool call.
"""
from __future__ import annotations

import pytest


def test_the_gemini_sdk_can_represent_a_thought_signature():
    """The field gemini-3 requires on every replayed function call."""
    genai_types = pytest.importorskip(
        "google.genai.types",
        reason="google-genai is not installed in this environment")

    part = genai_types.Part(
        function_call=genai_types.FunctionCall(name="list_dir",
                                               args={"path": "repos"}))
    assert hasattr(part, "thought_signature"), (
        "the installed google-genai cannot represent thought_signature, so "
        "every gemini-3 function call will be rejected on replay with "
        "400 INVALID_ARGUMENT. This is what `pydantic==2.9.2` did by "
        "backtracking the SDK to 1.2.0 -- check what is capping it before "
        "raising the floor in requirements.txt.")


def test_a_signature_survives_a_round_trip():
    """Present is not the same as carried. The adapter re-emits the raw
    Content object it was given, so the field has to survive being set,
    serialised and read back -- which is what a too-old SDK fails at
    even when the attribute exists."""
    genai_types = pytest.importorskip("google.genai.types")

    part = genai_types.Part(
        function_call=genai_types.FunctionCall(name="list_dir", args={}),
        thought_signature=b"opaque-signature-bytes")
    content = genai_types.Content(role="model", parts=[part])
    dumped = content.parts[0].model_dump(exclude_none=True)
    assert "thought_signature" in dumped, (
        "the signature is dropped on serialisation, so replaying the "
        "model's own turn loses it")


def test_the_floor_in_requirements_matches_what_is_installed():
    """A floor nobody satisfies is a comment.

    Read from the file rather than asserted as a number here, so the two
    cannot drift: this fails if requirements.txt is lowered without the
    installed SDK following it down, which is the state that existed for
    however long it took to notice.
    """
    import pathlib
    import re

    genai = pytest.importorskip("google.genai")
    text = (pathlib.Path(__file__).resolve().parents[3]
            / "requirements.txt").read_text(encoding="utf-8")
    match = re.search(r"^google-genai>=(\d+)\.", text, re.M)
    assert match, "google-genai lost its floor in requirements.txt"
    floor_major = int(match.group(1))
    installed_major = int(genai.__version__.split(".")[0])
    assert installed_major >= floor_major, (
        f"requirements.txt asks for google-genai>={floor_major}.x and "
        f"{genai.__version__} is installed. Something is capping it -- "
        f"look for a hard pin on pydantic or httpx, which is how it got "
        f"to 1.2.0 while the file said >=1.0.0 and everyone read that as "
        f"'current'.")
