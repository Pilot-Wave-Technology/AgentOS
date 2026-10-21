"""No kernel behaviour may depend on a fact only the local file declares.

THE BUG THIS IS BUILT FROM, which cost five dispatches on `next`.
`GIT_CREDENTIAL_URL` had one reader -- `_credential_helper` in
`kernel/workspace/bindings.py` -- and exactly one writer:

    infra/e0/docker-compose.yml

On Kubernetes nothing set it, the helper returned "", the clone of a
private repo ran unauthenticated, and GitHub asked for a username. Git
had prompting disabled, so the pod died in `workspace-compose` with
`could not read Username for 'https://github.com'`: a credential failure
wearing a terminal's clothes. "It worked when we were testing locally"
was exactly true, and this is the shape of why.

WHY THE SUITE DID NOT SEE IT. The two tests that exercise the helper set
the variable themselves with `monkeypatch.setenv` -- correctly, they are
testing what the helper does with a broker. Nothing anywhere asked
whether a deployment provides one. A test that supplies the input it is
checking the handling of cannot discover that no one supplies it.

SO THIS ASKS THE DEPLOYMENTS, NOT THE CODE. It reads the names declared
in the local compose file, keeps those the kernel actually references,
and drops any that something else also sets. What is left is the set of
facts the kernel needs and only a developer's laptop provides. That set
is allowed to be non-empty -- a Docker socket is genuinely local -- but
every member is written down here with a reason, so a new one is a test
failure and a decision rather than a discovery on a cluster.
"""
from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]
E0 = REPO / "infra" / "e0" / "docker-compose.yml"

# EVERY FILE THAT CONFIGURES A DEVELOPER MACHINE, not just the compose
# one. This guard was written for GIT_CREDENTIAL_URL and scanned only
# E0 -- and then KERNEL_WORKSPACE_URL did exactly the same thing from
# `local-development/env/local.values`: read in one place, written
# there and nowhere deployed, so the file explorer silently served an
# empty panel for every execution on Kubernetes. A guard narrower than
# the class it names will keep missing members of it.
LOCAL_VALUES = REPO / "local-development" / "env" / "local.values"

# An environment name as the compose file writes it: `  NAME: value`.
# The value is required, so a bare key or a word inside prose is not
# mistaken for a declaration.
DECLARED = re.compile(r"^\s+([A-Z][A-Z0-9_]{3,}):\s*\S", re.M)

# A service block: `  name:` at two spaces, until the next one.
SERVICE = re.compile(r"^  ([a-z][a-z0-9-]*):$", re.M)

# What an environment name looks like, so a scan of string constants
# does not treat every sentence as one.
NAME = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")

# Everywhere else a variable can be set for a deployed process.
ELSEWHERE = ("deploy/**/*.yaml", "deploy/**/*.yml", "cloudbuild*.yaml",
             "scripts/**/*.sh", "scripts/**/*.py", "app/**/*.py",
             "Dockerfile*")

# THE SET, WITH ITS REASONS. Each of these is referenced by kernel/ and
# declared only in the local compose file, and each is fine.
LOCAL_ONLY = {
    "DOCKER_HOST":
        "the Docker driver's socket. There is no Docker driver on "
        "Kubernetes, so a deployment that does not set it is a "
        "deployment that does not use it.",
    "AGENTOS_ASSISTANT_API_OPEN":
        "a development switch that serves the assistant API without "
        "auth. It MUST be absent everywhere else; a deployment that "
        "set it would be the bug.",
    "AGENTOS_CREDENTIAL_PROVIDER":
        "selects the cloud-credential provider. Absent means none is "
        "issued, which is the correct default for an execution that "
        "was granted no cloud identity.",
    # ANTHROPIC_API_KEY LEFT THIS LIST ON 2026-10-19, and how it got
    # here is worth keeping.
    #
    # Its entry read: "a secret. Cloud deployments deliver it through
    # Secret Manager and a secretKeyRef, which is invisible to a scan
    # of the repository -- the compose file is the only place a literal
    # can appear at all."
    #
    # Every clause of that was true, and together they explained away
    # the symptom of a real bug. The secretKeyRef existed and resolved
    # to NOTHING: `deploy_execution_plane.sh` wrote the key into
    # `conductor-secrets` as CLAUDE_API_KEY while the pod asked for
    # ANTHROPIC_API_KEY, so no deployed execution ever received an
    # Anthropic key. The name's absence from the provisioning script
    # was the evidence, and this list said that absence was expected.
    #
    # The script now writes ANTHROPIC_API_KEY, the literal appears, and
    # `_local_only()` stops classifying it -- which is the guard doing
    # exactly its job. A reason that is true can still be covering for
    # something; the test that fires when an entry stops being needed
    # is what eventually surfaced it.
    # Both of these WERE the defect. They are override hatches now:
    # `kernel/workspace/broker.py` derives each from CONTROL_PANEL_URL
    # when unset, so the compose file names a local endpoint rather
    # than supplying the only copy of the fact. The guard below proves
    # the derivation, so leaving them here cannot hide a regression.
    "GIT_CREDENTIAL_URL":
        "an override. Derived from CONTROL_PANEL_URL when unset; E0 "
        "points at host.docker.internal instead.",
    "RESOURCE_OBJECT_URL":
        "an override, on the same terms as GIT_CREDENTIAL_URL.",
    "KERNEL_WORKSPACE_URL":
        "names 'the container holding the workspace volume', which "
        "exists on Docker and cannot on Kubernetes -- every execution "
        "has its own pod. The explorer routes around it entirely there "
        "(app/services/kernel_workspace_pod.py), so an unset value is "
        "the correct state for a deployed environment.",
    "KERNEL_CONSOLE_URL":
        "the same address by its other name; E0 serves the console and "
        "the workspace from one process.",
    "KERNEL_DATABASE_URL":
        "an override. Falls back to DATABASE_URL, which every "
        "deployment sets.",
    "ASSISTANT_MAX_ACTIVE_THREADS":
        "a tunable ceiling with a default in thread_ceiling.py. Absent "
        "means the default, which is the intended production value.",
    "FIREBASE_AUTH_EMULATOR_HOST":
        "points the SDK at a local emulator. A deployment that set it "
        "would be talking to an emulator that is not there.",
}


def _our_services_declare() -> set:
    """Env names the compose file sets on services WE build.

    NOT every name in the file. `socket-proxy` is a third-party image
    (`tecnativa/docker-socket-proxy`) configured with `CONTAINERS: 1`
    and `POST: 1`, and those are ordinary words that also appear as
    string constants in our code -- so scanning the whole file reported
    that the kernel depended on the socket proxy's configuration. A
    service with no `build:` is somebody else's program; its
    environment is not our deployment's to carry.
    """
    text = E0.read_text(encoding="utf-8")
    bounds = [(m.group(1), m.start()) for m in SERVICE.finditer(text)]
    names = set()
    if LOCAL_VALUES.exists():
        # The whole file is ours -- it exists to list the variables the
        # services need -- so there is no third-party block to skip.
        names |= set(re.findall(r"^\s*([A-Z][A-Z0-9_]{3,})\s*=",
                                LOCAL_VALUES.read_text(encoding="utf-8"),
                                re.M))
    for i, (_service, start) in enumerate(bounds):
        end = bounds[i + 1][1] if i + 1 < len(bounds) else len(text)
        block = text[start:end]
        if "build:" not in block:
            continue
        names |= set(DECLARED.findall(block))
    return names


def _code_reads() -> set:
    """Environment names OUR CODE references. By PARSE, not by text.

    BOTH kernel/ AND app/, because the class is not kernel-specific.
    Scanning only kernel/ is why this guard missed
    KERNEL_WORKSPACE_URL: the file explorer that depended on it is app
    code, so the name was invisible here while the panel it serves was
    empty on every Kubernetes run.

    Substring matching over kernel source claimed the kernel depended
    on `CONTAINERS` and `POST` -- the socket-proxy's own variables in
    the same compose file, whose names happen to be ordinary words in
    our code. A guard with false positives gets exceptions added to it
    until it stops guarding.

    Every string CONSTANT in kernel/, not only the ones inside an
    `os.environ[...]`. A name is routinely held in a variable before it
    is used -- `OPEN_ENV = "AGENTOS_ASSISTANT_API_OPEN"`, a vendor
    table mapping `"anthropic"` to `"ANTHROPIC_API_KEY"`, and
    `broker.endpoint(path, "GIT_CREDENTIAL_URL")` itself -- and a scan
    that followed only subscripts went blind on all three, including on
    the very fix this guard exists to protect.

    Constants, still not raw text: a name in a comment or a docstring
    is a mention, not a dependency, and counting those is how a guard
    acquires exceptions until it stops guarding.
    """
    names = set()
    paths = [p for area in ("kernel", "app")
             for p in (REPO / area).rglob("*.py")]
    for path in paths:
        if "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                              # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if isinstance(node.value, str) and NAME.match(node.value):
                names.add(node.value)
    return names


def _elsewhere_text() -> str:
    out = []
    for pattern in ELSEWHERE:
        for path in REPO.glob(pattern):
            if "/tests/" in path.as_posix() or "__pycache__" in path.as_posix():
                continue
            out.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


def _local_only() -> set:
    code, elsewhere = _code_reads(), _elsewhere_text()
    found = set()
    for name in _our_services_declare():
        if name not in code:
            continue                       # the kernel never asks for it
        if re.search(rf"^\s*-?\s*{name}\s*[:=]", elsewhere, re.M) or \
                re.search(rf"\b{name}=", elsewhere):
            continue                       # something deployed sets it too
        found.add(name)
    return found


def test_the_scan_still_finds_something():
    # A regex that quietly matches nothing reports every deployment
    # perfect -- which is the failure mode of the check it replaces.
    assert len(_our_services_declare()) >= 10
    # And it must not swallow the third party's, which is what made the
    # first version of this check unusable.
    assert "CONTAINERS" not in _our_services_declare()


def test_nothing_new_depends_on_the_developer_machine():
    surprises = _local_only() - set(LOCAL_ONLY)
    assert not surprises, (
        f"{sorted(surprises)}: kernel/ reads these and ONLY "
        f"infra/e0/docker-compose.yml sets them, so they are empty on "
        f"every deployed environment. This is how "
        f"`could not read Username for 'https://github.com'` happened. "
        f"Either derive the value from a fact the deployment already "
        f"has (see kernel/workspace/broker.py) or add it to LOCAL_ONLY "
        f"with the reason it is genuinely local.")


def test_the_reasons_are_still_true():
    # An entry that has since been set properly should leave, or the
    # list becomes a place names go to stop being checked.
    stale = set(LOCAL_ONLY) - _local_only()
    assert not stale, (
        f"{sorted(stale)} are no longer local-only; drop them from "
        f"LOCAL_ONLY so the list keeps meaning what it says")
