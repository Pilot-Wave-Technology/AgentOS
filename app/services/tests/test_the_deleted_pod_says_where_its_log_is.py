"""A message that says the evidence is gone stops the investigation.

MEASURED 2026-09-17. `run_job` deletes its Job in a `finally` on every
run — deliberately — so any reader that arrives after the delete finds
no pod. What it was then told was:

    (no pod found for this job; its output is unavailable)

and, for a Job that failed:

    the Job failed and its pod is gone, so there is no record of what
    the criterion did

Both sentences are FALSE in the part that matters. Every container's
stdout is shipped to Cloud Logging, indexed by pod name, and outlives
the pod:

    gcloud logging read 'resource.type="k8s_container"
      resource.labels.namespace_name="agentos-staging"
      resource.labels.pod_name="<pod>"' --freshness=1h

The cost was not cosmetic. These strings land in
`criterion_results.detail` and in log lines, and three separate
investigations in one day read "unavailable", believed the output did
not exist, and went at the wrong component instead of running that
query. A message about the EVIDENCE when the true fact is about the
READER's REACH is the expensive kind of wrong.

So the tests below assert two things and not one: that the text names
where to look, and that no spelling of the old claim has crept back
into the modules that produce it.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from kernel.drivers.k8s_logref import log_query, logs_are_in_cloud_logging
from kernel.drivers.k8s_run import _ended, _logs

NS = "agentos-staging"


class _Pod:
    def __init__(self, name):
        self.metadata = type("M", (), {"name": name})()


class _Core:
    """A control plane that will not hand over a log."""

    def read_namespaced_pod_log(self, **kwargs):
        raise RuntimeError("the API server said no")


class _Status:
    conditions = []


# ── the sentence itself ────────────────────────────────────────────────

def test_the_query_names_the_pod_when_the_pod_is_known():
    q = log_query(pod="cap-abc123-x9k2p", namespace=NS)
    assert 'resource.type="k8s_container"' in q
    assert f'resource.labels.namespace_name="{NS}"' in q
    assert 'resource.labels.pod_name="cap-abc123-x9k2p"' in q


def test_the_query_falls_back_to_the_job_name_as_a_prefix():
    """Kubernetes appends a suffix to the Job's name, so the Job name is
    a prefix of its pod's — which Cloud Logging's `:` operator matches.
    Worse than exact and far better than the nothing the reader had."""
    q = log_query(job="cap-abc123", namespace=NS)
    assert 'resource.labels.pod_name:"cap-abc123"' in q


def test_the_sentence_never_says_the_output_is_unavailable():
    said = logs_are_in_cloud_logging(job="cap-abc123", namespace=NS,
                                     container="work")
    assert "unavailable" not in said
    assert "Cloud Logging" in said
    assert "cap-abc123" in said
    assert "gcloud logging read" in said


# ── the two places k8s_run produced it ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_missing_pod_is_told_where_its_log_went():
    """The ORDINARY path, not an exception: `run_job`'s own `finally`
    deletes the Job, so this fires whenever a reader loses the race."""
    out = await _logs(_Core(), None, NS, "work", "cap-abc123")
    assert "unavailable" not in out
    assert "Cloud Logging" in out
    assert "cap-abc123" in out


@pytest.mark.asyncio
async def test_a_log_read_that_failed_hands_back_the_exact_pod_name():
    """This branch HAS the pod name — the one key Cloud Logging indexes
    on — so the reader gets an exact query rather than a prefix."""
    out = await _logs(_Core(), _Pod("cap-abc123-x9k2p"), NS, "work", "c")
    assert out != "(log unavailable)"
    assert 'resource.labels.pod_name="cap-abc123-x9k2p"' in out


def test_a_failed_job_with_no_pod_admits_only_what_is_really_lost():
    """The CLASSIFICATION is lost — only the pod's containerStatuses
    tell a criterion that returned 1 from an image that would not pull.
    The OUTPUT is not, and the old wording conflated the two."""
    result = _ended(None, "crit-abc123", _Status(), "criterion", NS)
    assert result["exit_code"] is None
    why = result["infra_error"]
    assert "no record of what the criterion did" not in why
    assert "Cloud Logging" in why
    assert "crit-abc123" in why


# ── and no copy of the old claim is left ───────────────────────────────

BANNED = ("output is unavailable", "log unavailable",
          "no record of what the criterion did")

SCANNED = ("kernel/drivers", "kernel/verifier")


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


_HAS_DOC = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstrings(tree: ast.AST) -> set[int]:
    """The id() of every node that is a docstring, so the walk can skip
    them by identity rather than by guessing at indentation."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, _HAS_DOC) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and \
                    isinstance(first.value, ast.Constant) and \
                    isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def test_no_module_still_asserts_that_the_output_does_not_exist():
    """STRING LITERALS ONLY, via the AST. Prose about the incident is
    how this codebase remembers why a fix exists — `orphans.py` quotes
    the old message in a comment on purpose — and a grep would forbid
    the memory along with the bug. What must not survive is text that
    REACHES A READER."""
    root = _repo_root()
    offences = []
    for rel in SCANNED:
        for path in sorted((root / rel).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = _docstrings(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or \
                        not isinstance(node.value, str):
                    continue
                if id(node) in docs:
                    continue
                for phrase in BANNED:
                    if phrase in node.value:
                        offences.append(f"{rel}/{path.name}:{node.lineno} "
                                        f"{phrase!r}")
    assert not offences, (
        "these strings tell a reader the output does not exist; it does, "
        "in Cloud Logging: " + "; ".join(offences))
