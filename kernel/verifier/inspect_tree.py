"""How the assessor and the investigator look at a verified tree.

TWO BACKENDS, ONE SURFACE, and the split is the same one the rest of
Phase E draws: a substrate with persistent claims verifies IN THE
VERIFICATION POD, and Docker and E0 have no pod so they do it from a
tar.

WHY THIS MODULE EXISTS AT ALL. `assess` and `investigate` both took
`tree_tar: bytes` and answered their three tools from it -- `list_files`
from a name list parsed out of the archive, `read_file` by re-opening
the archive in memory, and `run` by starting AN EPHEMERAL CONTAINER PER
COMMAND with the tree unpacked into it. On the warm path there is no
tar, so both were skipped entirely: `stage.py` is the only thing that
sets `report["_tree_tar"]` and the warm path does not call it. Green
verdicts, faster, with the second key silently absent.

AND THE POD IS THE BETTER ANSWER, not merely the available one. An
ephemeral container gets the TREE and nothing else, so the investigator
explaining a failed check could not see what that check left behind --
no `.venv`, no build output, no partial artefact, none of the state that
makes a failure legible. Running in the pod the checks ran in, it reads
the actual aftermath. That is the difference between "your test failed"
and "your test failed because the package installed but the entry point
is missing".

THE POD BACKEND SPENDS ONE HTTP CALL PER TOOL. `shell.py` already
argues this at length: fifteen investigation steps used to be fifteen
Jobs, fifteen pods, fifteen image pulls and fifteen fetches of one
unchanged directory, against a twenty-step budget and a six-minute wall
clock -- and the first live run hit the stop-loss with its report
unwritten.

MEMORY. `TarTree` holds a whole archive in `bytes`, which is the
`io.BytesIO` wall plan 5.5 E5 names as what actually breaks large
codebases. `PodTree` holds nothing: the tree is on the volume and every
answer is a command. So the substrate that runs big repositories is the
one that stopped reading them into memory.
"""
from __future__ import annotations

import logging
import shlex

logger = logging.getLogger(__name__)

# What a tool may hand back to a model. Enough to reason with, bounded
# so one `cat` of a vendored bundle cannot spend the context the
# investigation needs to finish.
MAX_READ = 20_000
MAX_OUTPUT = 8_000
MAX_LISTING = 400


class PodTree:
    """The warm verification pod: where the checks actually ran.

    `cwd` is `volume.TREE` for every tool, the same directory
    `batch.run` gives the criteria, so a path the model saw in a failure
    message is the path it can read here.
    """

    def __init__(self, host: str, *, prepend: str = "", env: dict | None = None,
                 timeout_s: float = 120.0):
        self._host = host
        self._prepend = prepend
        self._env = env or {}
        self._timeout = timeout_s

    def __repr__(self) -> str:                              # pragma: no cover
        return f"<PodTree {self._host}>"

    def _preamble(self) -> str:
        """The environment a criterion would have had.

        Without it the investigator's `python` is the image's and not
        the built `.venv`'s, so it reports a missing package that the
        check itself could import -- an explanation that sends the
        executor after the wrong thing.
        """
        parts = [f"export {k}={shlex.quote(str(v))};"
                 for k, v in sorted(self._env.items())]
        if self._prepend:
            parts.append(f'export PATH={shlex.quote(self._prepend)}:"$PATH";')
        return " ".join(parts)

    async def _sh(self, command: str) -> dict:
        from kernel.verifier import shell                    # noqa: PLC0415
        from kernel.verifier import volume                   # noqa: PLC0415

        full = (f"cd {shlex.quote(volume.TREE)} && "
                + self._preamble() + " " + command)
        return await shell.run(self._host, full, self._timeout)

    async def list_files(self, prefix: str = "") -> str:
        """Flat when it fits, the SHAPE when it does not, total always.

        `sort | head -400` is an ALPHABETICAL PREFIX of the tree, not a
        sample, and it said so nowhere. Measured on this repository:
        2,447 files, the cut landing inside `app/services/`, and 14 of
        488 test files shown -- nothing under `tests/` at all. An
        assessor asked "are there tests for this?" would answer no,
        confidently, and refuse work that is tested.

        Raising the cap does not fix that; the bias is in `sort | head`.
        So a large tree gets per-directory counts instead -- how anyone
        orients in an unfamiliar repository. A few hundred bytes whether
        the tree is 2,000 files or 80,000, and it shows that `tests/`
        EXISTS so the model can narrow into it.
        """
        clean = (prefix or "").strip("/")
        where = shlex.quote("./" + clean) if clean else "."
        find = f"find {where} -type f -not -path '*/.git/*'"
        strip = r"sed 's|^\./||'"
        count = r'"$(find "$d" -type f | wc -l)"'
        script = (
            f'N=$({find} | wc -l); '
            f'if [ "$N" -le {MAX_LISTING} ]; then '
            f'{find} | {strip} | sort; '
            f'else '
            f'echo "TREE: $N files here. Showing the shape -- narrow with '
            f'the prefix argument, or use run with find/grep."; '
            f'find {where} -mindepth 1 -maxdepth 1 -type d -not -name .git '
            f'| while read -r d; do printf "%s/ (%s files)\\n" "${{d#./}}" '
            f'{count}; done | sort; '
            f'find {where} -maxdepth 1 -type f | {strip} | sort; '
            f'fi')
        got = await self._sh(script)
        out = (got.get("output") or "").strip()
        return out or "(nothing under that prefix)"

    async def read_file(self, path: str) -> str:
        """The first `MAX_READ` bytes, AND WHETHER THERE WERE MORE.

        The more dangerous of the three silences: a model that reads two
        thirds of a file and finds no `validate()` concludes there is
        none, and refuses on evidence it believes it has.
        """
        clean = (path or "").strip("/")
        if not clean:
            return "(no path given)"
        q = shlex.quote(clean)
        more = (f'printf "\\n... [truncated: showing {MAX_READ} of %s '
                f'bytes. Use run with sed for a later range.]\\n" "$S"')
        got = await self._sh(
            f'head -c {MAX_READ} -- {q}; S=$(wc -c < {q}); '
            f'if [ "$S" -gt {MAX_READ} ]; then {more}; fi')
        if got.get("exit_code"):
            return f"(no such file: {path})"
        return got.get("output") or ""

    async def run(self, command: str) -> str:
        if not (command or "").strip():
            return "(empty command)"
        got = await self._sh(command)
        # `shell.run` never raises on a bridge fault; an unreachable pod
        # is not a verdict and must not read as a failing command.
        if got.get("error"):
            return f"(the run could not complete: {got['error']})"
        # THE TAIL, AND IT SAYS SO. Tail is right for a test run -- the
        # summary is at the end -- but a model reading a silently cut
        # log cannot tell an empty first half from one it never saw.
        body = got.get("output") or ""
        note = ("" if len(body) <= MAX_OUTPUT else
                f"[showing the last {MAX_OUTPUT} of {len(body)} bytes]\n")
        return f"exit={got.get('exit_code')}\n" + note + body[-MAX_OUTPUT:]


class TarTree:
    """Docker and E0: no claim, so the tree is an archive in hand.

    TODAY'S BEHAVIOUR, MOVED AND NOT CHANGED. Every line here came out
    of `investigate._tool` unaltered, including the per-command
    ephemeral container -- there is no warm pod to keep, so there is
    nothing else for `run` to be.
    """

    def __init__(self, driver, tree_tar: bytes, tree_names, image: str,
                 *, prepend: str = "", env: dict | None = None,
                 timeout_s: float = 120.0):
        self._driver = driver
        self._tar = tree_tar
        self._names = tree_names
        self._image = image
        self._prepend = prepend
        self._env = env or {}
        self._timeout = timeout_s

    def __repr__(self) -> str:                              # pragma: no cover
        return f"<TarTree {len(self._names)} members>"

    async def list_files(self, prefix: str = "") -> str:
        want = "verify/" + str(prefix or "").strip("/")
        got = [n[len("verify/"):] for n in self._names
               if n.startswith(want.rstrip("/"))]
        return "\n".join(got[:MAX_LISTING]) or "(nothing under that prefix)"

    async def read_file(self, path: str) -> str:
        import io                                            # noqa: PLC0415
        import tarfile                                       # noqa: PLC0415

        want = "verify/" + str(path or "").strip("/")
        try:
            with tarfile.open(fileobj=io.BytesIO(self._tar)) as tf:
                blob = tf.extractfile(want).read(MAX_READ)
            return blob.decode("utf-8", "replace")
        except Exception:                                    # noqa: BLE001
            return f"(no such file: {path})"

    async def run(self, command: str) -> str:
        from kernel.spec import ExecutionSpec                # noqa: PLC0415
        from kernel.verifier.kinds import (TREE_PATH,        # noqa: PLC0415
                                           VERIFIER_UID, tree_kwargs)

        if not (command or "").strip():
            return "(empty command)"
        spec = ExecutionSpec(
            image=self._image,
            command=["sh", "-c",
                     (f'export PATH="{self._prepend}:$PATH"; '
                      if self._prepend else "")
                     + f"cd {TREE_PATH} && {command}"],
            env=self._env,
            network_mode="none",
            user=f"{VERIFIER_UID}:{VERIFIER_UID}",
            memory_mb=1024,
        )
        result = await self._driver.run_ephemeral(
            spec, timeout=self._timeout,
            **await tree_kwargs(self._driver, self._tar))
        if result.get("infra_error"):
            return f"(the run could not complete: {result['infra_error']})"
        return (f"exit={result['exit_code']}"
                + (" (timed out)" if result.get("timed_out") else "")
                + "\n" + (result.get("output") or "")[-MAX_OUTPUT:])


async def dispatch(tree, name: str, args: dict) -> str:
    """One tool call against whichever backend this run has.

    The three names are the model's contract and are unchanged from
    `investigate.TOOLS`, so a prompt written for the tar path works
    against the pod without an edit.
    """
    if name == "list_files":
        return await tree.list_files(str(args.get("prefix") or ""))
    if name == "read_file":
        return await tree.read_file(str(args.get("path") or ""))
    if name == "run":
        return await tree.run(str(args.get("command") or ""))
    return f"(unknown tool {name})"


def _prepend_and_env(mounts_env: dict) -> tuple[str, dict]:
    """The PATH prepend and the variables a criterion would have had.

    Same computation `investigate._env` has always done, on this side of
    the seam because it is now the TREE that carries the environment:
    both backends need it and neither agent does.
    """
    prepends, env = [], {}
    for at in sorted(mounts_env or {}):
        env.update(mounts_env[at].get("env") or {})
        prepends += mounts_env[at].get("prepend") or []
    return ":".join(dict.fromkeys(prepends)), env


async def for_run(driver, env_report: dict | None, contract: dict | None):
    """The tree access this verification has, or None.

    READS THE REPORT, which is how `_tree_tar` already travelled --
    `stage.py` puts the archive there on the tar path and `verify_claim`
    puts `_warm_host` there on the volume path, so one lookup answers
    both and neither agent has to know which ran.

    None means neither is available, and both agents already treat that
    as "do not run": the assessor accepts and the investigator stays
    silent. That is unchanged and is the safe direction -- an assessor
    that cannot see the tree must not refuse work over it.
    """
    report = env_report or {}
    prepend, env = _prepend_and_env(report.get("_mounts_env") or {})

    host = report.get("_warm_host")
    if host:
        return PodTree(host, prepend=prepend, env=env)

    tar = report.get("_tree_tar")
    if not tar:
        return None
    import asyncio                                            # noqa: PLC0415

    from kernel.verifier.tarslice import names                # noqa: PLC0415

    listing = sorted(await asyncio.to_thread(names, tar))
    image = (contract or {}).get("base_digest") or         (contract or {}).get("base_image")
    return TarTree(driver, tar, listing, image, prepend=prepend, env=env)


async def changed_for(conn, driver, execution_id: int) -> str:
    """What this story changed, per mount, as `git diff --stat` text.

    WHY THE ASSESSOR NEEDS IT. It is handed a task, a plan and a TREE,
    and told to judge whether the work does what was asked. On this
    repository that tree is 2,447 files and `list_files` shows 400 of
    them in alphabetical order -- 14 of 488 test files, and nothing
    under `tests/` at all. So it spent its steps hunting, and could
    still miss the four files the story touched.

    The work is bounded by the commit, not by the repository. A
    monorepo story that changes three files then costs the same as a
    tiny one, which is the scaling property the tree listing does not
    have.

    CUMULATIVE, FROM THE MERGE-BASE. A story has many rounds and this
    judges the finished work, not the last delta -- round three touching
    one file does not make that file the thing to assess. Merge-base
    rather than the lane head, so a trunk that moved mid-story does not
    arrive as this story's work.

    COMPUTED IN THE EXECUTION POD. `git archive` is how the tree reaches
    the verification pod and an archive carries no `.git`, so the
    verifier has files at one commit and no history. The clone lives in
    the execution pod; `podfs.op_changed` runs there.

    "" ON ANY DOUBT, and the caller falls back to the tree listing --
    which is what every assessment did before this existed. A pod that
    has been reaped, a lane that will not resolve, a podfs that refuses:
    none of them is worth failing an assessment over.
    """
    try:
        import asyncio                                        # noqa: PLC0415

        from kernel.verifier.tree import pod_address           # noqa: PLC0415
        from kernel.workspace.mounts import mounts_for         # noqa: PLC0415
        from kernel.workspace.podfs_client import changed      # noqa: PLC0415

        rows = await conn.fetch(
            "SELECT gr.repo_owner, gr.repo_name, gr.branch_name "
            "FROM goal_resources gr "
            "JOIN executions e ON e.goal_id = gr.goal_id "
            "WHERE e.id = $1 AND gr.kind = 'branch' AND gr.role = 'trunk' "
            "AND gr.branch_name IS NOT NULL", execution_id)
        # `repos/<owner>__<name>` is the mount layout for every story.
        lanes = {f"repos/{r['repo_owner']}__{r['repo_name']}":
                 r["branch_name"] for r in rows}
        if not lanes:
            return ""
        mounts = await mounts_for(conn, execution_id)
        host = await pod_address(conn, execution_id, driver)
        if not host:
            return ""
        workspace = await conn.fetchval(
            "SELECT workspace_path FROM executions WHERE id = $1",
            execution_id)
        got = await asyncio.to_thread(
            changed, host, workspace or "", mounts, lanes,
            execution_id=execution_id)
    except Exception as exc:                                  # noqa: BLE001
        logger.info("execution %s: no change summary for the assessor "
                    "(%s); it will read the tree instead", execution_id, exc)
        return ""

    parts = []
    for at, stat in sorted((got.get("changed") or {}).items()):
        if (stat or "").strip():
            parts.append(f"{at or '<root>'}:\n{stat.strip()}")
    return "\n\n".join(parts)
