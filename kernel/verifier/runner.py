"""E2 — the program that runs several criteria inside ONE container.

Measured on execution 203: `verify: 2 criteria against f46ab932cb23 in
75.4s`, while the criterion's own output was `6 passed in 0.03s`. The
work took 0.03s and the Jobs took 75 — pod scheduling, image pull, and
the same tree unpacked twice because each Job fetches the same object
into its own emptyDir.

So several criteria share one container, and the thing that has to
change is HOW A VERDICT LEAVES IT. With one criterion per Job the exit
code IS the verdict; with several there is no single code that can carry
them, so this runner prints ONE JSON LINE PER CHECK and the Job's exit
status means only "the runner ran".

FOUR PROPERTIES THIS TEXT HAS TO KEEP, each of them paid for already:

    a subshell per check    an `exit 1` in somebody's conftest, a
                            segfault, a `set -e` — none of them may take
                            the other checks' results with them, so each
                            check is its own `sh` and its status is
                            captured on the line after it.
    `timeout` per check     the budget is PER CRITERION (budget.py), and
                            one deadline for the whole batch would let
                            the first slow check spend the last one's.
    output to a FILE        never to the runner's own stdout. A check
                            that prints something shaped like a result
                            line must not be able to forge one, and the
                            cheapest guarantee is that its stdout never
                            reaches the pod log directly.
    base64 on the way out   a result line stays ONE line and stays
                            parseable whatever the check emitted — a
                            control character, half a UTF-8 sequence, an
                            ANSI escape, a stray NUL.

THE COMMAND IS COPIED IN VERBATIM, through a quoted heredoc, which
expands nothing. `sh -c "cd X && <command>"` is what a single criterion
gets today and the text between the heredoc markers is that same string
— splitting or re-quoting it would change what an approved criterion
means, which is the argument kinds.run_command already makes.

WHAT BATCHING COSTS, said out loud rather than discovered later.
Criteria sharing a container are no longer isolated FROM EACH OTHER: one
can leave a file behind, exhaust the memory limit, or kill the runner.
Per-criterion pods bought that isolation and E2 spends it. The four
properties in verify.py's docstring — a fresh container at the
contract's digest, a different uid, the committed tree, the command from
the contract — are per-CONTAINER facts and survive unchanged; this is
the one that does not, and it is why a contract with a declared service
keeps a Job per criterion (a sidecar's state is worth more than the 30s).

A BATCH OF ONE NEVER COMES HERE. kernel/verifier/batch.py sends a
singleton group down the ordinary `run_command` path, so the common case
— 207, 209 and 210 each had one criterion — pays nothing for any of
this, not even the preflight below.
"""

from __future__ import annotations

import base64 as _base64
import json
import os
import shlex

from kernel.verifier.kinds import TREE_PATH

# What a result line starts with. Deliberately not a word that appears
# in test output: the parser scans for it, and a check's own stdout is
# redirected to a file precisely so it can never print one.
MARKER = "@@agentos-check@@"

# The tools the runner cannot do without. NAMED AND CHECKED FIRST rather
# than discovered halfway through: an image with no `base64` would
# otherwise run every check, print nothing parseable, and hand back
# `error` for all of them — non-refusing (R1), but a wasted Job per
# quorum round. Missing one of these is reported on the FIRST line and
# batch.py falls the whole contract back to the per-criterion path.
REQUIRED = ("base64", "date", "tail", "timeout", "tr")

# How much of a check's output travels. `from_exit` keeps the last 4000
# characters and `quorum.observations` keeps 800, so this is the same end
# of the same stream with room to spare — and it bounds a BATCHED pod's
# log, which now carries every check's output where it used to carry one.
OUTPUT_TAIL_BYTES = 65536

# What `timeout` reports when it killed the check. Coreutils uses 124;
# busybox propagates 128+SIGTERM. Neither is authoritative on its own —
# see `timed_out` in batch.py, which also requires the elapsed time to
# have reached the budget, because outcome.py is explicit that a check
# genuinely exiting 124 has FAILED and must not be excused.
TIMEOUT_EXITS = (124, 137, 143)

_PREFLIGHT = r"""set +e
for _t in __TOOLS__; do
  command -v "$_t" >/dev/null 2>&1 || {
    printf '%s{"missing":"%s"}\n' '__MARKER__' "$_t"
    exit 3
  }
done
_d="${TMPDIR:-/tmp}/agentos-batch.$$"
mkdir -p "$_d" || {
  printf '%s{"missing":"tmpdir"}\n' '__MARKER__'
  exit 3
}"""

_RUN = r"""_s=$(date +%s)
timeout __TIMEOUT__ sh "$_d/__SLOT__.sh" >"$_d/__SLOT__.out" 2>&1
_r=$?
printf '%s{"i":__SLOT__,"exit":"%s","elapsed":"%s","out":"' '__MARKER__' "$_r" "$(( $(date +%s) - $_s ))"
tail -c __TAIL__ "$_d/__SLOT__.out" | base64 | tr -d '\n'
printf '"}\n'"""

# The pod is thrown away either way; removing the scratch stops a check
# reading the PREVIOUS round's output when the quorum runs this twice in
# one container, which is a way for a re-run to agree with itself for a
# reason nobody can see.
_CLEANUP = 'rm -rf "$_d" 2>/dev/null\nexit 0'


class Check:
    """One criterion, reduced to what the shell needs in order to run it."""

    __slots__ = ("command", "timeout_s", "env", "path_prepend")

    def __init__(self, command: str, timeout_s: int,
                 env: dict | None = None, path_prepend: str = ""):
        self.command = command
        self.timeout_s = max(1, int(timeout_s))
        self.env = dict(env or {})
        self.path_prepend = path_prepend or ""


def _fill(template: str, **parts: str) -> str:
    """Token substitution, not `format`.

    The templates are shell, and shell is mostly braces. `str.format`
    over this text would mean doubling every one of them, and a missed
    pair is a runner that fails to parse in the cluster rather than
    here.
    """
    for name, value in parts.items():
        template = template.replace(f"__{name}__", value)
    return template


def _body(check: Check, cwd: str) -> str:
    """The check's own script: its environment, then the contract's line.

    PER-CHECK ENVIRONMENT BECOMES `export`, where a single-criterion Job
    puts it on the container. Same variables and the same precedence —
    and the only way to give two checks in one container different
    values for the same name, which `environment_for` exists to produce.
    """
    lines = []
    if check.path_prepend:
        lines.append(f'export PATH="{check.path_prepend}:$PATH"')
    for name in sorted(check.env):
        lines.append(f"export {name}={shlex.quote(str(check.env[name]))}")
    # ONE LINE, `cd ... && <command>`, character for character what
    # kinds.run_command hands `sh -c`. A `cd` that fails must not leave
    # the command running against whatever directory the pod started in.
    lines.append(f"cd {shlex.quote(cwd)} && {check.command}")
    return "\n".join(lines)


def script(checks: list[Check], *, cwd: str = TREE_PATH,
           tag: str | None = None) -> str:
    """The whole runner, as one `sh` program.

    `tag` seeds the heredoc delimiters and defaults to random bytes, so
    a check's own text cannot close its own heredoc and continue as
    runner code. Injectable because the tests need a stable string.
    """
    tag = tag or os.urandom(8).hex()
    out = [_fill(_PREFLIGHT, TOOLS=" ".join(REQUIRED), MARKER=MARKER)]
    # EVERY SCRIPT WRITTEN BEFORE THE FIRST ONE RUNS. Interleaving would
    # let check 0 sit between the heredoc for check 1 and its execution,
    # which is a longer window than it needs to be for no gain.
    for slot, check in enumerate(checks):
        delimiter = f"AGENTOS_CHECK_{slot}_{tag}"
        out.append(f"cat > \"$_d/{slot}.sh\" <<'{delimiter}'\n"
                   f"{_body(check, cwd)}\n{delimiter}")
    for slot, check in enumerate(checks):
        out.append(_fill(_RUN, SLOT=str(slot), MARKER=MARKER,
                         TIMEOUT=str(check.timeout_s),
                         TAIL=str(OUTPUT_TAIL_BYTES)))
    out.append(_CLEANUP)
    return "\n".join(out) + "\n"


def parse(output: str) -> tuple[dict[int, dict], str]:
    """Results by slot, and the tool the preflight found missing.

    TOLERANT BY DESIGN. Anything that is not a marker line is skipped
    without comment: the runner's own stderr, `timeout`'s complaints,
    and — if a check's bytes ever do reach the pod log — its garbage. A
    line that carries the marker but will not parse is skipped too,
    which leaves that slot missing, which batch.py reports as `error`.
    The one thing this must never do is turn noise into a verdict.
    """
    results: dict[int, dict] = {}
    missing = ""
    for line in (output or "").splitlines():
        at = line.find(MARKER)
        if at < 0:
            continue
        try:
            payload = json.loads(line[at + len(MARKER):])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("missing"):
            missing = str(payload["missing"])
            continue
        if "i" not in payload:
            continue
        slot = _int_or_none(payload["i"])
        if slot is None:
            continue
        results[slot] = {"exit": _int_or_none(payload.get("exit")),
                         "elapsed": _int_or_none(payload.get("elapsed")) or 0,
                         "output": _decode(payload.get("out"))}
    return results, missing


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _decode(encoded) -> str:
    """The check's output, back from base64.

    `replace` rather than strict: the bytes are whatever the check
    wrote, and half a UTF-8 sequence at the 64KB cut is the ordinary
    case rather than a fault. Undecodable base64 becomes an empty
    string — the exit code is still a real verdict, and inventing output
    would be worse than admitting there is none.
    """
    if not isinstance(encoded, str) or not encoded:
        return ""
    try:
        return _base64.b64decode(encoded).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""
