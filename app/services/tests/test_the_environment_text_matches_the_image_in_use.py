"""B7 -- the model is told about the machine it is actually on.

`base_image.for_goal` exists so a goal can name its own image: "a goal
that works on a Rust repository names a Rust image here once". With one
constant describing one image, the FIRST goal to use that gets an
executor told about a compiler it may not have, an `apt` rule that may
not apply, and package managers that are not installed -- every one of
them a decision the text exists to inform.

Two cautions this had to satisfy, and both are tested: the readers are
inside the pod and hold no connection, so the answer has to travel on
the spec; and the text sits in the stable prefix, so resolving it must
not become a per-leg database read.
"""

from __future__ import annotations

import inspect

from kernel import env_text


# -- which text, for which image ---------------------------------------

def test_our_own_image_gets_the_full_description():
    for name in ("agentos-kernel/fixture:dev",
                 "us-central1-docker.pkg.dev/p/agentos-staging/sandbox-base:abc",
                 "us-central1-docker.pkg.dev/p/agentos-staging/sandbox-base"
                 "@sha256:0123"):
        assert env_text.text_for(name) is env_text.AGENTOS, name


def test_a_plain_python_image_is_not_described_as_ours():
    """It has no compiler, no Node and not necessarily git -- and
    `SHIM_IMAGE` is exactly this image."""
    got = env_text.text_for("python:3.12-slim")
    assert got is env_text.PYTHON_SLIM
    assert "NO compiler" in got


def test_an_image_we_do_not_build_says_so():
    """Describing an unknown image as if it were ours is the worst
    shape: the model acts on specifics that are false."""
    got = env_text.text_for("rust:1.83-bookworm")
    assert got is env_text.UNKNOWN
    assert "CHECK" in got and "--version" in got


def test_an_unset_image_is_unknown_and_not_ours():
    """A pod dispatched by code that has not learned to set the variable
    must say it does not know, not describe whatever we last built."""
    assert env_text.text_for("") is env_text.UNKNOWN
    assert env_text.text_for(None) is env_text.UNKNOWN


def test_every_text_still_refuses_apt():
    """True of every image, because it is a property of the SANDBOX --
    non-root, discarded between rounds -- and not of the image."""
    for text in (env_text.AGENTOS, env_text.PYTHON_SLIM, env_text.UNKNOWN):
        assert "`apt` IS NOT AVAILABLE" in text
        assert "env_setup" in text


def test_ours_describes_the_image_we_actually_build():
    """The row in B3's table that had no test: the text claimed 3.12 and
    Node 22 while the image moved. It has now moved twice -- 3.12 to
    3.14 and back -- and this is what made the second move three edits
    instead of a hunt.

    READ THE MAJOR.MINOR OUT OF THE DOCKERFILE rather than pinning a
    literal here: a version named in two files is a version that drifts
    in one of them, which is the whole defect this test exists for.
    """
    import re

    image = open("infra/e0/fixtures/Dockerfile", encoding="utf-8").read()
    text = env_text.AGENTOS

    built = re.search(r"python:(\d+\.\d+)-slim-trixie", image)
    assert built, "the Dockerfile no longer records which python tag it pins"
    assert f"Python {built.group(1)}" in text, (
        f"the image builds python {built.group(1)} and the text the model "
        f"reads says otherwise")

    node = re.search(r"node:(\d+)-trixie-slim", image)
    assert node and f"Node {node.group(1)}" in text
    assert "rustc" in text and "rustc cargo" in image
    assert "trixie" in text and "trixie" in image


# -- how the answer reaches the pod ------------------------------------

def test_the_conductor_writes_the_resolved_image_onto_the_spec():
    """Both readers run inside the pod and neither holds a connection or
    a goal id, so `for_goal` is not callable from either."""
    from kernel.conductor import sandbox_spec

    src = inspect.getsource(sandbox_spec.spec_for_dispatch)
    assert "spec.env[_IMAGE_IN_USE] = spec.image" in src


def test_it_is_written_after_every_other_chooser():
    """The contract, the goal and the deployment default all have a say;
    an earlier value would describe an image the run does not use."""
    from kernel.conductor import sandbox_spec

    src = inspect.getsource(sandbox_spec.spec_for_dispatch)
    assert src.index("spec.image = chosen") < src.index("spec.env[_IMAGE_IN_USE]")
    assert src.index("if image:") < src.index("spec.env[_IMAGE_IN_USE]")


def test_the_variable_is_not_credential_shaped():
    """`assert_no_credentials` refuses anything ending _KEY/_TOKEN/
    _SECRET/_PASSWORD on a spec's env."""
    from kernel.conductor.env_guard import assert_no_credentials

    assert_no_credentials({env_text.ENV_VAR: "some/image:tag"})


def test_the_reader_takes_it_from_the_environment(monkeypatch):
    monkeypatch.setenv(env_text.ENV_VAR, "agentos-kernel/fixture:dev")
    assert env_text.environment_text() is env_text.AGENTOS
    monkeypatch.setenv(env_text.ENV_VAR, "golang:1.23")
    assert env_text.environment_text() is env_text.UNKNOWN
    monkeypatch.delenv(env_text.ENV_VAR)
    assert env_text.environment_text() is env_text.UNKNOWN


# -- the stable prefix must stay stable --------------------------------

def test_resolving_it_is_not_a_database_read():
    """The text sits in the stable prefix, and anything time-varying
    there turns every step into a cache miss -- 8.3 prices the
    difference at $60 against $314."""
    lines = [l for l in inspect.getsource(env_text).splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    start = lines.index("from __future__ import annotations")
    code = chr(10).join(lines[start:])
    for forbidden in ("fetchval", "fetchrow", "await ", "asyncpg", "conn."):
        assert forbidden not in code, forbidden
    assert "async def" not in code, (
        "an async resolver here is a per-leg read waiting to happen")


def test_the_prompt_is_byte_identical_across_two_builds(monkeypatch):
    monkeypatch.setenv(env_text.ENV_VAR, "agentos-kernel/fixture:dev")
    assert env_text.environment_text() == env_text.environment_text()


def test_both_readers_ask_the_same_function():
    """Two spellings would be two descriptions of one machine."""
    for path in ("kernel/profiles/prompt.py", "kernel/worker/scout.py"):
        src = open(path, encoding="utf-8").read()
        assert "environment_text" in src, path
        assert "import ENVIRONMENT" not in src, path


def test_the_old_name_still_means_ours():
    """Every existing reference to `ENVIRONMENT` meant our image."""
    from kernel.base_image import ENVIRONMENT

    assert ENVIRONMENT is env_text.AGENTOS
