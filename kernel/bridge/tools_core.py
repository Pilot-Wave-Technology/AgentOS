"""Sandbox-side core tools: read, write, bash, and the env allowlist.

Split out of shim.py so the shim is the HTTP server and the dispatch table
and nothing else. Runs inside the sandbox; stdlib only.
"""

import os
import subprocess

import errors
import limits
from paths import contain

# Defined HERE and imported by shim.py, not the other way round: these
# functions use them, and a module that reads a name its importer happens
# to define is a NameError waiting for the first call.
BRIDGE_PORT = int(os.environ["BRIDGE_PORT"]) if "BRIDGE_PORT" in os.environ else 9100
WORKSPACE = (
    os.environ["WORKSPACE"]
    if "WORKSPACE" in os.environ and os.path.isdir(os.environ["WORKSPACE"])
    else "/tmp"
)
# The bridge carries the FULL output to the worker, which decides what the
# model sees and what gets stored (E7). Truncating here destroyed the data
# at source: a 50 MB log was cut to 32 KB inside the sandbox and the rest
# was gone, not merely hidden. The cap that remains is a refusal limit, not
# a display limit — the worker bounds for display.
MAX_OUTPUT = 64 * 1024 * 1024
#: How much of ONE file a read may pull in before it is windowed.
#: `readlines()` had no bound, and a file with no newline is a
#: single line -- which is how a sandbox died reading /proc.
MAX_READ_BYTES = 8 * 1024 * 1024


# Ec4d/F6 — THE VARIABLES A CLOUD SDK ACTUALLY READS, and the second
# place they have to be declared.
#
# `build_sandbox_spec` sets these on the sandbox CONTAINER, which is
# necessary and was not sufficient: every shell the executor runs goes
# through `_safe_env`, an ALLOWLIST, and an allowlist that has not heard
# of GOOGLE_APPLICATION_CREDENTIALS drops it before `gcloud` starts. The
# run then holds a valid lease, correct IAM, a correct container
# environment — and reports no credentials, which sends everybody to
# look at the IAM policy.
#
# SAFE BECAUSE THEY ARE PATHS. `issue.sdk_pointers` refuses to emit any
# of these as a value (see its guard), so what passes here is a filename
# or a profile name — printable, revocable, and with a lease id behind
# it. That is the same argument that puts AGENTOS_CREDENTIALS on this
# list, and it is the only argument that would justify either.
#
# Restated rather than imported: this module runs INSIDE the sandbox,
# where `kernel.credentials` is not shipped. `test_credential_issue.py`
# asserts this set covers every provider's `sandbox_pointers`, so the
# two cannot drift without a test going red.
SDK_POINTER_VARS = (
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",  # gcloud: the raw-token file
    "CLOUDSDK_CORE_PROJECT",            # gcloud: which project, not a secret
    "GOOGLE_APPLICATION_CREDENTIALS",   # google-auth, when a real ADC exists
    "AWS_SHARED_CREDENTIALS_FILE", "AWS_PROFILE",   # boto, aws cli
    "AZURE_ACCESS_TOKEN_FILE",          # az
)


def _safe_env() -> dict:
    """Allowlisted environment — defence in depth, shared by every shell path.

    The sandbox should carry no credential; if one leaked in, the tool must
    not forward it. Background jobs use the same allowlist, or they would
    be the hole in it.
    """
    # Ec4b — `AGENTOS_CREDENTIALS` is a PATH, and that is the whole point
    # of it being here. A credential in the environment is in every child
    # process's `environ`, cannot be revoked mid-run, and has no lease id
    # to point at afterwards. A pointer has none of those problems and is
    # safe to print, so the allowlist stays an allowlist rather than
    # becoming the leak it exists to prevent.
    # AND THE THINGS A SHELL CANNOT WORK WITHOUT, which this allowlist
    # silently ate. Both were set on the sandbox container and both were
    # dropped here, so the fix looked applied and changed nothing:
    #
    #   HTTP_PROXY etc  -- the namespace is default-deny egress and the
    #     only route out is the squid proxy. Without these, `pip install`
    #     does not fail, it HANGS: measured at 20 minutes on NCAL5 run
    #     193, holding the bridge's tool lock until every later call
    #     timed out and the executor reported the bridge as unresponsive.
    #
    #   GIT_CONFIG_*    -- safe.directory. The init container clones as
    #     the worker's uid and the sandbox runs as 65532, so without it
    #     every `git` command in the checkout dies of "dubious
    #     ownership".
    #
    # NEITHER IS A CREDENTIAL. A proxy address and a git config key are
    # facts about where this container sits, in the same class as
    # `WORKSPACE` and `PATH` -- which is the test this allowlist applies.
    # Verified the wrong way first: `kubectl exec` inherits the
    # container's env directly and never passes through here, so `git
    # status` worked by hand and would have failed for the model.
    env = {
        k: v for k, v in os.environ.items()
        if k in ("PATH", "HOME", "USER", "SHELL", "TERM", "LANG",
                 "WORKSPACE", "BRIDGE_PORT", "HOSTNAME",
                 "AGENTOS_CREDENTIALS",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "no_proxy",
                 "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                 "GIT_CONFIG_VALUE_0") or k in SDK_POINTER_VARS
    }
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def _tool_read(args: dict) -> dict:
    """Read a file, or a window of one.

    offset/limit were implemented from the start and were NEVER IN THE
    SCHEMA, so no model could reach them — every read was a whole file.
    G3's baseline measured 84,603 tokens to first write against a tool
    whose cheap path was unreachable. A capability the caller cannot see
    is a capability that does not exist.
    """
    path = args.get("path", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", 2000)

    if not path:
        return errors.fail("E_BAD_ARGUMENT", "path is required")
    try:
        offset = max(0, int(offset))
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        return errors.fail("E_BAD_ARGUMENT",
                           "offset and limit must be whole numbers")

    target, bad = contain(WORKSPACE, path)
    if bad:
        return bad
    # BOUNDED, and `readlines()` was not. A file with no newline is one
    # line, and a synthetic one under /proc has no end -- which took a
    # 9GiB sandbox down through `grep`. Containment above makes that
    # unreachable; this makes a big file inside the workspace survivable
    # too, since the window is applied after the read either way.
    try:
        with open(target) as f:
            blob = f.read(MAX_READ_BYTES)
            # One more byte decides whether the cap actually bit. Without
            # it a file of exactly the cap looks truncated and a
            # truncated one looks whole, and the footer below would lie
            # either way.
            clipped = bool(f.read(1))
    except OSError as e:
        return errors.from_oserror(e, path)
    lines = blob.splitlines(keepends=True)

    selected = lines[offset:offset + limit]
    output = "".join(selected)
    truncated = len(output) > MAX_OUTPUT
    if truncated:
        output = output[:MAX_OUTPUT] + "\n... [truncated]"

    # SAY WHAT WAS NOT RETURNED. A window that stops silently at the
    # bound reads as the end of the file, and a model that believes it
    # has seen the whole thing edits from a partial picture. The counts
    # are also what makes G4's "reads are windowed" rule countable.
    total, shown = len(lines), len(selected)
    # TWO NOTES, NOT ONE. The window note used to ASSIGN over the
    # clipped note, so a read that was both clipped and windowed
    # said only "N more below" -- which reads as "the rest is below"
    # when the rest was never loaded. Different facts; a reader
    # needs both.
    notes = []
    if clipped:
        notes.append(
            f"only the first {MAX_READ_BYTES // (1024 * 1024)}MB of this "
            f"file was read; it is longer on disk")
    if offset or shown < total:
        last = offset + shown
        parts = [f"lines {offset + 1}-{last} of {total}"]
        if last < total:
            parts.append(f"{total - last} more below")
        if offset:
            parts.append(f"{offset} above")
        notes.append("; ".join(parts)
                     + ". Re-read with a different offset for the rest")
    footer = (chr(10) + "[" + ". ".join(notes) + "]") if notes else ""
    return errors.ok(output + footer, lines_total=total,
                     lines_shown=shown, offset=offset, truncated=truncated)


def _tool_write(args: dict) -> dict:
    """Write content to a file."""
    path = args.get("path", "")
    content = args.get("content", "")

    if not path:
        return errors.fail("E_BAD_ARGUMENT", "path is required")

    target, bad = contain(WORKSPACE, path)
    if bad:
        return bad
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(content)
        return errors.ok(f"wrote {len(content)} bytes to {path}", bytes_written=len(content))
    except PermissionError:
        return errors.fail("E_PERMISSION", f"permission denied: {path}")


def _tool_bash(args: dict) -> dict:
    """Run a bash command. Sandbox-side only — rule 9."""
    command = args.get("command", "")

    if not command:
        return errors.fail("E_BAD_ARGUMENT", "command is required")

    # WAS `min(args.get("timeout", 30), 120)`, which silently clamped. A
    # model asking for 600 got 120 and then `E_TIMEOUT: no output within
    # 120s` — which reads as a slow command rather than an out-of-range
    # request, so the answer it needed (bash_background) was never
    # suggested. `min` also raised TypeError on a string or a null.
    try:
        timeout = limits.bash_timeout(args)
    except limits.BadTimeout as bad:
        return errors.fail("E_BAD_ARGUMENT", str(bad))

    safe_env = _safe_env()

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE,
            env=safe_env,
        )
        output = result.stdout
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n... [truncated]"
        error = result.stderr if result.returncode != 0 else None
        return {"output": output, "error": error, "error_code": None, "retryable": False, "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return errors.fail("E_TIMEOUT", f"no output within {timeout}s", exit_code=124)
