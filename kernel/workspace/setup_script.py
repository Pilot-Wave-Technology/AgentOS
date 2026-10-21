"""`env_setup` as an init container's program, discovered at RUN time.

P3, and 12.5 is why it exists. `LEG_MAX_S` is 2700 seconds. A
thirty-minute `env_setup` inside a leg leaves fifteen minutes for the
work, which is not enough -- so the install moves out of the leg
entirely, into an init container that runs once before the sandbox
starts.

WHY THE DISCOVERY IS IN SHELL AND NOT IN PYTHON. `kernel/verifier/
setups.py` walks the tree and returns what to run, and it is the
obvious thing to reuse. It cannot be reused here, for two reasons that
compound:

  * THE TREE DOES NOT EXIST WHEN THE MANIFEST IS BUILT. The conductor
    writes the pod spec; the workspace is cloned by the
    `workspace-compose` init container that runs INSIDE that pod. So
    nothing on the conductor can read `requirements.txt` to decide
    whether to install it.
  * THE CONTRACT'S IMAGE DOES NOT CARRY OUR CODE. `podinit` can import
    `kernel/` because it runs on the BRIDGE image. This container runs
    on `spec.image` -- defence 1, the same rule `prepare_job.py` states
    first: a setup run on the executor's image can leave a PATH shim in
    the environment the work is then done in.

So the decision table travels as generated `sh`, the same way
`prepare_script.script` does for the verifier's prepare Job. The
mounts ARE known when the manifest is built (`spec.workspace_mounts`),
and `setups.REGISTRY` is a filename-to-command table with supersession
-- both of which an `if`/`elif` chain expresses exactly.

AND IT MUST NOT FAIL THE POD. An init container that exits non-zero is
a pod that never starts. Today a broken manifest is reported to the
model, which fixes it and carries on; as a failing init container it
would be a story that can never begin and can never be repaired, and
the person watching would see a pod in `Init:Error` with no verdict and
no feedback. Section 5.8 row 5 is explicit that a genuinely broken
manifest IS about the work and the executor should hear it. So every
command here is allowed to fail: the output goes to `REPORT` on the
workspace, the container exits 0, and the leg reads the file and tells
the model.

WHAT THIS DOES NOT DO. It does not replace the `env_setup` TOOL. A
model that adds `requirements.txt` mid-run still calls it, and
`envsetup.py` still runs it through the job channel -- this only
removes the FIRST install, the one every story with a committed
manifest pays before it has done anything. 5.10's rule that the
verifier rebuilds when the committed manifest CHANGES is untouched.
"""

from __future__ import annotations

import shlex

from kernel.verifier.setups import REGISTRY

# Where the init container leaves its account of what it did. On the
# workspace volume, so the leg -- a different container, later -- can
# read it without a bridge call or a database row.
REPORT = ".agentos-setup-report"

# The marker a reader looks for. One line per mount, so a partial file
# from a container killed mid-write is still parseable up to the cut.
MARKER = "@@agentos-setup@@"


def _ordered() -> list:
    """REGISTRY with the superseded entries last.

    An `elif` chain IS the supersession rule, provided the thing that
    supersedes is tested first: `setups.from_tar` drops a manifest when
    something in its `superseded_by` is also present, and testing the
    superseding one earlier and stopping reaches the same answer without
    a second pass. A lockfile-based install must therefore be tested
    before the manifest it supersedes, or a repo with both gets the
    wrong one.
    """
    rows = list(REGISTRY)
    superseded = set()
    for row in rows:
        over = row.get("superseded_by") or ()
        if isinstance(over, str):
            over = (over,)
        superseded.update(over)
    return ([r for r in rows if r["manifest"] not in superseded]
            + [r for r in rows if r["manifest"] in superseded])


def _for_mount(at: str, workspace: str) -> list[str]:
    """The `if`/`elif` chain for one mount."""
    where = f"{workspace}/{at}" if at else workspace
    quoted = shlex.quote(where)
    label = shlex.quote(at or ".")
    out = [
        f"if [ -d {quoted} ]; then",
        f"  cd {quoted} || exit 0",
    ]
    first = True
    for row in _ordered():
        manifest = shlex.quote(row["manifest"])
        keyword = "if" if first else "elif"
        first = False
        out += [
            f"  {keyword} [ -f {manifest} ]; then",
            f"    echo \"{MARKER} start {at or '.'} {row['manifest']}\"",
            # EVERY COMMAND MAY FAIL. See the module docstring: a
            # non-zero exit here would be a pod that never starts, and
            # a manifest the model could have fixed in one leg becomes a
            # story that cannot begin.
            f"    if {row['command']} > \"$SETUP_LOG\" 2>&1; then",
            f"      echo \"{MARKER} ok {at or '.'} {row['manifest']}\"",
            "    else",
            f"      echo \"{MARKER} failed {at or '.'} {row['manifest']}\"",
            "      sed 's/^/    /' \"$SETUP_LOG\" | tail -n 40",
            "    fi",
        ]
    if not first:
        out += [
            "  else",
            f"    echo \"{MARKER} none {at or '.'}\"",
            "  fi",
        ]
    out += [
        "else",
        f"  echo \"{MARKER} absent {label}\"",
        "fi",
    ]
    return out


def script(mounts, workspace: str) -> str:
    """The whole init container's program, for `sh -c`.

    `mounts` are the mount points relative to `workspace` -- "" for the
    root. Order is the caller's; nothing here sorts, because a repo that
    must be installed before its sibling is a fact the caller knows and
    this function does not.
    """
    report = shlex.quote(f"{workspace}/{REPORT}")
    lines = [
        # NOT `set -e`. `prepare_script` uses it because a prepare Job
        # that half-built an environment must not stamp it as current;
        # here the opposite is wanted -- one mount's broken manifest must
        # not stop the next mount installing, because a story with two
        # repos should not lose both to one typo.
        "umask 022",
        "SETUP_LOG=$(mktemp 2>/dev/null || echo /tmp/agentos-setup.log)",
        f"exec > {report} 2>&1",
        f"echo \"{MARKER} begin\"",
    ]
    for at in mounts:
        lines += _for_mount(at, workspace)
    lines.append(f"echo \"{MARKER} done\"")
    # ALWAYS ZERO. The last line of a shell script is its exit status,
    # and a `sed` that matched nothing would otherwise take the pod down.
    lines.append("exit 0")
    return "\n".join(lines)


def outcome(report: str) -> dict:
    """Read the report back. `{"ran": [...], "failed": [...]}`.

    A report that was never written, or was cut off mid-run, is not an
    error here -- the container is best-effort by design. The leg gets
    what there is, and an empty answer means "nothing was installed",
    which is what a story with no manifests should also see.
    """
    ran, failed = [], []
    for line in (report or "").splitlines():
        line = line.strip()
        if not line.startswith(MARKER):
            continue
        parts = line[len(MARKER):].split()
        if len(parts) >= 3 and parts[0] == "ok":
            ran.append((parts[1], parts[2]))
        elif len(parts) >= 3 and parts[0] == "failed":
            failed.append((parts[1], parts[2]))
    return {"ran": ran, "failed": failed}
