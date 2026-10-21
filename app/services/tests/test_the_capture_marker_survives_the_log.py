"""A capture that worked must not be reported as one that did nothing.

MEASURED ON EXECUTION 207. The sidecar printed exactly what it should:

    capture: 45864960 bytes from /tmp/verify (work exited 0)
    AGENTOS_CAPTURE_ID 76d0b59c6419a672f1e4dba7f432cb1364a98e76a2a5db45...

45 megabytes stored, work exited 0, the id on its own line. And the
verifier said:

    the capture sidecar stored nothing, so the environment replay has no
    directory to carry

`_logs` was annotated `-> str` and returned whatever the Kubernetes
client handed back, which was `bytes`. `artifact_id_from` then compared
`b'AGENTOS_CAPTURE_ID'` to `'AGENTOS_CAPTURE_ID'`, and in Python 3 those
are never equal -- so the marker was never found, on any run.

THE SHAPE IS WHAT MATTERS, not the encoding. This is a RETURN CHANNEL: a
content-addressed store means the caller cannot know the id in advance,
so the sidecar's stdout is the only way the id travels. A parser that
silently answers None turns every success into a reported failure, and
the failure it reports points at the wrong component -- three separate
sessions went looking at egress, IAM and the artifact store for a fault
that was a type mismatch in a string compare.

Two layers on purpose. `_logs` decodes, which is the real fix and the
right seam for it -- one place logs enter the process. `artifact_id_from`
also tolerates bytes, because it is the single point where the whole
mechanism can fail silently.
"""
from __future__ import annotations

from kernel.artifacts.capture import MARKER
from kernel.drivers.k8s_capture import artifact_id_from

ID = "76d0b59c6419a672f1e4dba7f432cb1364a98e76a2a5db452290dcd9a36340a1"
REAL = (f"capture: 45864960 bytes from /tmp/verify (work exited 0)\n"
        f"{MARKER} {ID}\n")


# ── the regression ─────────────────────────────────────────────────────

def test_the_marker_is_found_in_a_decoded_log():
    assert artifact_id_from(REAL) == ID


def test_the_marker_is_found_even_if_the_log_arrives_as_bytes():
    """THE ONE THAT WOULD HAVE CAUGHT IT. Every capture on Kubernetes
    took this path and every one of them reported storing nothing."""
    assert artifact_id_from(REAL.encode()) == ID


def test_the_marker_survives_a_str_containing_a_bytes_repr():
    """EXECUTION 208, and the reason this file has two bytes tests.

    The obvious `bytes` case was fixed first and the symptom did not
    change, because the client was not returning bytes at all -- it was
    returning a `str` whose CONTENT is a bytes repr:

        b'AGENTOS_CAPTURE_ID 07c760fc...\ncapture: 45864960 bytes...\n'

    A real `b`, real quotes, and backslash-n instead of newlines.
    `isinstance(x, bytes)` is False, `splitlines()` returns one line, and
    the marker is invisible -- the identical failure wearing a type that
    the first fix does not catch. The printed evidence looks the same
    either way, which is what cost the second deploy cycle."""
    assert artifact_id_from(repr(REAL.encode())) == ID


def test_the_two_bytes_shapes_are_normalised_by_one_function():
    """One spelling, because two is how the second round happened."""
    from kernel.drivers.k8s_run import as_text

    assert as_text(REAL.encode()) == REAL
    assert as_text(repr(REAL.encode())) == REAL
    assert as_text(REAL) == REAL


def test_text_that_merely_starts_with_b_is_left_alone():
    """`literal_eval` must not eat ordinary output. A criterion that
    prints `b"something"` as its own text is not a bytes repr of a log,
    and mangling it would be a new bug in the name of fixing an old
    one."""
    from kernel.drivers.k8s_run import as_text

    for plain in ("building the wheel", "b'unterminated",
                  "bytes are fine", "b'a' and then more text"):
        assert as_text(plain) == plain


def test_a_log_with_no_marker_is_still_nothing():
    """The negative has to keep working, or the fix above just makes
    every failure look like a success."""
    assert artifact_id_from("capture: OSError: Project was not passed\n") is None
    assert artifact_id_from(b"") is None
    assert artifact_id_from(None) is None


def test_the_last_marker_wins():
    """A retried sidecar prints more than one, and the newest is the one
    whose bytes are in the store."""
    log = f"{MARKER} aaa\nnoise\n{MARKER} bbb\n"
    assert artifact_id_from(log) == "bbb"


def test_undecodable_output_does_not_lose_the_marker():
    """A container may emit anything. Losing the whole log -- and with
    it the id -- over one stray byte would be the same failure with a
    different cause, so the decode replaces rather than raises."""
    assert artifact_id_from(b"\xff\xfe garbage\n" + f"{MARKER} {ID}".encode()) == ID


# ── and the seam that should have made the above unnecessary ───────────

def test_the_log_reader_promises_a_string():
    """`_logs` is where logs enter this process. A caller that has to
    remember to decode is this bug waiting for its next caller."""
    import pathlib

    from kernel.drivers import k8s_run

    src = pathlib.Path(k8s_run.__file__).read_text(encoding="utf-8")
    body = src[src.index("async def _logs("):]
    body = body[:body.index("async def _delete(")]
    assert "isinstance(out, bytes)" in body
    assert 'decode("utf-8", "replace")' in body
