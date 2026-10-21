"""Run the pod this code WOULD dispatch, without a build or a dispatch.

THE LOOP THIS REPLACES TOOK TEN MINUTES AND FOUND ONE BUG. Push, wait
for Cloud Build, wait for the conductor to roll, dispatch an errand by
hand, watch a pod, read a log, find the next defect, repeat. Four
defects in a row were found that way and every one of them lived in the
POD MANIFEST rather than in the image:

    sandbox_attachments   the wrong spec builder
    await_ready           a kwarg one driver refused
    runAsNonRoot          the worker had no uid
    cp -a                 a non-root copy cannot preserve

The manifest is rendered by kernel code on THIS machine. Only the code
INSIDE the image needs a build, and none of those four were. So this
renders the manifest locally, applies it under a name nothing else
owns, reports what each container did, and deletes it.

WHAT IT DOES NOT TEST, said plainly so nobody reads a green here as
more than it is: the image's own contents are whatever is deployed, so
a change to `podinit.py` or `podfs.py` still needs a build. This checks
the SHAPE of the pod -- security contexts, volumes, commands, images,
init ordering, and whether Autopilot admits any of it.

    python -m scripts.podcheck            # render, apply, report, delete
    python -m scripts.podcheck --keep     # leave it running to poke at
    python -m scripts.podcheck --render   # print the manifest, apply nothing
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

NAMESPACE = os.environ.get("AGENTOS_K8S_NAMESPACE") or "agentos-staging"
# NOT `exec-<id>`. The reconciler reaps pods by that name against the
# executions table, and a check pod wearing one would be reaped as an
# orphan -- or worse, counted as a live execution's compute.
NAME = f"podcheck-{int(time.time())}"


def _kubectl(*args, check=True, capture=True):
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args],
                          capture_output=capture, text=True, check=check)


def manifest() -> dict:
    """The pod this code would dispatch for a one-repo errand."""
    from kernel.conductor.spec_builder import build_spec
    from kernel.drivers import k8s_bridge
    from kernel.drivers.k8s_spec import pod_manifest
    from kernel.workspace.mounts import Mount

    # A representative grant: one writable git mount on a branch, which
    # is what every story and most errands actually get.
    #
    # OVERRIDABLE, BECAUSE THE DEFAULT IS THE EASY CASE. A public repo
    # with no ref and kind="repo" cleared every container while the real
    # execution -- a PRIVATE repo, kind="branch", a ref that must exist
    # -- failed in `workspace-compose`, and a check that only ever runs
    # the easy case reports green for the thing that is broken. Read the
    # real mount off an execution with
    #
    #   mounts_for(conn, <id>)
    #
    # in the conductor and pass it here.
    source = (os.environ.get("PODCHECK_REPO")
              or "https://github.com/octocat/Hello-World.git")
    mount = Mount(at=os.environ.get("PODCHECK_AT")
                  or "repos/podcheck__sample", binding="git",
                  source=source,
                  ref=os.environ.get("PODCHECK_REF") or "",
                  writable=True,
                  kind=os.environ.get("PODCHECK_KIND") or "repo")
    # A STORY, because the git credential broker scopes its token to
    # one. Without it `_credential_helper` returns "" and a private
    # clone runs unauthenticated -- which is the exact defect this
    # script was pointed at, and it would have gone on reproducing it
    # for the wrong reason.
    spec = build_spec({"id": 0, "kind": "errand", "domain": "ops",
                       "goal_id": "PODCHECK",
                       "story_id": os.environ.get("PODCHECK_STORY") or ""},
                      [], dict(os.environ), mounts=(mount,))
    pod = pod_manifest(spec, NAME, namespace=NAMESPACE,
                       pvc_name=f"ws-{NAME}",
                       service_account=os.environ.get(
                           "AGENTOS_K8S_SERVICE_ACCOUNT")
                       or "agentos-execution",
                       bridge_image=k8s_bridge.kernel_image(),
                       # --public omits the bearer, so a public clone
                       # exercises the proxy and the egress policy
                       # WITHOUT also exercising the credential path.
                       # One variable at a time is the whole value of a
                       # loop this short.
                       workspace_secret=(
                           None if "--public" in sys.argv
                           else os.environ.get("AGENTOS_WORKSPACE_SECRET")
                           or "conductor-secrets"),
                       control_panel_url=os.environ.get(
                           "CONTROL_PANEL_URL") or "",
                       egress_proxy=os.environ.get("AGENTOS_EGRESS_PROXY")
                       or "http://egress-proxy:3128")
    # A label so a stray one is findable, and NOT `managed-by: kernel`,
    # which is what the reconciler reaps on.
    pod["metadata"]["labels"]["podcheck"] = "true"
    return pod


def claim(pod) -> dict:
    from kernel.conductor.spec_builder import build_spec
    from kernel.drivers.k8s_spec import pvc_manifest

    spec = build_spec({"id": 0, "kind": "errand", "domain": "ops",
                       "goal_id": "PODCHECK"}, [], {})
    manifest = pvc_manifest(spec, f"ws-{NAME}", namespace=NAMESPACE)
    # SO SOMETHING ELSE CAN COLLECT IT. The delete below is best effort:
    # it is skipped under `--keep`, and it never runs at all if this
    # script is killed. `list_workspaces` then leaves the claim alone for
    # ever, because it reaps only names shaped `ws-exec-<digits>` -- a
    # rule written to protect an operator's hand-made claim, which this
    # is not. Measured: one `ws-podcheck-*` claim Pending for 19 hours
    # against a 120-claim namespace quota, with no collector anywhere.
    #
    # The label says "nobody will miss this after a while", which is the
    # one thing an age-based reaper needs and a name cannot say.
    manifest["metadata"].setdefault("labels", {})
    manifest["metadata"]["labels"]["agentos-ephemeral"] = "true"
    return manifest


def report() -> bool:
    """Print what each container did. True when everything started."""
    out = _kubectl("get", "pod", NAME, "-o", "json", check=False)
    if out.returncode != 0:
        print(f"  pod {NAME} is gone")
        return False
    status = json.loads(out.stdout).get("status", {})
    ok = True
    for key, label in (("initContainerStatuses", "init"),
                       ("containerStatuses", "run ")):
        for c in status.get(key) or []:
            state = c.get("state") or {}
            kind = next(iter(state), "unknown")
            detail = state.get(kind) or {}
            note = (detail.get("reason") or detail.get("message")
                    or f"exit={detail.get('exitCode')}"
                    if kind != "running" else "running")
            print(f"  {label} {c['name']:20} {kind:11} {note}")
            if kind == "terminated" and detail.get("exitCode"):
                ok = False
                logs = _kubectl("logs", NAME, "-c", c["name"], check=False)
                for line in (logs.stdout or logs.stderr).strip().splitlines()[-8:]:
                    print(f"       | {line}")
            if kind == "waiting":
                ok = False
    return ok and bool(status.get("containerStatuses"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pod = manifest()
    if "--render" in argv:
        print(json.dumps(pod, indent=2))
        return 0

    print(f"applying {NAME} to {NAMESPACE}")
    for body in (claim(pod), pod):
        applied = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "apply", "-f", "-"],
            input=json.dumps(body), capture_output=True, text=True)
        if applied.returncode != 0:
            # An admission refusal lands here, which is the fastest
            # feedback this whole script exists to give: Autopilot says
            # no before anything is scheduled.
            print(applied.stderr.strip())
            return 1
        print(f"  {applied.stdout.strip()}")

    ok = False
    for _ in range(40):
        time.sleep(5)
        phase = _kubectl("get", "pod", NAME, "-o",
                         "jsonpath={.status.phase}", check=False).stdout
        print(f"  phase={phase}")
        ok = report()
        if ok or phase in ("Failed", "Succeeded"):
            break

    if "--keep" not in argv:
        print(f"deleting {NAME}")
        _kubectl("delete", "pod", NAME, "--wait=false", check=False)
        _kubectl("delete", "pvc", f"ws-{NAME}", "--wait=false", check=False)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
