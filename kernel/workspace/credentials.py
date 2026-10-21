"""Ec4b — how a credential reaches an errand without reaching the journal.

THE SHAPE, and why it is a file rather than an environment variable.

An env var is the obvious answer and it is the wrong one, for three
reasons that each bit somebody already:

  * IT IS IN EVERY CHILD PROCESS'S `environ`, so `env`, `ps eww`, a crash
    dump and any library that logs its configuration all carry it. Stage
    G watched agents echo tokens through `exec` and `bash`; the fix there
    was to sanitize output, which is a filter on the symptom.
  * IT CANNOT BE REVOKED MID-RUN. A process started with a variable keeps
    the value it started with, so a lease that expires or a grant somebody
    withdraws is still live inside a running leg.
  * IT HAS NO LEASE ID. When the journal shows a call that used a
    credential, there is nothing to point at — and "which grant let this
    happen" is the first question anybody asks afterwards.

A FILE UNDER `/workspace` fixes all three. It is read at the moment of
use, so revoking is deleting. It is not in anybody's `environ`. And the
POINTER is what the journal records: `AGENTOS_CREDENTIALS` names a
directory, the lease id names a file in it, and the token itself is never
a value anything prints.

WHAT GOES IN THE ALLOWLIST is therefore the POINTER, never the secret.
`_safe_env` in the sandbox is an allowlist precisely so a leaked variable
is not forwarded; adding a credential to it would make the allowlist the
leak. `AGENTOS_CREDENTIALS` is a path, and a path is safe to print.

NOTHING MINTS ANYTHING HERE. This is the seam: where a credential lands,
what names it, and what the journal is allowed to see. Minting is per
provider — GCP's is `Ec4d`, AWS's is `Ec4e` — and both need real cloud
access, which E0 does not have and is not going to get.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat

logger = logging.getLogger(__name__)

# The pointer, and the only credential-shaped name the sandbox may carry.
ENV_POINTER = "AGENTOS_CREDENTIALS"

# Where leases land, relative to the workspace root. Under the workspace
# so it dies with the workspace: a credential that outlives the run it was
# minted for is a credential nobody is tracking.
LEASE_DIR = ".agentos/credentials"

# A lease id is a filename. Anything that is not this is a path traversal
# wearing a lease's clothes.
_LEASE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class BadLease(ValueError):
    """The lease id is not one, or the payload is not storable."""


def lease_dir(workspace_path: str) -> str:
    return os.path.join(workspace_path or "/workspace", *LEASE_DIR.split("/"))


# Every sidecar `issue` may write beside a lease. `revoke` deletes
# each one, so a new sidecar is one entry here and not a leak.
SIDECAR_SUFFIXES = (".token",)


def lease_path(workspace_path: str, lease_id: str) -> str:
    if not _LEASE_ID.match(str(lease_id or "")):
        raise BadLease(
            f"{lease_id!r} is not a lease id: lowercase letters, digits, "
            f"`-` and `_`, 3 to 64 characters. A lease id becomes a "
            f"filename, so anything else is a path.")
    return os.path.join(lease_dir(workspace_path), lease_id + ".json")


def write_lease(workspace_path: str, lease_id: str,
                payload: dict | str) -> str:
    """Put a credential on disk and return its path. Owner-readable only.

    0600 AND A 0700 DIRECTORY. The sandbox runs as its own uid and the
    Verifier as `nobody`; a world-readable credential in a shared
    workspace is available to a process that was never granted it.

    A DICT IS WRITTEN AS JSON, A STRING VERBATIM. Clouds do not agree on
    the format of a credential file: GCP reads ADC JSON, AWS reads an INI
    under `AWS_SHARED_CREDENTIALS_FILE`, Azure reads a bare federated
    token. JSON-only would force two of the three through a translation
    nobody asked for, so the provider decides and this writes what it is
    given.
    """
    path = lease_path(workspace_path, lease_id)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, stat.S_IRWXU)
    except OSError:                                          # noqa: BLE001
        # Windows and some mounted filesystems do not carry POSIX modes.
        # Not fatal, and not silent: a deployment where this fails is one
        # where the file permission is not the control it looks like.
        logger.warning("could not restrict %s to owner-only", directory)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:                                          # noqa: BLE001
        logger.warning("could not restrict %s to owner-only", path)
    logger.info("credential lease %s written for workspace %s",
                lease_id, workspace_path)
    return path


def read_lease(workspace_path: str, lease_id: str) -> dict | None:
    """The credential, or None when the lease is gone.

    NONE IS THE REVOCATION PATH. Deleting the file is how a grant is
    withdrawn mid-run, and a caller that treats a missing lease as an
    error rather than as a no would turn revocation into a crash.

    A NON-JSON LEASE ALSO READS AS NONE, and that is a real case now:
    `write_lease` takes a string so AWS can write the INI its tooling
    reads. Callers that need the credential itself go through the
    provider; this is for the JSON leases whose provenance fields are
    worth reading back.
    """
    try:
        with open(lease_path(workspace_path, lease_id),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def sidecar_path(workspace_path: str, lease_id: str, suffix: str) -> str:
    """A file beside the lease, sharing its id. Same rules, same mode.

    WHY A SECOND FILE. The lease is the RECORD -- provenance, expiry, the
    grant it came from -- and a record is JSON. What `gcloud` needs is a
    file containing the raw token and nothing else (`--access-token-file`
    / CLOUDSDK_AUTH_ACCESS_TOKEN_FILE), because there is no ADC shape for
    a bare access token: `authorized_user` demands client_id,
    client_secret and refresh_token, and a file missing them does not
    load AT ALL.

    That was found the expensive way. A run held a correctly minted
    token, in a file no SDK could parse, and reported no credentials --
    with the IAM policy, the pointer and the token all correct.
    """
    if not _LEASE_ID.match(str(lease_id or "")):
        raise BadLease(f"{lease_id!r} is not a lease id")
    if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise BadLease(f"{suffix!r} is not a sidecar suffix")
    return os.path.join(lease_dir(workspace_path), lease_id + suffix)


def write_sidecar(workspace_path: str, lease_id: str, suffix: str,
                  text: str) -> str:
    """Write a sidecar verbatim, 0600 in a 0700 directory."""
    path = sidecar_path(workspace_path, lease_id, suffix)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def place_lease(workspace_path: str, lease_id: str, payload: dict,
                sidecars: dict | None = None) -> list:
    """The lease and every sidecar, in one call. Returns what was written.

    ONE CALL because it has to cross a process boundary. On Kubernetes
    the workspace is in the execution's pod and the issuer runs in the
    conductor, so this is what `podfs.op_credentials` invokes THERE --
    the same function, on the filesystem that has the workspace.
    """
    write_lease(workspace_path, lease_id, payload)
    out = [lease_path(workspace_path, lease_id)]
    for suffix, text in (sidecars or {}).items():
        write_sidecar(workspace_path, lease_id, suffix, text)
        out.append(sidecar_path(workspace_path, lease_id, suffix))
    return out


def revoke(workspace_path: str, lease_id: str) -> bool:
    """Delete the lease AND every sidecar written beside it.

    Deleting only the .json would leave the raw token on disk, which is
    the file that actually authenticates -- a revoke that removes the
    record and keeps the credential is worse than none, because it reads
    as done."""
    gone = False
    try:
        os.unlink(lease_path(workspace_path, lease_id))
        gone = True
    except OSError:
        pass
    for suffix in SIDECAR_SUFFIXES:
        try:
            os.unlink(sidecar_path(workspace_path, lease_id, suffix))
            gone = True
        except OSError:
            pass
    return gone


def sandbox_env(workspace_container_path: str = "/workspace") -> dict:
    """What the sandbox is told. A PATH, never a secret.

    The container sees its own workspace path, which is not the host's —
    handing it the host path would name a directory it cannot open and
    read as a missing credential rather than as a misconfiguration.

    POSIX SEPARATORS, ALWAYS. This built the path with `os.path.join`,
    which uses the HOST's separator: a worker on Windows handed the
    sandbox a path with BACKSLASHES in it -- one filename rather than
    a directory. The sandbox is Linux
    whatever the worker runs on, so the container path is POSIX by
    definition and never the host's idea of a path. `lease_dir` above is
    the other case and correctly uses `os.path.join`: it names a
    directory the WORKER writes to.
    """
    return {ENV_POINTER: "/".join(
        [(workspace_container_path or "/workspace").rstrip("/"),
         *LEASE_DIR.split("/")])}


# ── what the journal may hold ────────────────────────────────────────────

# Shapes that are credentials whatever they are called. Matched against
# VALUES, because the field name is the thing that varies: `token`,
# `key`, `secret`, `password`, `authorization`, and whatever the next
# provider calls it.
_SECRET_SHAPES = (
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),            # GitHub PAT
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),          # OpenAI / Anthropic
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),              # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),              # AWS session key id
    re.compile(r"\bya29\.[A-Za-z0-9\-_]{20,}"),       # Google OAuth
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\."),  # JWT
)

REDACTED = "[redacted credential]"


def scrub(text: str) -> str:
    """Replace anything credential-shaped. For journal rows and cards.

    A LAST LINE, NOT THE DESIGN. The design is that the secret is in a
    file nothing prints. This catches the case where a tool output
    happened to contain one anyway — a `cat` of the lease, a library that
    logs its own configuration — because a journal is durable and a
    credential in it outlives every rotation.
    """
    out = str(text or "")
    for shape in _SECRET_SHAPES:
        out = shape.sub(REDACTED, out)
    return out


def holds_secret(text: str) -> bool:
    return scrub(text) != str(text or "")
