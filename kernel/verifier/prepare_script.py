"""The shell the prepare Job runs. One function, no cluster, asserted.

SIX THINGS IN ORDER, and the order is the correctness argument.

1  RE-ASK THE VOLUME. The scheduler's copy of the stamp says what it
   last saw; the volume says what it HAS, and volume.py is explicit that
   when they disagree the volume is right. A Job launched on a stale
   database row therefore costs a pod start and nothing else.
2  DELETE THE OLD ENV FIRST. 5.9's dangerous direction is a round that
   REMOVES a dependency: installing over yesterday's `.venv` leaves the
   package there and a check PASSES THAT SHOULD FAIL. Installing into a
   wiped directory cannot do that.
3  COPY THE MOUNT'S SLICE OUT OF `volume.TREE`, which the init container
   has already pulled from the execution pod (`prepare_fetch`). This is
   E15, and it is the step that used to be `base64 -d | tar xz` over a
   payload in the Job's own command.
4  RUN `setups.REGISTRY`'s OWN COMMAND, unchanged, with cwd at the
   mount's directory under `volume.ENV`. Not a second spelling of pip
   and npm: the registry is the executor-verifier contract and both
   sides run the same text from the same committed file.
5  LINK `TREE/<at>/node_modules` AT THE CACHED ONE (E16, `env_link`).
6  STAMP LAST. A stamp written before the installs finish would name a
   half-built environment as current, and everything downstream --
   `volume.stale`, E13's wait-for-rebuild -- would believe it. `set -e`
   means a failed install never reaches this line.

WHY THE SLICE IS COPIED AND NOT USED WHERE IT LIES. Step 4's cwd has to
be `ENV/<at>`, because a venv bakes its interpreter path in ABSOLUTELY
and the tree is replaced on every round while the environment is not
(volume.py). Building it in the tree would put a path that is deleted
next round into every shebang the install writes -- and getting that
wrong is fault 12 wearing a new coat. Copying costs a tree-sized copy
once per MANIFEST change, which is the rare path by construction.

WHAT THE COPY GIVES BACK, and it is the regression E15 exists to undo:
`setup.sh` sees its whole slice again. A script that reads a sibling
works; `-r base.txt` works; `pip install -e .` resolves. None of them
could when the Job carried only the manifests themselves, and execution
207 wrote a `setup.sh`.

`cp -R` AND NOT `cp -a`, WHICH COST A POD ONCE ALREADY. `-a` implies
`-p`: preserve mode, ownership AND timestamps, which only root may do.
`k8s_bridge.init_container` records the exact failure -- a complete copy
followed by `cp: preserving times ... Operation not permitted` and a pod
reporting Init:Error. Nothing here needs preserving: `-R` keeps the mode
bits (so an executable `setup.sh` stays executable) and copies symlinks
as symlinks, which is all the install reads.
"""

from __future__ import annotations

import shlex

from kernel.verifier import env_link, volume


def _mount_dir(at: str) -> str:
    return f"{volume.ENV}/{at}" if at else volume.ENV


def _tree_dir(at: str) -> str:
    return f"{volume.TREE}/{at}" if at else volume.TREE


def script(key: str, commands) -> str:
    """`sh -c` for one prepare Job. `commands` is [(at, command), ...].

    NO PAYLOAD ARGUMENT ANY MORE. It used to take `env_payload.payload`'s
    base64 tar.gz, capped at 256KB by etcd's object limit, because the
    Job was assumed to run before the tree existed. Plan 5.15: the
    prepare Job is networked and always was -- zero egress is a property
    of the CRITERIA pod -- so it pulls the tree instead, and both the
    payload and the cap are gone.
    """
    short = key[:12]
    lines = [
        "set -e",
        # STATED RATHER THAN INHERITED. The Job and the verification pod
        # run as the same uid (prepare_job, pod.UID), so ownership is
        # already right; what a stray restrictive umask in a contract's
        # image would change is whether `node_modules/.bin` is still
        # executable, and a criterion failing on a permission bit reads
        # exactly like a criterion failing on the work.
        "umask 022",
        f"if [ \"$(cat {shlex.quote(volume.STAMP)} 2>/dev/null)\" = "
        f"{shlex.quote(key)} ]; then",
        f"  echo 'agentos-prepare: {short} already on the volume'",
        "  exit 0",
        "fi",
        # PLAN 13: say what phase this is at the start, not only on
        # failure. A container that prints nothing until it dies leaves a
        # silence identical to one that never ran.
        f"echo 'agentos-prepare: start {short}'",
        f"rm -rf {shlex.quote(volume.ENV)}",
        f"mkdir -p {shlex.quote(volume.ENV)}",
    ]
    for at, command in commands:
        lines += _mount(at, command)
    lines.append(f"printf %s {shlex.quote(key)} > {shlex.quote(volume.STAMP)}")
    lines.append(f"echo 'agentos-prepare: stop, wrote {short}'")
    return "\n".join(lines)


def _mount(at: str, command: str) -> list:
    """One mount: its slice, its install, and its node link."""
    where, source = _mount_dir(at), _tree_dir(at)
    return [
        f"mkdir -p {shlex.quote(where)}",
        # REFUSED WITH THE PATH, not silently skipped. An absent slice
        # means the init container pulled a different mount list from
        # the one `setups` discovered, and installing nothing would land
        # as a stamped, empty environment -- a check then fails for a
        # missing dependency that was never asked for.
        f"if [ ! -d {shlex.quote(source)} ]; then echo "
        + shlex.quote(f"agentos-prepare: {at or '.'} was not seeded at "
                      f"{source}; the prepare Job pulls the tree before "
                      f"it installs (kernel/verifier/prepare_fetch.py)")
        + " >&2; exit 1; fi",
        # QUOTED, not interpolated bare. `at` is a mount path out of the
        # workspace configuration, and a shell line that pastes it into
        # its own quoting is one edit away from being the injection this
        # Job's network access would make expensive.
        "echo " + shlex.quote(f"agentos-prepare: {at or '.'}"),
        f"cp -R {shlex.quote(source)}/. {shlex.quote(where)}/",
        # `sh -c` AROUND THE REGISTRY'S TEXT, exactly as the tar path's
        # `_script` does it, so a command containing `&&` or `;` runs as
        # the registry wrote it rather than as this file re-reads it.
        f"( cd {shlex.quote(where)} && sh -c {shlex.quote(command)} )",
        # E16. Node walks UP from the source file looking for
        # `node_modules`, so the install's answer has to be reachable
        # from the TREE even though it lives in the cached ENV.
        env_link.shell(at),
    ]


def bin_paths(at: str, bins) -> list:
    """Where this mount's executables ended up, on the volume.

    DERIVED, NOT DISCOVERED. The tar path had to scan the captured
    archive to learn whether `.venv/bin` existed, because the capture
    was the only evidence. Here the layout is declared -- volume.ENV,
    the mount's `at`, the registry's own `bins` -- so there is nothing
    to look up and nothing that can disagree.
    """
    return [f"{_mount_dir(at)}/{b}" for b in bins]
