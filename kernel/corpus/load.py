"""G1 — the graded task corpus, read off disk.

A TASK IS A CONTRACT. That is the whole design decision and everything
else follows from it: the checks are `criteria` with F11's `goal`/`guard`
labels, grading is a verification, and the four defences F2 built — fresh
container at the contract digest, a different uid, the committed tree,
the command from the contract — apply to a corpus run without a line of
new code. A corpus with its own bespoke grader would be a second thing
to trust, graded by machinery nobody had attacked.

Each task is a directory:

    tasks/<id>/
        task.json        title, checks, and what the task is for
        PROMPT.md        exactly what the executor is told
        solution.patch   a known-correct diff, applied with `git apply`

THE SOLUTION IS NOT AN ANSWER KEY THE EXECUTOR IS GRADED AGAINST. It
exists to grade the TASK: a task whose goals do not fail on the seed
cannot show that anything was done, and one whose goals do not pass on
the solution cannot be satisfied at all. Both are corpus bugs, and
`validate.py` refuses them. Nothing compares an executor's diff to it —
that would be marking style rather than outcome, and it is exactly the
judgement the contract model exists to avoid.
"""

from __future__ import annotations

import io
import json
import logging
import os

logger = logging.getLogger(__name__)

# Where the corpus lives inside the control-plane container. `ci/` is
# bind-mounted at /app/ci, and the corpus sits beside it.
# The membership test, not a silent default: this repository bans
# the short form and checks for it, because a default taken quietly
# is how a service comes up healthy against the wrong thing.
CORPUS_ROOT = (os.environ["CORPUS_ROOT"] if "CORPUS_ROOT" in os.environ else "/app/corpus")

REQUIRED = ("id", "title", "checks")
VALID_LABELS = ("goal", "guard")


class CorpusError(Exception):
    """A task on disk is not a task."""


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def task_dir(task_id: str, root: str | None = None) -> str:
    return os.path.join(root or CORPUS_ROOT, "tasks", task_id)


def repo_dir(root: str | None = None) -> str:
    return os.path.join(root or CORPUS_ROOT, "repo")


def load_task(task_id: str, root: str | None = None) -> dict:
    """One task, validated for shape. Raises rather than returning junk."""
    where = task_dir(task_id, root)
    spec_path = os.path.join(where, "task.json")
    if not os.path.exists(spec_path):
        raise CorpusError(f"{task_id}: no task.json at {spec_path}")
    try:
        spec = json.loads(_read(spec_path))
    except json.JSONDecodeError as e:
        raise CorpusError(f"{task_id}: task.json is not JSON — {e}") from None

    for key in REQUIRED:
        if key not in spec:
            raise CorpusError(f"{task_id}: task.json has no {key!r}")
    if spec["id"] != task_id:
        raise CorpusError(
            f"{task_id}: task.json says id={spec['id']!r}. The directory "
            f"name is the id; a task addressed by two names is a task that "
            f"gets graded twice and counted once.")

    checks = spec["checks"]
    if not isinstance(checks, list) or not checks:
        raise CorpusError(f"{task_id}: a task with no checks grades nothing")
    goals = 0
    for i, c in enumerate(checks):
        if not isinstance(c, dict) or not str(c.get("run", "")).strip():
            raise CorpusError(f"{task_id}: check {i} has no `run`")
        if c.get("label") not in VALID_LABELS:
            raise CorpusError(
                f"{task_id}: check {i} is labelled {c.get('label')!r}; "
                f"every check is a goal (the work) or a guard (what must "
                f"not break)")
        goals += c["label"] == "goal"
    if not goals:
        # Every check a guard means nothing can show the work happened,
        # only that nothing broke — which grades a task the executor
        # passes by doing nothing.
        raise CorpusError(
            f"{task_id}: every check is a guard, so nothing here can show "
            f"the work was done")

    prompt_path = os.path.join(where, "PROMPT.md")
    if not os.path.exists(prompt_path):
        raise CorpusError(f"{task_id}: no PROMPT.md — nothing to ask for")
    patch_path = os.path.join(where, "solution.patch")
    if not os.path.exists(patch_path):
        raise CorpusError(
            f"{task_id}: no solution.patch. Not an answer key — it is what "
            f"proves the task is satisfiable at all.")

    return {
        "id": task_id,
        "title": spec["title"],
        "why": spec.get("why", ""),
        "checks": checks,
        "prompt": _read(prompt_path),
        "patch": _read(patch_path),
        "patch_path": patch_path,
        "dir": where,
        # The repositories the task declares, if any. Dropped here
        # before, so the validator could not tell a task that brings its
        # own repos from one using the shared fixture -- and validated
        # every task as though it were the second.
        "repos": list(spec.get("repos") or []),
        "writable": list(spec.get("writable") or []),
    }


def task_ids(root: str | None = None) -> list[str]:
    where = os.path.join(root or CORPUS_ROOT, "tasks")
    if not os.path.isdir(where):
        return []
    return sorted(
        d for d in os.listdir(where)
        if os.path.isdir(os.path.join(where, d)) and not d.startswith("."))


def load_all(root: str | None = None) -> list[dict]:
    return [load_task(t, root) for t in task_ids(root)]


def as_criteria(task: dict) -> list[dict]:
    """The task's checks in the shape the Verifier takes."""
    return [{"id": -(i + 1), "kind": "command", "held_out": False,
             "label": c["label"], "spec": {"run": c["run"]}}
            for i, c in enumerate(task["checks"])]
