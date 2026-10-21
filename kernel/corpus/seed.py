"""H13 — turning a graded task into a story you can actually run.

Five corpus tasks already existed as CONTRACTS — a task.json with
`goal`/`guard` checks and a solution patch proving those checks
discriminate. What did not exist was a way to put one in front of an
executor: `kernel.corpus.run` grades a whole sweep for measurement, and
starting one story to watch it work meant assembling the execution, the
origin, the mounts and the contract by hand.

WHY THE ORIGIN IS BUILT PER RUN. The workspace must hold the task
UNDONE. H0x spent four attempts discovering that a shared, persistent
tree means the first run does the work and every run afterwards starts
with it already done — measuring nothing, and reporting PASS. So each
seeding builds a fresh origin and the workspace is cloned from it.

WHAT A TASK CAN NOW BE. A single repo, several repos, or an errand; and
one of them deliberately cannot be finished without asking a person,
because the card path and the relay deserve a fixture rather than being
exercised by hand every time.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

TASK_ROOT = "/app/corpus/tasks"
ERRAND_ROOT = "/app/corpus/errands"
SHARED_REPO = "/app/corpus/repo"


def _git(path: str, *args: str):
    return subprocess.run(["git", *args], cwd=path, capture_output=True,
                          check=True)


def load(task_id: str) -> dict:
    """A task's definition, from either directory."""
    for root in (TASK_ROOT, ERRAND_ROOT):
        path = os.path.join(root, task_id)
        if os.path.isdir(path):
            spec = json.load(open(os.path.join(path, "task.json"),
                                  encoding="utf-8"))
            spec["_dir"] = path
            spec["_is_errand"] = root == ERRAND_ROOT
            prompt = os.path.join(path, "PROMPT.md")
            spec["prompt"] = (open(prompt, encoding="utf-8").read()
                              if os.path.isfile(prompt) else spec["title"])
            return spec
    raise FileNotFoundError(f"no task {task_id!r} in {TASK_ROOT} or "
                            f"{ERRAND_ROOT}")


def available() -> list:
    """Every task that can be seeded, with what kind of run it is."""
    out = []
    for root, errand in ((TASK_ROOT, False), (ERRAND_ROOT, True)):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not os.path.isfile(os.path.join(root, name, "task.json")):
                continue
            spec = json.load(open(os.path.join(root, name, "task.json"),
                                  encoding="utf-8"))
            out.append({"id": name, "title": spec.get("title", name),
                        "errand": errand,
                        "repos": len(spec.get("repos") or ["repo"]),
                        "why": spec.get("why", "")})
    return out


def build_origins(spec: dict, into: str) -> list:
    """A fresh origin per repository the task needs. Returns [(name, path)].

    `repos` in task.json names subdirectories of the task's own folder;
    a task without it gets the shared fixture repo, which is what the
    five existing ones use.
    """
    shutil.rmtree(into, ignore_errors=True)
    os.makedirs(into, exist_ok=True)
    names = spec.get("repos") or [None]

    built = []
    for name in names:
        source = (os.path.join(spec["_dir"], name) if name else SHARED_REPO)
        label = name or "fixture"
        path = os.path.join(into, label)
        shutil.copytree(source, path,
                        ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                      "*.pyc"))
        _git(path, "init", "-q")
        _git(path, "config", "user.email", "corpus@agentos.local")
        _git(path, "config", "user.name", "AgentOS Corpus")
        _git(path, "add", "-A")
        _git(path, "commit", "-qm", f"seed: {spec['id']}")
        built.append((label, path))
    return built


async def seed(conn, task_id: str, goal_id: str, origins_root: str,
               workspace_root: str) -> dict:
    """Create a runnable execution for one task. Returns what it made."""
    from kernel.workspace.mounts import Mount, record

    spec = load(task_id)
    origins = build_origins(spec, os.path.join(origins_root, task_id))

    kind = "errand" if spec["_is_errand"] else "story"
    # THE DOMAIN IS NOT ALWAYS `coding`. The registered profiles are
    # errand/ops, story/coding and story/business, and an errand seeded
    # as errand/coding is refused at dispatch with UnknownProfileError --
    # after the origin has been built and the execution row written, so
    # the failure arrives detached from the thing that caused it. A task
    # may name its own; otherwise the kind decides.
    domain = spec.get("domain") or ("ops" if spec["_is_errand"] else "coding")
    eid = await conn.fetchval(
        """
        INSERT INTO executions (kind, domain, goal_id, state, task)
        VALUES ($1, $2, $3, 'PENDING', $4) RETURNING id
        """,
        kind, domain, goal_id, spec["prompt"])

    # One mount per origin. A task naming two repositories gets two, and
    # `writable` comes from the task rather than being assumed -- a
    # review task grants repositories it may not touch, which is what
    # E7f made expressible.
    writable = set(spec.get("writable") or [n for n, _ in origins])
    # A FIXTURE NAMES ITS OWN MOUNT. `repo_path` derives
    # repos/<owner>__<name> from a clone URL, which is right for a real
    # remote and wrong here: the origin is a temporary directory, so the
    # "owner" would be whatever folder the harness happened to build it
    # in, and a task's checks could not name a stable path.
    mounts = tuple(
        Mount(at=f"repos/{label}", binding="git", source=path,
              writable=(label in writable), kind="repo")
        for label, path in origins)
    await record(conn, eid, mounts)
    await conn.execute(
        "UPDATE executions SET workspace_path = $2 WHERE id = $1",
        eid, os.path.join(workspace_root, str(eid)))

    logger.info("seeded %s as execution %s (%d repo(s), %d writable)",
                task_id, eid, len(mounts), len(writable))
    return {"execution_id": eid, "task": task_id, "kind": kind,
            "domain": domain,
            "checks": spec.get("checks", []),
            "mounts": [m.at for m in mounts],
            "origins": [p for _, p in origins]}
