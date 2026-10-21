"""Bridge shim — sandbox-side HTTP listener.

Stdlib only, no deps. Runs as the container command inside the sandbox.
Bind-mounted read-only at create time so python:3.12-slim works as-is —
no custom image needed. build_spec's hardcoded image stays viable and
image selection stays deferrable to H1b.

Listens on 0.0.0.0:9100. The sandbox shares the worker's netns via
--network=container:<worker>, so localhost:9100 is reachable from the worker.

Protocol: POST /tool with JSON body {"tool": str, "args": dict, "id": str}.
Returns JSON {"output": str, "error": str|null, "exit_code": int}.
GET /health returns 200.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# The whole bridge/ directory is mounted at /app/bridge, so siblings are
# importable. Previously only shim.py was mounted and it had to be one
# self-contained file; twelve tools do not fit in one file worth reading.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import errors
import harness_jobs
import limits
import tools_core
import tools_fs
import tools_jobs
import treefp

# Allow port reuse — prevents "Address already in use" on restart
ThreadingHTTPServer.allow_reuse_address = True

# ONE TOOL AT A TIME, BUT ALWAYS ANSWER /health.
#
# THE SERVER WAS `HTTPServer`: single-threaded, one request served to
# completion before the next is read. `env_setup` runs the tree's
# install -- `pip install -r requirements.txt`, `npm ci` -- and used to
# do it in ONE BLOCKING CALL with a ten-minute ceiling, and for that
# whole time the bridge answered nothing at all. (E14 moved that caller
# to `job_start`/`job_poll`, so the install no longer holds anything;
# the threading below is still what keeps /health answerable, and the
# model's own foreground `bash` can still hold the lock for 120s.) `worker/loop.py` probes `bridge_health(timeout=3)`,
# and with two conductor replicas the worker NOT running the leg is the
# one that probes: three seconds into the install it concluded the
# bridge was dead and destroyed the pod underneath a healthy run.
#
#   execution 192: bridge dead; destroying the pod so dispatch
#   rebuilds it (the claim, and the tree on it, survive)
#
# From the outside that is "the workspace disappears as soon as it
# reaches env_setup", over and over -- the file explorer reads the live
# pod, and there was no live pod.
#
# AND IT IS WHY AN ERRAND WORKED. Errand 187 wrote a report at the
# workspace root and never called `env_setup`: nothing ever held the
# bridge for more than a moment, every probe answered, the pod lived.
# A coding story is the first thing that blocks it for minutes.
#
# Threaded so /health is answerable during a long call, and a lock so
# TOOLS stay serialised -- concurrency between two bash calls on one
# workspace is not something this design has ever offered, and handing
# it over by accident while fixing a liveness probe would be a worse
# bug than the one being fixed.
_TOOL_LOCK = threading.Lock()

# WHAT IS HOLDING IT, so the refusal can say. A bare "busy" sends the
# reader to look at the network; "env_setup has been running pip install
# for 214s" sends them to the requirements file.
_HOLDER: dict = {"tool": "", "since": 0.0}

# How long a caller waits for the lock before being told the sandbox is
# busy. Short: the point is to answer, not to queue. Anything longer and
# the caller's own timeout wins and we are back to a silent hang.
_BUSY_AFTER_S = 2.0


def _busy_message() -> str:
    """Name the holder and its age. Never raises -- this is an error path."""
    tool = _HOLDER.get("tool") or "another tool"
    since = _HOLDER.get("since") or 0.0
    age = (time.time() - since) if since else 0.0
    return (f"E_SANDBOX_BUSY: {tool} has held this sandbox for "
            f"{age:.0f}s and tools run one at a time here. This is NOT a "
            f"dead bridge -- /health is answering. Wait for it, or use "
            f"bash_background for work that should not block the turn.")

# S10: no environ.get with defaults. These are set by the container env
# or have fallbacks computed without os.environ.get().
# Single source in tools_core, re-exported here for the server code.
BRIDGE_PORT = tools_core.BRIDGE_PORT
WORKSPACE = tools_core.WORKSPACE
MAX_OUTPUT = tools_core.MAX_OUTPUT


class BridgeHandler(BaseHTTPRequestHandler):
    """Handle tool execution requests from the worker."""

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/tool":
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        tool = body.get("tool", "")
        args = body.get("args", {})

        try:
            # THE HARNESS'S JOB OPS NEVER TAKE THE LOCK, and that is the
            # point of them. `job_start` spawns and returns, `job_poll`
            # reads a buffer, `job_cancel` signals -- none of them runs
            # the work, so none of them is the thing the lock protects
            # against. Putting a poll behind the lock would mean a
            # 30-minute install could not be watched while the model
            # holds it for a 120s `bash`, which reintroduces the wait
            # this change exists to remove.
            #
            # The JOB still runs concurrently with the model's tools,
            # exactly as `bash_background`'s jobs already do. That is a
            # deliberate widening: the alternative is the old one, where
            # every model tool call was refused for the length of an
            # install.
            if tool in limits.JOB_TOOLS:
                self._respond(200, execute_job_tool(tool, args))
                return

            # SERIALISED, while /health above stays lock-free. The whole
            # point of threading this server is that liveness is
            # answerable during a long install; it is not to run two
            # tools against one workspace at once.
            #
            # BUT A CALLER THAT CANNOT HAVE THE LOCK IS TOLD SO, rather
            # than left to hang until its own HTTP timeout. This is the
            # defect behind "the tool bridge is unresponsive with
            # TimeoutError on all operations", which is how a model
            # reported it, and behind `env_install` appearing broken when
            # it was simply the next caller to arrive.
            #
            # `env_setup` ran the tree's own `pip install -r
            # requirements.txt` in one blocking call -- up to TEN
            # MINUTES -- and held this lock for all of it. Every tool
            # call in that window timed out. Measured on exec-199: health
            # answered in 0.04s while `echo hi` timed out at 25s, which
            # is the worst possible pair of facts, because it says the
            # sandbox is alive and unusable and names neither.
            #
            # A REFUSAL IS BETTER THAN A TIMEOUT even though both fail the
            # call: it arrives at once instead of after the ceiling, it
            # says WHAT is running and for how long, and it is
            # distinguishable from a dead bridge -- which is the
            # distinction the worker's reachability check gets wrong when
            # it destroys a pod that was only busy.
            if not _TOOL_LOCK.acquire(timeout=_BUSY_AFTER_S):
                self._respond(409, {
                    "output": "",
                    "error": _busy_message(),
                    "exit_code": 1,
                })
                return
            _HOLDER["tool"], _HOLDER["since"] = tool, time.time()
            try:
                result = execute_tool(tool, args)
            finally:
                _HOLDER["tool"], _HOLDER["since"] = "", 0.0
                _TOOL_LOCK.release()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {
                "output": "",
                "error": f"{type(e).__name__}: {e}",
                "exit_code": 1,
            })

    def _respond(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        # Suppress per-request logging in production; keep for DEBUG
        pass


def execute_job_tool(tool: str, args: dict) -> dict:
    """The harness's three long-tool ops. Never runs the work itself.

    HARNESS-ONLY, and nothing here enforces that because nothing here
    could: the shim implements, chain stage 2 on the worker authorises.
    What keeps a model out is that these names are not in
    `kernel/tools/catalog/` at all, so no profile can grant them and
    `executor.py` routes only what the catalog defines.
    """
    if tool == limits.JOB_START:
        return harness_jobs.tool_start(args, WORKSPACE, tools_core._safe_env())
    if tool == limits.JOB_POLL:
        return harness_jobs.tool_poll(args)
    return harness_jobs.tool_cancel(args)


def execute_tool(tool: str, args: dict) -> dict:
    """Execute a tool inside the sandbox. Returns {output, error, exit_code}.

    Sandbox-side surface. Which of these a given execution may actually
    call is decided by the profile and enforced by chain stage 2 on the
    worker — the shim implements, it does not authorise.

      read glob grep list_dir  orientation
      write edit              editing; edit refuses a non-unique match
      bash                    foreground, hard 120s cap
      bash_background/_output/_kill
                              long work that would otherwise outlive the leg
      tree_fingerprint        worker-only; what the tree held when a job ran

    The harness's `job_start`/`job_poll`/`job_cancel` are answered by
    `execute_job_tool` before the lock, not here.
    """
    if tool == "read":
        return tools_core._tool_read(args)
    elif tool == "write":
        return tools_core._tool_write(args)
    elif tool == "bash":
        return tools_core._tool_bash(args)
    elif tool == "glob":
        return tools_fs.tool_glob(args, WORKSPACE)
    elif tool == "grep":
        return tools_fs.tool_grep(args, WORKSPACE)
    elif tool == "list_dir":
        return tools_fs.tool_list_dir(args, WORKSPACE)
    elif tool == "edit":
        return tools_fs.tool_edit(args, WORKSPACE)
    elif tool == "bash_background":
        return tools_jobs.tool_bash_background(args, WORKSPACE, tools_core._safe_env())
    elif tool == "bash_output":
        return tools_jobs.tool_bash_output(args)
    elif tool == "bash_wait":
        return tools_jobs.tool_bash_wait(args)
    elif tool == "bash_jobs":
        return tools_jobs.tool_bash_jobs(args)
    elif tool == "bash_kill":
        return tools_jobs.tool_bash_kill(args)
    elif tool == "tree_fingerprint":
        # NOT A MODEL TOOL. The worker asks for it to key a recorded
        # check run; no profile offers it and the model has no use
        # for a digest it cannot act on.
        return treefp.tool_tree_fingerprint(args, WORKSPACE)
    else:
        return errors.fail("E_UNKNOWN_TOOL", f"unknown tool: {tool!r}",
                           exit_code=127)


# The default, and the reason it is one. Under a shared netns 0.0.0.0
# exposes the bridge to every container on the network; 127.0.0.1
# restricts it to the namespace the sandbox shares with its caller.
# That is rule 7's "localhost protocol" and it is the tightest reach
# available on Docker and E0.
DEFAULT_BIND = "127.0.0.1"


def _bind_from(argv) -> str:
    """The address to listen on. `--bind ADDR`, else 127.0.0.1.

    AN ARGUMENT AND NOT AN ENVIRONMENT VARIABLE. The sandbox is created
    with `"env": []` -- not "no secrets" but none at all, because the
    cheapest way to keep a container credential-free is to give it
    nothing to hold. A bind address is not a credential, but putting it
    in the env would be the first exception to a rule whose value is
    that it has none.

    A POD HAS NO SHARED NAMESPACE WITH ITS CALLER. The conductor is a
    different pod, so 127.0.0.1 answers nothing and every tool call
    fails. There the sandbox binds 0.0.0.0 and an Ingress
    NetworkPolicy -- `execution-bridge-ingress` in deploy/k8s -- is what
    restricts the reach instead: the namespace is otherwise open on
    ingress, so without that policy any pod could call a shim that runs
    bash. The port moves; the control moves with it.
    """
    if "--bind" in argv:
        index = argv.index("--bind")
        if index + 1 < len(argv):
            return argv[index + 1]
    return DEFAULT_BIND


def main(argv=None):
    bind = _bind_from(list(sys.argv[1:] if argv is None else argv))
    # daemon_threads: a long tool call must not keep the process alive
    # after the pod is asked to stop.
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((bind, BRIDGE_PORT), BridgeHandler)
    print(f"bridge shim listening on {bind}:{BRIDGE_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
