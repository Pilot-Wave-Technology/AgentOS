"""Who may ask the workspace server to do something.

WHAT WAS TRUE BEFORE, AND WAS VERIFIED LIVE ON exec-193.

`podfs` binds `0.0.0.0:9101` in the worker container and has no
authentication of any kind. The sandbox is a container in the SAME pod,
so it shares the network namespace, so the model's own `bash` reaches it
on loopback:

    curl 127.0.0.1:9101 -d '{"op":"explore", ...}'   -> the workspace tree

Nine operations were reachable that way, and they are not readers.
`commit` writes and pushes with the story's git identity. `credentials`
places a lease on disk. `revoke_credentials` removes one. `plan` writes
into every writable git mount. A model that wandered into a port scan --
or was told to by something it read in a repository -- could commit as
the story, or drop the story's own credential.

THIS IS A SURFACE THAT GREW WHILE NOBODY LOOKED AT IT. It began as
`read`, for the file explorer. The conductor-side filesystem work of the
last week added `commit`, `plan`, `digest`, `archive_mounts`,
`credentials` and `revoke_credentials` to it, one at a time, each for a
good reason, none of them the moment anybody asked what could call it.

── the token is DERIVED, not stored ────────────────────────────────────

A per-execution secret has to be known in two places that never meet: the
worker container, which checks it, and whichever conductor replica makes
the next call, which presents it. The three ways to do that are a column
on `executions` (a migration, and a secret at rest in the database), a
read-back of the pod's own env through the Kubernetes API (visible to
anything holding the pod's projected service-account token), or a
derivation from a key the conductor already has.

The third. `AGENT_TOKEN_SIGNING_KEY` is already in `conductor-secrets`,
the derivation is a pure function of the execution id, and the worker
container is given ONLY its own answer -- never the key. So a token that
leaks is one execution's token, and it cannot be used to mint another.

THE SANDBOX MUST NEVER HOLD THIS VALUE. Two independent lists have to
keep it out: the sandbox container's `env` in `drivers/k8s_spec.py`, and
`_safe_env()` in `bridge/tools_core.py`, which is a SECOND allowlist
applied inside the sandbox before `bash` sees anything. Both are
asserted together by the guard test, because a secret kept out of one
and not the other is not kept out -- which is exactly how the proxy
variables were fixed twice and still did not reach the model.
"""
from __future__ import annotations

import hashlib
import hmac
import os

# The environment variable the WORKER container carries and the sandbox
# does not. Named here once: `k8s_spec` injects it, `podfs` demands it,
# `podfs_client` presents it, and a fourth spelling in any of them is a
# pod that starts and refuses every call.
ENV_VAR = "AGENTOS_PODFS_TOKEN"

# A header rather than a query parameter or a body field: a URL reaches
# access logs, and a body field would have to be threaded through every
# op's argument parsing, where one op forgetting it is one op still open.
HEADER = "X-AgentOS-Workspace"

# The conductor's own signing key, already mounted from `conductor-secrets`
# and already used to sign agent tokens. Reused rather than joined by a
# second secret to rotate: one key, one rotation, one thing to get wrong.
SIGNING_KEY_VAR = "AGENT_TOKEN_SIGNING_KEY"

# Namespaced so a token minted here can never be replayed as an agent
# token, and vice versa.
_PURPOSE = "podfs"

# ── THE READ-ONLY SCOPE, AND WHY IT NEEDED NO NEW SECRET ────────────────
#
# E5 has the VERIFICATION pod pull its own tree: it dials podfs on 9101
# directly, streams `archive` to its volume, and the Deployment never
# touches the bytes. `execution-bridge-ingress` can admit it, but
# admitting it with the full token would hand a criterion `publish`,
# `commit`, `plan` and `credentials` -- so a check able to call the
# workspace could push as the story, or rewrite the very tree it is
# checking. The scope is the whole point of letting it dial at all.
#
# A SECOND SECRET WAS NOT NEEDED, and that is what makes this shippable
# without touching the pod spec's env or `_safe_env`'s allowlist. The
# read-only token is derived from the FULL token rather than from the
# signing key, so:
#
#   * the conductor derives it -- it has the full token already;
#   * the WORKER verifies it -- it holds its own full token and can
#     compute this from it, still without ever seeing the signing key;
#   * the implication runs one way only. Holding the read-only token
#     gets you no closer to the full one than any other sha256 preimage.
#
# The alternative -- a second AGENTOS_PODFS_RO_TOKEN injected alongside
# the first -- would need `k8s_spec` and `_safe_env` changed together,
# which is precisely the two-list arrangement that let the proxy
# variables be fixed twice and still reach the model.
_READ_ONLY = "readonly"
_FULL = "full"

# WHAT A READ-ONLY CALLER MAY ASK FOR. An allowlist and never a
# denylist: the surface grew from `read` to eleven operations one at a
# time, each for a good reason, and a denylist would have silently
# admitted every one of them. A twelfth op is refused by default and
# somebody has to say out loud that it only reads.
READ_ONLY_OPS = frozenset({"read", "archive", "archive_mounts", "heads",
                           "digest", "explore",
                           # `changed` runs `git merge-base` and
                           # `git diff --stat` and returns text. It
                           # writes nothing, checks nothing out, and
                           # moves no ref -- said out loud, as the note
                           # above requires.
                           "changed"})


class TokenMissing(RuntimeError):
    """Nothing can be authorised, so refuse at startup rather than later.

    A server that starts and then refuses every call looks like a bug in
    its caller, and costs an afternoon before anyone checks the env.
    """


def derive(execution_id: int) -> str:
    """This execution's token. Raises `TokenMissing` without the key.

    Called on the CONDUCTOR side -- at spec build, to inject it, and by
    `podfs_client`, to present it. The worker never calls this: it has
    the answer and not the key.
    """
    if SIGNING_KEY_VAR not in os.environ:
        raise TokenMissing(
            f"{SIGNING_KEY_VAR} is unset, so no workspace token can be "
            f"derived. It is mounted from `conductor-secrets`; a "
            f"conductor without it cannot talk to any execution's "
            f"workspace.")
    key = os.environ[SIGNING_KEY_VAR].strip()
    if not key:
        raise TokenMissing(
            f"{SIGNING_KEY_VAR} is present but empty -- the classic "
            f"unset-variable-rendered-as-empty-string failure. Every "
            f"execution would derive the same token from an empty key.")
    return hmac.new(key.encode("utf-8"),
                    f"{_PURPOSE}:{execution_id}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def read_only_of(token: str) -> str:
    """The read-only twin of a full workspace token.

    Both ends compute it: the conductor from `derive`, the worker from
    the value it was given. Neither needs the other's input.
    """
    return hmac.new(token.encode("utf-8"), _READ_ONLY.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def derive_read_only(execution_id: int) -> str:
    """This execution's READ-ONLY token. For the verification pod."""
    return read_only_of(derive(execution_id))


def configured_token() -> str:
    """The token THIS process was given. The worker side.

    Read per call rather than captured at import: the worker and the
    conductor are separate processes, and a value frozen at import in one
    is a value the other cannot correct. The house rule in `kernel/` bans
    `environ.get(x, default)` -- a default nobody can see is a policy
    nobody reviewed -- and here the policy is that there is no default.
    """
    if ENV_VAR not in os.environ:
        raise TokenMissing(
            f"{ENV_VAR} is unset, so the workspace server cannot tell "
            f"its caller from the sandbox sharing its network namespace. "
            f"It is injected into the WORKER container by "
            f"drivers/k8s_spec.py; a pod without it is a pod whose "
            f"workspace the model can commit to.")
    token = os.environ[ENV_VAR].strip()
    if not token:
        raise TokenMissing(
            f"{ENV_VAR} is present but empty, which would authorise an "
            f"empty header -- that is, every caller.")
    return token


def scope(presented: str | None) -> str | None:
    """Which token is this -- "full", "readonly", or none of ours?

    `hmac.compare_digest` rather than `==`: the comparison is against a
    value that can be guessed one character at a time if the compare
    short-circuits, and doing it properly costs nothing. BOTH branches
    use it, including the read-only one -- a scope check that leaks its
    own answer by timing would let a sandbox walk up to the narrow
    token, and from there to every op on this list.
    """
    if not presented:
        return None
    try:
        expected = configured_token()
    except TokenMissing:
        # `serve()` checks at startup, so this cannot be reached through
        # it -- but a direct caller must not get a scope out of a
        # misconfiguration.
        return None
    if hmac.compare_digest(presented, expected):
        return _FULL
    if hmac.compare_digest(presented, read_only_of(expected)):
        return _READ_ONLY
    return None


def permits(holder: str | None, op: str) -> bool:
    """May a caller at this scope run `op`?

    An UNKNOWN scope permits nothing, which is the direction that fails
    safe: a fourth scope added later and not listed here is refused
    rather than treated as full.
    """
    if holder == _FULL:
        return True
    return holder == _READ_ONLY and op in READ_ONLY_OPS


def matches(presented: str | None) -> bool:
    """Does `presented` authorise ANY call? The pre-scope question.

    Kept because the gate still asks it first: an unrecognised token is
    a 401 and gets no hint about which operations exist, while a
    recognised one too narrow for the op it named is a 403. Two facts,
    two answers.
    """
    return scope(presented) is not None
