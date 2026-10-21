"""The way out of a pod, and the label that grants it. One declaration.

WHAT WAS TRUE BEFORE, AND IT IS A HANG RATHER THAN A REFUSAL.

The namespace is default-deny. A pod reaches the internet only through
squid, and two NetworkPolicies decide which port it may use:

    agentos.io/egress: proxy   ->  TCP 3128, the full allowlist
    agentos.io/egress: store   ->  TCP 3129, the object store alone

The label was spelled in four places and the PROXY ADDRESS was chosen
somewhere else entirely, so nothing made the two agree. They did not:

  * `k8s_capture.capture_manifest` labels its pod `proxy` (3128 only).
  * `k8s_ephemeral.run_capture` hands that pod `fetch_env()`, which sets
    HTTP_PROXY to AGENTOS_STORE_PROXY -- port 3129 -- for BOTH the
    `fetch-tree` init container and the `capture` sidecar.

3129 from a `proxy`-labelled pod is denied twice over: by that pod's own
egress rule, and by `egress-proxy-ingress`, which accepts 3129 only from
`store`-labelled pods. Default-deny drops rather than resets, so the
connection does not fail -- it HANGS. The measured version of this on the
executor side was `pip install` never returning and holding a lock for
twenty minutes; here it is a capture that cannot fetch its tree and a
sidecar that cannot upload the directory it captured. Both directions of
the artifact store, on the one path a criterion needs to run at all.

`fetch_env`'s own comment asserted the pod was labelled `store`. That is
true of a criterion Job and false of a capture Job, and a comment is not
a thing the second caller has to satisfy.

WHY A LABEL IS THE INPUT AND NOT A PROXY ADDRESS. The caller that picks
the label is the caller that knows what the pod is for. Asking it for the
label and deriving the address means the two cannot disagree again; asking
it for an address just moves the same mistake one call up.

THE FULL PORT STILL REACHES THE STORE. squid's `full_hosts` includes
`.googleapis.com` (deploy/k8s/conductor.yaml), so a capture pod on 3128
writes artifacts exactly as before. `store` is the NARROWER of the two and
is right for a criterion, which must not reach the forge; nothing here
widens anything.
"""
from __future__ import annotations

import os

# What the NetworkPolicies select on. Not the pod's `app` label: see the
# comment in k8s_spec.py, which makes the same point from the other side.
LABEL = "agentos.io/egress"

# The full allowlist -- package indexes, the forge, the store. For a pod
# that runs somebody's setup script or our own capture sidecar.
VIA_PROXY = "proxy"

# The object store alone. For a criterion's Job, which must be able to
# read the tree it checks and nothing else.
VIA_STORE = "store"

# THE SAME ALLOWLIST AS `proxy`, AND A DIFFERENT POLICY. For the capture
# Job, and only it.
#
# Measured on execution 205. The capture Job wore `proxy`, which is
# correct for its WORK container -- that container runs `pip install` and
# needs the full port. But its SIDECAR calls `storage.Client()`, and that
# takes the project id and the token from 169.254.169.254, which
# `workload-egress-via-proxy` does not admit. So the install succeeded,
# the upload died, the archive came back empty, and every criterion
# returned `error`:
#
#   capture: OSError: Project was not passed and could not be determined
#            from the environment.
#
# The run then looped VERIFYING -> RUNNING for ever, with the executor
# asked to fix work that was never broken.
#
# AND THE MISSING RULE COULD NOT SIMPLY BE ADDED TO `proxy`. That label
# also selects the EXECUTION pod, whose sandbox shares the pod's network
# namespace, so metadata there would let model-authored code mint a
# Workload Identity token for `agentos-execution` -- the one property the
# two-uid pod design rests on. The note in conductor.yaml says so
# explicitly, and it is right.
#
# So the capture Job gets its own label instead. It already has its own
# identity -- `agentos-capture`, distinct from `agentos-execution` and
# `agentos-verifier` -- and this makes the egress grant match the
# identity boundary that was already drawn.
VIA_CAPTURE = "capture"

# THE PREPARE JOB, and it needs a pair no other class has: the full port
# for `pip`, AND an execution pod's podfs on 9101 for the committed tree.
#
# It cannot be `proxy`: that class ALSO SELECTS THE EXECUTION POD, whose
# sandbox runs model-authored code, so the 9101 rule there would let a
# story's own code dial other stories' workspaces. NetworkPolicy is
# per-pod, which is the same wall that produced `capture` after 205.
#
# And it should not be `capture`: that class exists to reach the
# METADATA SERVER, for a sidecar calling `storage.Client()`. This Job has
# no sidecar, no artifact store and no cloud identity to mint -- faults 8
# and 9 have nothing left to fail at here -- so granting it metadata
# would hand a cloud credential path to the one container in this design
# that runs a repository's own `.agentos/setup.sh`.
#
# Same address and port as `proxy` and `capture`; a different policy.
VIA_PREPARE = "prepare"

# Which address each label is permitted to use. The conductor holds them
# and passes them on; it makes no outbound calls of its own.
#
# `capture` shares `proxy`'s ADDRESS because it shares its port: 3128,
# the full allowlist. They differ in POLICY, not in route, which is why
# this maps two labels to one variable rather than inventing a third
# address nobody would have set.
_PROXY_VAR = {
    VIA_PROXY: "AGENTOS_EGRESS_PROXY",
    VIA_STORE: "AGENTOS_STORE_PROXY",
    VIA_CAPTURE: "AGENTOS_EGRESS_PROXY",
    VIA_PREPARE: "AGENTOS_EGRESS_PROXY",
}


class UnknownEgress(ValueError):
    """The label is not one the NetworkPolicies know.

    Never silently resolved to a default. A typo that fell back to
    `store` would give a setup script the object store's allowlist and
    a hang on the first package index -- which is this module's own bug
    with a different spelling.
    """


def proxy_for(label: str) -> str:
    """The proxy address a pod carrying `label` is actually allowed to use.

    Empty when the deployment set no address, which is E0 and the test
    fixtures: there is no proxy there and no policy to need one, and the
    callers already treat "" as "no proxy variables".

    Read per call rather than captured at import -- the conductor and the
    worker are separate processes, and the house rule in `kernel/` bans
    `environ.get(x, default)` because a default nobody can see is a
    policy nobody reviewed.
    """
    if label not in _PROXY_VAR:
        raise UnknownEgress(
            f"{label!r} is not an egress class; expected "
            f"{VIA_PROXY!r}, {VIA_STORE!r}, {VIA_CAPTURE!r} or "
            f"{VIA_PREPARE!r}. The label "
            f"and the proxy address are one decision -- a pod labelled "
            f"for one port and pointed at the other does not fail, it "
            f"hangs.")
    var = _PROXY_VAR[label]
    if var not in os.environ:
        return ""
    return os.environ[var].strip()


# ── what must NEVER go through the proxy ────────────────────────────────
#
# THREE SPELLINGS OF THIS EXISTED and none of them was right:
#
#   k8s_spec        localhost,127.0.0.1,::1,.svc,.svc.cluster.local,
#                   .cluster.local
#   k8s_workspace   localhost,127.0.0.1,.svc,.cluster.local,10.0.0.0/8
#   k8s_ephemeral   localhost,127.0.0.1,.svc,.cluster.local
#
# Nobody could say which was correct, which is the tell. They differed on
# `::1`, on the pod CIDR, and on the full cluster suffix -- and all three
# omitted the one address that is not optional.
#
# 169.254.169.254 IS THE IDENTITY. Workload Identity, the project id, and
# every access token come from the metadata server on that link-local
# address. Send it through squid and it is denied like any other host,
# and the SDK reports the consequence rather than the cause:
#
#   capture: OSError: Project was not passed and could not be determined
#            from the environment.
#
# Measured on execution 202's capture Job. `storage.Client()` is called
# with no arguments ON PURPOSE -- kernel/artifacts/gcs.py explains that a
# caller passing explicit credentials would be carrying a secret the
# kernel deliberately does not -- so the ambient identity IS the design,
# and cutting the pod off from it breaks every GCS write in a proxied
# pod. The conductor was unaffected because it has no proxy at all, which
# is exactly why this only ever bit the Jobs.
#
# The pod CIDR is in here because pod-to-pod is not the proxy's business
# either: the conductor reaching an execution pod's bridge, and podfs
# answering it, are in-cluster and were only ever routed correctly by the
# one of the three lists that happened to name 10.0.0.0/8.
NO_PROXY = ",".join((
    "localhost", "127.0.0.1", "::1",
    "169.254.169.254", "metadata.google.internal", "metadata",
    ".svc", ".svc.cluster.local", ".cluster.local",
    "10.0.0.0/8",
))


def proxy_env(proxy: str) -> list:
    """The proxy variables a container needs, as a k8s env list.

    BOTH CASES, deliberately: curl reads lowercase `http_proxy`, most
    Python tooling reads either, npm reads both -- and a half-set pair is
    a tool that silently bypasses the proxy and hangs as if the network
    were gone.

    Empty for an empty proxy, which is E0 and the test fixtures: there is
    no proxy there and no policy needing one.
    """
    if not proxy:
        return []
    out = []
    for name in ("HTTP_PROXY", "HTTPS_PROXY"):
        out.append({"name": name, "value": proxy})
        out.append({"name": name.lower(), "value": proxy})
    out.append({"name": "NO_PROXY", "value": NO_PROXY})
    out.append({"name": "no_proxy", "value": NO_PROXY})
    return out
