"""G1 — grading the corpus before the corpus grades anything.

Stage G's whole subject is proving rather than asserting, and a task
nobody has proved is a task that will report a number with no meaning
behind it. So every task must earn its place by discriminating, in
exactly the sense F11 gave the word:

    on the SEED       every `goal` must FAIL     or the task can be
                                                 passed by doing nothing
    on the SEED       every `guard` must PASS    or the task starts
                                                 already broken and the
                                                 guard is really a goal
    on the SOLUTION   every check must PASS      or the task cannot be
                                                 satisfied at all

Two verifications per task, in the same containers a real run uses. That
is the cost, and it is the right one: a corpus validated by reading it
is a corpus validated by opinion.

The three rules are F11's, pointed at the corpus instead of at a plan.
The difference is that here they are HARD — a plan's dry-run warns a
person who can judge, and nobody is watching a corpus run.
"""

from __future__ import annotations

import logging
import os

from kernel.corpus.load import as_criteria, repo_dir
from kernel.corpus.workspace import apply_solution, checkout, cleanup

_GIT = None  # bound lazily; see _seed_workspace

logger = logging.getLogger(__name__)

# The corpus runs its checks where a real story's would run: the same
# image, so a task that only grades under some other toolchain is a task
# that will surprise somebody later.
IMAGE = "agentos-kernel/fixture:dev"
TIMEOUT = 180.0


def _contract() -> dict:
    return {"base_image": IMAGE, "base_digest": None, "workspace_spec": {}}


async def _run(driver, criteria, workspace, commit):
    from kernel.verifier.verify import verify_claim

    results = await verify_claim(driver, _contract(), criteria, workspace,
                                 commit, timeout=TIMEOUT)
    return [outcome for _criterion, outcome in results]



def _seed_workspace(task: dict, where: str, root: str | None) -> str:
    """Lay the task's repositories out the way its checks address them.

    TWO SHAPES, and the validator only ever knew one. The five original
    tasks use the shared fixture repo and address it as the workspace
    root. H13 added tasks that declare their own `repos`, mounted at
    `repos/<name>` -- and this still checked out the shared repo
    unconditionally, so a multi-repo task could not be validated at all.
    It surfaced as a missing solution.patch, because the loader refused
    first and nothing got as far as the layout.

    ONE GIT REPOSITORY EITHER WAY. The solution diff is applied with
    `git apply` across the whole workspace, so a task spanning two
    repositories needs them under one worktree here -- which is also how
    the composite workspace presents them to a real executor.
    """
    import shutil
    import subprocess

    from kernel.corpus.seed import build_origins

    names = task.get("repos") or []
    if not names:
        return checkout(repo_dir(root), where)

    def git(*args):
        return subprocess.run(["git", *args], cwd=where, check=True,
                              capture_output=True, text=True)

    shutil.rmtree(where, ignore_errors=True)
    os.makedirs(os.path.join(where, "repos"), exist_ok=True)
    # build_origins reads `_dir`; load_task calls it `dir`. Two names
    # for one thing, and the reason this join was never made.
    spec = dict(task)
    spec["_dir"] = task.get("_dir") or task.get("dir", "")
    for label, origin in build_origins(spec, os.path.join(where, ".origins")):
        shutil.copytree(origin, os.path.join(where, "repos", label),
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
    shutil.rmtree(os.path.join(where, ".origins"), ignore_errors=True)

    git("init", "-q")
    git("config", "user.email", "corpus@agentos.local")
    git("config", "user.name", "AgentOS Corpus")
    git("add", "-A")
    git("commit", "-qm", f"seed: {task['id']}")
    return git("rev-parse", "HEAD").stdout.strip()


async def validate_task(driver, task: dict, scratch: str,
                        root: str | None = None) -> dict:
    """Run one task's checks on the seed and on the solution.

    Returns a report. `ok` is the only field a caller must read; the
    rest is what to show a person when it is False.
    """
    where = os.path.join(scratch, f"corpus-{task['id']}")
    criteria = as_criteria(task)
    labels = [c["label"] for c in task["checks"]]
    commands = [c["run"] for c in task["checks"]]
    problems: list[str] = []

    try:
        seed = _seed_workspace(task, where, root)
        before = await _run(driver, criteria, where, seed)

        for cmd, label, outcome in zip(commands, labels, before):
            if label == "goal" and outcome.passes != "fail":
                problems.append(
                    f"GOAL ALREADY {outcome.passes.upper()} ON THE SEED: "
                    f"{cmd} — it cannot show the work was done, so this "
                    f"task can be passed by doing nothing")
            if label == "guard" and outcome.passes != "pass":
                problems.append(
                    f"GUARD {outcome.passes.upper()}S ON THE SEED: {cmd} — "
                    f"a guard is what must not break, and this is already "
                    f"broken. If fixing it is part of the task it is a goal")

        solved = apply_solution(where, task["patch"])
        after = await _run(driver, criteria, where, solved)

        for cmd, label, outcome in zip(commands, labels, after):
            if outcome.passes != "pass":
                problems.append(
                    f"{label.upper()} {outcome.passes.upper()}S ON THE "
                    f"SOLUTION: {cmd} — the known-correct diff does not "
                    f"satisfy this task, so nothing can")
    except Exception as e:                                  # noqa: BLE001
        problems.append(f"{type(e).__name__}: {e}")
        before = after = []
        seed = solved = None
    finally:
        cleanup(where)

    report = {
        "id": task["id"], "title": task["title"],
        "ok": not problems, "problems": problems,
        "seed": seed, "solution": solved,
        "before": [o.passes for o in before],
        "after": [o.passes for o in after],
        "labels": labels,
    }
    if problems:
        logger.warning("corpus task %s is not gradeable: %s",
                       task["id"], "; ".join(problems))
    return report


async def validate_all(driver, tasks, scratch: str,
                       root: str | None = None) -> dict:
    reports = [await validate_task(driver, t, scratch, root) for t in tasks]
    bad = [r for r in reports if not r["ok"]]
    return {"ok": not bad, "tasks": reports, "gradeable": len(reports) - len(bad),
            "total": len(reports)}
