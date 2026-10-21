"""The sandbox shares the worker's network namespace, and reached nine ops.

WHAT WAS TRUE BEFORE, AND WAS VERIFIED LIVE ON exec-193.

`podfs` binds `0.0.0.0:9101` in the WORKER container and authenticated
nothing at all. The sandbox -- the container the model drives -- is not
a separate pod: it is a container in the SAME pod, sharing the network
namespace, so the `execution-bridge-ingress` NetworkPolicy that decides
which POD may open 9101 never applies to it. From the model's own bash:

    curl 127.0.0.1:9101 -d '{"op":"explore", ...}'   -> the workspace tree

and eight more operations answered the same way. They are not readers.
`commit` writes and pushes with the story's git identity. `credentials`
places a cloud lease on disk. `revoke_credentials` removes one. `plan`
writes into every writable git mount. A model that wandered into a port
scan -- or was told to by something it read in a repository -- could
commit as the story, or drop the story's own credential.

THE SURFACE GREW WHILE NOBODY LOOKED AT IT. It began as `read`, for the
file explorer. Six more operations were added one at a time, each for a
good reason, and none of them at the moment anybody asked again what
could call it. So the test that matters most below is not any single
refusal: it is the one that walks `podfs.OPS` and demands a 401 from
every name in it, because the defect's shape is "the tenth op arrives
and nobody re-asks the question".

WHY THESE TESTS SPAWN A REAL SERVER. The defect was never in `handle()`,
which has never seen a header; it was in what answers on a socket. A
test that called `podfs.handle({"op": "commit"})` would have passed
happily throughout the vulnerable period and proved nothing. So every
case here is a real child process, a real TCP port and a real HTTP
request -- the shape the bridge's liveness test uses, and for the same
reason (app/services/tests/test_the_bridge_answers_while_it_works.py).

NOTHING HERE IS WINDOWS-HOSTILE, deliberately, and that is why there is
no `os.name == "nt"` skip. The bridge's test carries one because it runs
`sleep 5` through the sandbox's bash; the operations exercised here are
`read` and `credentials`, which are `open()` and `os.makedirs`, so this
file runs everywhere and the refusal is proven on the machine at hand
rather than on somebody else's CI.

THE PORT IS REBOUND IN THE CHILD, not added as a flag. `WORKER_PORT` is
a constant deliberately paired with the NetworkPolicy in
`kernel/drivers/k8s_workspace.py`, and giving the server a `--port` so a
test could be tidier is how a pairing like that stops being one fact.
The child sets the module attribute and then calls the shipped `main()`,
so the startup refusal, the gate and the one exemption under test are
all the real code path.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from kernel.workspace import podfs, podfs_auth

# A REAL child process on a fixed TCP port -- two xdist workers
# would race on the bind. Serial runs, including every CI run,
# are unaffected.
pytestmark = pytest.mark.no_parallel

# Beside the bridge test's 9397 and away from the real 9101, so a
# developer running this while a pod-shaped stack is up collides with
# neither.
_PORT = 9398
_STARTUP_PORT = 9399

# Any string works: the worker never derives, it compares. What the
# CONDUCTOR would present here is `podfs_auth.derive(execution_id)`, and
# that half is exercised by the client's own callers.
TOKEN = "the-token-this-execution-was-given"
WRONG = "the-token-some-other-execution-was-given"

_BOOT = ("import sys\n"
         "from kernel.workspace import podfs\n"
         "podfs.WORKER_PORT = int(sys.argv[1])\n"
         "raise SystemExit(podfs.main(['--bind', '127.0.0.1']))\n")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _spawn(workspace, token, port=_PORT):
    """The real server, in a real process. `token=None` configures none."""
    env = dict(os.environ)
    env["AGENTOS_WORKSPACE_ROOT"] = str(workspace)
    if token is None:
        env.pop(podfs_auth.ENV_VAR, None)
    else:
        env[podfs_auth.ENV_VAR] = token
    return subprocess.Popen(
        [sys.executable, "-c", _BOOT, str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)


def _health(port=_PORT, timeout=2):
    return urllib.request.urlopen(
        f"http://127.0.0.1:{port}/health", timeout=timeout)


def _wait_for_health(proc, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        if proc.poll() is not None:
            raise AssertionError(f"podfs exited: {proc.stdout.read()[:600]}")
        try:
            with _health() as resp:
                if resp.status == 200:
                    return
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(0.2)
    raise AssertionError("podfs never became healthy")


def _post(payload: dict, header: str | None = None):
    """(status, raw body). Raw, so two refusals can be compared as bytes."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{_PORT}/", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    if header is not None:
        request.add_header(podfs_auth.HEADER, header)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture()
def workspace(tmp_path):
    """A served workspace with one file in it. Yields the root."""
    root = tmp_path / "workspace"
    root.mkdir()
    # BYTES, not `write_text`: text mode turns "\n" into "\r\n" on
    # Windows and the read-back assertion would fail for a reason that
    # has nothing to do with authorisation.
    (root / "report.md").write_bytes(b"the story's own words\n")
    proc = _spawn(root, TOKEN)
    try:
        _wait_for_health(proc)
        yield root
    finally:
        proc.terminate()
        proc.wait(timeout=15)


# ── the gate ─────────────────────────────────────────────────────────

def test_a_call_with_no_token_does_not_get_the_workspace(workspace):
    """The exact call that was verified working from the sandbox."""
    status, body = _post({"op": "explore", "what": "tree",
                          "root": str(workspace)})
    assert status == 401
    assert b"report.md" not in body, (
        "the tree came back, which is the finding from exec-193")


def test_a_call_with_another_executions_token_is_refused(workspace):
    status, body = _post({"op": "read", "root": str(workspace),
                          "path": "report.md"}, header=WRONG)
    assert status == 401
    assert b"bytes" not in body


def test_the_conductors_own_token_is_accepted(workspace):
    """Without this the rest could pass on a server that refuses all."""
    status, body = _post({"op": "read", "root": str(workspace),
                          "path": "report.md"}, header=TOKEN)
    assert status == 200, body
    assert base64.b64decode(json.loads(body)["bytes"]) == \
        b"the story's own words\n"


def test_every_operation_is_refused_and_not_only_the_readers(workspace):
    """THE DEFECT'S SHAPE. Six write-capable ops were added to a surface
    that began as `read`, one at a time. A gate that has to be
    remembered per op is a gate the seventh addition walks past, so the
    check lives above the dispatch table and this walks the table."""
    refused = {op: _post({"op": op})[0] for op in podfs.OPS}
    assert set(refused.values()) == {401}, refused
    for writer in ("commit", "plan", "credentials", "revoke_credentials"):
        assert writer in refused, (
            f"{writer} left the dispatch table; this test is now "
            f"checking less than it claims to")


def test_an_unknown_op_is_refused_before_it_is_looked_up(workspace):
    """401 and not the usual 200-with-E_UNKNOWN_OP: an unauthorised
    caller must not be able to enumerate the surface."""
    status, body = _post({"op": "no-such-op"})
    assert status == 401
    assert b"E_UNKNOWN_OP" not in body


def test_a_write_is_refused_before_it_reaches_the_disk(workspace):
    """A 401 after the lease was already written is not a fix.

    `credentials` is the write worth proving with: it is pure filesystem
    -- no git, no mounts -- so what the assertion sees is authorisation
    and not a missing prerequisite. The authorised half of the test is
    what makes that true; without it, "no file appeared" would also be
    satisfied by a call that could never have worked.
    """
    from kernel.workspace.credentials import lease_path

    lease = "exec-193-cloud"
    args = {"op": "credentials", "root": str(workspace),
            "lease_id": lease, "payload": {"access_token": "s3cret"}}
    target = lease_path(str(workspace), lease)

    status, _ = _post(args)
    assert status == 401
    assert not os.path.exists(target), (
        "the credential was placed and THEN refused, so the refusal is "
        "an audit note rather than a control")

    status, body = _post(args, header=TOKEN)
    assert status == 200, body
    assert os.path.exists(target)


def test_the_refusal_does_not_say_which_way_it_failed(workspace):
    """"No token is configured" and "your token is wrong" are different
    facts, and an unauthorised caller is entitled to neither: the first
    of them says that any header at all would have worked."""
    args = {"op": "read", "root": str(workspace), "path": "report.md"}
    _, absent = _post(args)
    _, wrong = _post(args, header=WRONG)
    assert absent == wrong
    for leak in (b"configured", b"unset", b"empty", b"signing",
                 podfs_auth.ENV_VAR.encode()):
        assert leak not in absent, absent


# ── the one exemption, and the startup refusal ───────────────────────

def test_health_answers_without_a_token(workspace):
    """DELIBERATE, and bounded. A kubelet probe is configured in a pod
    manifest and has no way to present a per-execution token, so a
    guarded liveness path would one day restart healthy pods. It
    discloses one bit -- "a server is listening" -- that anything in
    this network namespace already has from `ss -ltn`, and it cannot
    grow an operation: every op is a POST and GET has no dispatch."""
    with _health() as resp:
        assert resp.status == 200
    # And the exemption is the PATH, not the verb.
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/", timeout=5)
    assert caught.value.code == 404


def test_the_server_refuses_to_start_with_no_token_configured(tmp_path):
    """FAIL CLOSED.

    Treating "no token configured" as "no checking today" reproduces
    exactly the bug being fixed -- silently, in whichever environment
    forgot to set the variable, with the surface wide open and nothing
    anywhere saying so. A pod that will not start is a CrashLoopBackOff
    somebody reads within the hour.
    """
    proc = _spawn(tmp_path, None, port=_STARTUP_PORT)
    try:
        out = proc.communicate(timeout=60)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError(
            "podfs came up with no token configured, which is the open "
            "surface this change exists to close")
    assert proc.returncode != 0, out
    assert podfs_auth.ENV_VAR in out, out
    with pytest.raises(urllib.error.URLError):
        _health(port=_STARTUP_PORT)
