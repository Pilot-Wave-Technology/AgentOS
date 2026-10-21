"""The three workspace bindings: git, copyin, none.

Each implements acquire · checkpoint · publish. Nothing above the seam
knows which one is running — the profile names it and the registry
resolves it, the same way a profile names its tools.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile

from kernel.workspace import broker
from kernel.workspace.driver import CheckpointRef, WorkspaceError, WorkspaceSpec
from kernel.workspace.git_bearer import write_bearer

logger = logging.getLogger(__name__)


def _git_env() -> dict:
    """The environment every git subprocess here runs under.

    `GIT_TERMINAL_PROMPT=0` is the whole point. When the credential
    helper fails -- and the one below calls the control panel's broker,
    so it fails whenever that endpoint does -- it exits non-zero and
    prints NOTHING on stdout. Git reads that as "no credential offered"
    and falls back to asking a human, which in a container means:

        fatal: could not read Username for 'https://github.com'

    That sentence is the LAST line of the error and describes a terminal
    nobody expected to be there. The real cause is always the line above
    it, and with prompting off git names the credential helper as the
    failure instead of inventing a tty problem. Every instance of this
    so far has been the line above: a 404 from the broker, then an unset
    `GIT_CREDENTIAL_URL`, then `curl: not found`.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git(path: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True,
                            env=_git_env())
    if check and result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout.decode(errors="replace").strip()



def _credential_helper(spec) -> str:
    """A git credential helper that mints per-story tokens on demand.

    The control panel's broker (/internal/git-credential, the M5 design)
    answers the git credential protocol with a short-lived installation
    token scoped to THIS story's grant on THIS repo -- a read-only grant
    yields a read-only token, and nothing durable lands on disk except
    the helper command itself. Without this, `git clone <private url>`
    simply failed: the kernel had only ever cloned local corpus origins,
    and the first live GitHub story found the gap.

    Requires a broker URL (derived from CONTROL_PANEL_URL, or set
    explicitly), OPENCLAW_AUTH_TOKEN, a story to scope the token to, and
    a github.com https source. Missing any of them returns "" and the
    clone proceeds bare, which is right for local corpus origins -- and
    wrong, silently, for a private repository, which is why three of the
    four are now carried deliberately rather than assumed.

    THE BEARER IS A PATH, NEVER A VALUE. This command is persisted into
    `<workspace>/<mount>/.git/config`, which the sandbox mounts and reads
    as root -- so interpolating the token here published the shared
    gateway bearer to model-authored code, and that bearer opens the
    broker for any story. `kernel/workspace/git_bearer.py` puts it in a
    worker-private directory the sandbox has no mount for; only the path
    reaches the config file. Same pointer-not-value rule
    `kernel/credentials/issue.py` enforces for cloud credentials.
    """
    source = spec.source or ""
    if "github.com/" not in source or not source.startswith("https://"):
        return ""
    # Derived from CONTROL_PANEL_URL unless GIT_CREDENTIAL_URL is set
    # explicitly. Read `kernel/workspace/broker.py` before changing
    # this: the variable had one reader here and one writer in
    # `infra/e0/docker-compose.yml`, so every clone of a private repo
    # on Kubernetes ran unauthenticated.
    url = broker.endpoint(broker.GIT_CREDENTIAL, "GIT_CREDENTIAL_URL")
    # required=True: the source is a github https remote, so a clone
    # that proceeds without a credential is one that cannot push.
    token_file = write_bearer(required=True)
    if not url or not token_file or not spec.story_id:
        return ""
    repo = source.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    # The broker already answers in git's credential format; the helper
    # only has to fetch it. stdin (git's request) is ignored on purpose.
    #
    # WRAPPED IN A FUNCTION because git appends its operation ("get") as
    # an argument to the helper command. Bare curl treated that word as
    # a second URL and printed `error: 406` noise on every clone, fetch
    # and push -- harmless (the first URL had already answered) but
    # indistinguishable from a real credential failure in every log it
    # littered. The function swallows the argument.
    #
    # F5 -- RETRIED, because a transient 5xx here is SILENT. `curl -fsS`
    # prints nothing git can use, git proceeds unauthenticated, and
    # GitHub answers `Repository not found`: an auth failure wearing a
    # missing-repo message, which sends everyone to look at the wrong
    # thing. Observed twice on live runs (a 500 and a 502 on
    # `access_tokens`, both transient, neither retried).
    #
    # `--retry-all-errors` is what makes `--retry` cover a connection
    # reset as well as a 5xx; without it curl retries only the status
    # codes, and the reset is the case that actually happened.
    #
    # `t=$(cat FILE) || exit 1` rather than inlining the substitution in
    # the header: a missing file would otherwise send `Bearer ` and the
    # broker's 401 would surface, three layers later, as GitHub's
    # `Repository not found` -- the same auth-failure-wearing-a-missing-
    # repo-message this function's other comments were written about.
    # cat prints its own reason to stderr, which git shows.
    #
    # Built by concatenation, not an f-string: the shell function needs
    # literal braces, and doubling them around interpolated parts is how
    # a quoting bug gets into a credential path.
    return ("!f() { t=$(cat " + token_file + ") || exit 1; "
            "curl -fsS --retry 3 --retry-delay 1 --retry-all-errors "
            '--max-time 30 -H "Authorization: Bearer $t" '
            '"' + url + "?story_id=" + spec.story_id + "&repo=" + repo
            + '"; }; f')


def _clone_failure(stderr: str, source: str, had_helper: bool) -> str:
    """What actually went wrong, when GitHub will not say.

    F5 -- GITHUB ANSWERS `Repository not found` FOR BOTH "there is no
    such repository" and "you are not authenticated for it", because
    saying which would leak the existence of private repos. That is the
    right call for GitHub and a terrible message to hand a person: a
    transient 500 on the token broker sent someone looking for a missing
    repository that was there the whole time.

    We know something GitHub does not: whether a credential helper was
    configured for this clone. If one was, the repo is private, we were
    meant to be authenticated, and `not found` means the helper came back
    empty far more often than it means the repo is gone.
    """
    low = (stderr or "").lower()
    ambiguous = "not found" in low or "403" in low or "authentication" in low
    if not (had_helper and ambiguous):
        return f"clone failed: {stderr}"
    return (
        f"clone failed: {stderr}\n"
        f"  This repository is private and a credential helper WAS "
        f"configured for it, so `not found` here almost always means the "
        f"token broker did not answer -- not that {source} is missing. "
        f"Check the control panel's /internal/git-credential for a 5xx "
        f"at this time; the helper retries three times before giving up.")


def _ensure_bearer_for(spec: WorkspaceSpec) -> None:
    """Write the broker bearer when this spec would need one to clone.

    The same condition `_credential_helper` applies, without building a
    helper string: an adopted workspace already carries the helper in its
    `.git/config`, and what it is missing is the file that helper reads.
    """
    source = spec.source or ""
    if "github.com/" not in source or not source.startswith("https://"):
        return
    write_bearer(required=True)


class GitDriver:
    """Coding profile: clone, commit, push."""

    name = "git"

    def acquire(self, spec: WorkspaceSpec) -> str:
        path = spec.root
        if os.path.isdir(os.path.join(path, ".git")):
            # Already there — adopt it. This is the rebuild path after a
            # lost sandbox, and re-cloning over live work would discard
            # exactly the uncommitted edits a resume depends on.
            logger.info("git workspace already present at %s; adopting", path)
            # THE BEARER IS PER-POD, NOT PER-CLONE, AND THIS PATH FORGOT IT.
            #
            # `write_bearer` was only ever reached through
            # `_credential_helper`, which only the clone branch below
            # calls. An adopted workspace therefore came up with the
            # helper already persisted in `.git/config` -- pointing at a
            # bearer file that this pod never wrote.
            #
            # Everything authenticated then fails, including the
            # platform's own worker-side publish, and it fails at the
            # END: execution 407 (next, 2026-10-06) did all its work,
            # passed every check, and only then discovered it could
            # never deliver. Eleven generations of park/resume followed,
            # with the model hunting the filesystem for a credential.
            #
            # Idempotent and cheap -- `write_bearer` returns early on a
            # module global -- so it costs an adopted workspace nothing.
            _ensure_bearer_for(spec)
            return path

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if spec.source:
            clone_cmd = ["git"]
            helper = _credential_helper(spec)
            if helper:
                clone_cmd += ["-c", f"credential.helper={helper}"]
            # FULL DEPTH, AND THAT IS A DECISION. A shallow clone is
            # tempting -- a story works on a branch tip and the ancestry
            # can be enormous (execution 222 read a 95 KB deletion patch
            # out of a history whose tip is two files). It is refused
            # because of what a workspace has to be able to DO.
            #
            # The trunk moves under an open branch, and taking those
            # changes is an ordinary coding story that runs `sync_trunk`
            # here: fetch the lane, merge it in. A merge needs the merge
            # BASE, and `--depth 1` has no common ancestor at all while
            # `--shallow-exclude=<trunk>` excludes precisely the branch
            # point the merge needs. `op_changed` reads the same base to
            # show the assessor a story's own work.
            #
            # So the size problem is answered where it belongs: the 32 KB
            # output bound (`harness/loop_tool`) and the prompt telling
            # the executor to ask history for a summary. If clone COST
            # ever becomes the complaint, `--filter=blob:none` is the
            # lever -- it keeps the whole commit graph, so merge-base
            # still resolves, and fetches blobs on demand.
            result = subprocess.run(
                clone_cmd + ["clone", spec.source, path],
                capture_output=True, env=_git_env())
            if result.returncode != 0:
                raise WorkspaceError(_clone_failure(
                    result.stderr.decode(errors="replace").strip(),
                    spec.source, bool(helper)))
            if spec.ref:
                _git(path, "checkout", spec.ref)
            # A FALLBACK IDENTITY, on the clone as well as on `init`.
            # Only the init branch set one, so a cloned workspace could
            # never be checkpointed by the driver -- `git commit` refused
            # with "Author identity unknown" and the mount looked broken.
            # This is a floor, not the attribution: the real author and
            # co-authors arrive per-commit through `identity.env()` in
            # story_commit, which overrides these.
            _git(path, "config", "user.email", "executor@agentos.local")
            _git(path, "config", "user.name", "AgentOS Executor")
            # TWO UIDS SHARE THIS REPOSITORY, so git must create its
            # objects group-writable. The worker is 65531 and the
            # sandbox is 65532, in one group, and `loop_progress`
            # already spells out why: "a file the worker creates under
            # the default 0022 umask is 0644, owner 65531:
            # group-READABLE and not group-writable".
            #
            # `podinit.compose` sets umask 0o002 for the clone, which
            # covers provisioning and nothing after it. A later
            # worker-side `story_commit` runs under the default umask
            # and creates `.git/objects/<xx>/` at 0755 -- owned by
            # 65531, and shut to the sandbox that has to add the next
            # object to it.
            #
            # Measured on run 264. The executor spent five steps doing
            # surgery inside git's own object store to get its commit
            # out:
            #
            #   cd .git/objects && cp -r 56 56.bak && chmod -R u+w 56.bak
            #   && rm -rf 56 && mv 56.old ... && mkdir -m 775 56
            #
            # It worked. It is also a model repairing the platform's
            # permission model by hand, differently each time -- run 262
            # did a cruder `chmod -R g+w .git`.
            #
            # `core.sharedRepository=group` is GIT'S OWN ANSWER to this
            # and belongs here rather than a umask: a umask is a
            # property of whichever process happens to run, and this is
            # a property of the repository. It survives every later
            # command in either container.
            _git(path, "config", "core.sharedRepository", "group")
            if helper:
                # Persisted, so later fetch/push in this workspace mints a
                # fresh scoped token per operation instead of carrying one.
                _git(path, "config", "credential.helper", helper)
        else:
            os.makedirs(path, exist_ok=True)
            _git(path, "init")
            _git(path, "config", "user.email", "executor@agentos.local")
            _git(path, "config", "user.name", "AgentOS Executor")
            # The init branch needs it for the same reason; an `init`
            # workspace is written by both containers too.
            _git(path, "config", "core.sharedRepository", "group")
        return path

    def checkpoint(self, path: str, message: str) -> CheckpointRef:
        _git(path, "add", "-A")
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                cwd=path, capture_output=True)
        if staged.returncode == 0:
            # Not an error: nothing changed since the last checkpoint.
            return CheckpointRef(kind="git", ref="", detail="nothing to commit")
        _git(path, "commit", "-m", message)
        return CheckpointRef(kind="git", ref=_git(path, "rev-parse", "HEAD"))

    def publish(self, path: str) -> CheckpointRef:
        remotes = _git(path, "remote", check=False)
        if not remotes:
            return CheckpointRef(kind="git", ref="", detail="no remote")
        _git(path, "push")
        return CheckpointRef(kind="git", ref=_git(path, "rev-parse", "HEAD"),
                             detail="pushed")


# ── the object fetcher, injected by the host ─────────────────────────
#
# Uploaded documents live in the control panel's object store. The
# kernel never imports the control panel, so whoever hosts these
# drivers registers a fetcher at startup and E0 registers none -- and
# then says so, rather than silently building a workspace with the
# document missing.
_object_fetcher = None    # (uri) -> bytes


def set_object_fetcher(fn) -> None:
    """Register how to read `gs://...` (or any URI) as bytes."""
    global _object_fetcher
    _object_fetcher = fn


# ── and the substrate's own, for a host that is not there ────────────
#
# I3. A Kubernetes execution pod composes its workspace in an
# initContainer running pure kernel code: `app/` is not in that image,
# so nothing calls `set_object_fetcher` and every `gs://` document
# refused. `kernel/workspace/object_fetch.py` reads the bucket on the
# pod's own Workload Identity and registers itself here.
_kernel_object_fetcher = None    # (uri) -> bytes | None


def set_kernel_object_fetcher(fn) -> None:
    """Register a substrate-level fetcher. LOWER PRECEDENCE THAN THE HOST.

    PRECEDENCE, and it is deliberate: host first, this second, HTTP to
    the control panel last. The host's fetcher holds credentials and
    grant policy the kernel does not -- `app.storage.download_resource_
    uri` runs as the control panel, which is what authorises the read
    against the story's grant. A kernel fetcher that took precedence
    would quietly move every cloud document read off that path and onto
    a bucket-wide identity, which is a widening nobody asked for.

    So this fires only where no host registered one, which is exactly
    the pod. A fetcher registered here returns None for a URI it does
    not own, and the chain continues -- see `_fetch_object`.
    """
    global _kernel_object_fetcher
    _kernel_object_fetcher = fn


def _is_uri(source: str) -> bool:
    return "://" in (source or "")


def _fetch_over_http(spec) -> bytes:
    """Ask the control panel for a granted document's bytes.

    THE SAME SHAPE AS THE GIT CREDENTIAL HELPER above, and for the same
    reason. The object lives in the control panel's store; this process
    has no cloud credentials and a standing rule that it keeps none. So
    it asks, scoped to (story, resource) -- the grant is the
    authorisation, and a bare URI would make the endpoint an open proxy
    for the whole bucket.
    """
    import urllib.request

    # `in os.environ`, not `.get(..., "")`. B1's guard bans a default
    # argument here and F7 runs it over kernel/ -- because a silent
    # default turns "this deployment is misconfigured" into "the
    # feature quietly does nothing". The function four lines above this
    # one already reads its two variables exactly this way; I wrote the
    # convenient form instead of the one beside me.
    url = broker.endpoint(broker.RESOURCE_OBJECT, "RESOURCE_OBJECT_URL")
    token = (os.environ["OPENCLAW_AUTH_TOKEN"]
             if "OPENCLAW_AUTH_TOKEN" in os.environ else "")
    if not url or not token:
        raise WorkspaceError(
            "RESOURCE_OBJECT_URL and OPENCLAW_AUTH_TOKEN are needed to "
            "place a granted document in the workspace, and one is "
            "missing")
    if not spec.story_id or not spec.resource_id:
        raise WorkspaceError(
            "a document mount needs both a story and a resource id to be "
            "fetched; the grant is what authorises the read")
    req = urllib.request.Request(
        f"{url}?story_id={spec.story_id}&resource_id={spec.resource_id}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _fetch_object(spec) -> bytes:
    """The document's bytes, however this host can get them.

    THREE LINKS, IN THIS ORDER, and the order is the point:

      1. the HOST's injected fetcher -- the control panel runs the
         kernel in-process in cloud, can read the store directly, and
         holds the grant policy that authorises the read. It wins
         wherever it exists.
      2. the SUBSTRATE's, registered by a pod entrypoint on the pod's
         own identity. Only reached when (1) is absent, which is the
         initContainer case: no `app/` in that image, nothing to inject.
         It returns None for a URI it does not own -- an `artifact://`
         document keeps resolving the way it already does -- and the
         chain falls through.
      3. HTTP to the control panel, which is the E0 case and the one
         that matters locally.
    """
    uri = spec.source
    if _object_fetcher is not None:
        # THE HOST'S ANSWER IS FINAL, including a bad one: it is the
        # link that holds the grant, so falling through from it would
        # retry an authorised read as an unauthorised one.
        blob = _object_fetcher(uri)
    elif _kernel_object_fetcher is not None:
        blob = _kernel_object_fetcher(uri)
        if blob is None:
            blob = _fetch_over_http(spec)
    else:
        blob = _fetch_over_http(spec)
    if not blob:
        # WHAT THE EARLIER LINKS SAID. Imported here, not at module
        # scope: `object_fetch` registers INTO this module, so a
        # top-level import is a cycle.
        because = ""
        try:
            from kernel.workspace import object_fetch  # noqa: PLC0415

            because = object_fetch.why_declined(uri)
        except Exception:                               # noqa: BLE001
            because = ""
        raise WorkspaceError(
            f"{uri!r} fetched empty. The story is written against this "
            f"document; a workspace without it produces work planned on "
            f"a guess."
            + (f" The last link to try it reported: {because}"
               if because else ""))
    return blob


class CopyinDriver:
    """Business profile: a folder from a drive or object store.

    No repository, so `checkpoint` is an archive into the artifact store.
    That is the whole reason this binding exists: without it a business
    story has no durable progress marker at all, and its work survives only
    as long as the directory does.
    """

    name = "copyin"

    def acquire(self, spec: WorkspaceSpec) -> str:
        path = spec.root
        if os.path.isdir(path) and os.listdir(path):
            logger.info("copyin workspace already populated at %s; adopting", path)
            return path

        # A URI SOURCE IS ONE FILE, FETCHED. An uploaded document lives
        # in object storage, not on this disk, and it is a single file
        # rather than a tree -- so `docs/<name>` is that file, not a
        # directory containing it.
        #
        # The fetcher is INJECTED by the host, the same seam
        # `kernel.go.read` uses for exactly the same reason: the kernel
        # must not import the control panel, and object-store
        # credentials are the host's. With no fetcher this refuses in
        # words, because a workspace that quietly lacks the document the
        # story is written against produces a story planned on guesses.
        #
        # I3 -- WHERE THERE IS NO HOST AT ALL, a pod entrypoint registers
        # `kernel/workspace/object_fetch.py` instead, which reads the
        # bucket on the pod's own identity. Second in precedence, never
        # first: see set_kernel_object_fetcher.
        if spec.source and _is_uri(spec.source):
            blob = _fetch_object(spec)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(blob)
            logger.info("copyin fetched %s -> %s (%d bytes)",
                        spec.source, path, len(blob))
            return path

        os.makedirs(path, exist_ok=True)
        if spec.source:
            if not os.path.isdir(spec.source):
                raise WorkspaceError(f"source is not a directory: {spec.source!r}")
            for entry in os.listdir(spec.source):
                src = os.path.join(spec.source, entry)
                dst = os.path.join(path, entry)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
        return path

    def checkpoint(self, path: str, message: str) -> CheckpointRef:
        from kernel.artifacts.store import get_store

        if not os.path.isdir(path):
            raise WorkspaceError(f"workspace missing: {path!r}")
        fd, tmp = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        try:
            with tarfile.open(tmp, "w:gz") as tar:
                tar.add(path, arcname=".")
            with open(tmp, "rb") as fh:
                artifact_id = get_store().put(fh.read())
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return CheckpointRef(kind="archive", ref=artifact_id, detail=message)

    def publish(self, path: str) -> CheckpointRef:
        # Copy-back to the origin belongs with the credentials that reach
        # it, which E0 does not have. Checkpointing already made the work
        # durable, so this is honest rather than pretending.
        return CheckpointRef(kind="archive", ref="",
                             detail="copy-back needs Stage H credentials")


class NoneDriver:
    """Errand profile: no workspace of its own."""

    name = "none"

    def acquire(self, spec: WorkspaceSpec) -> str:
        return spec.root or ""

    def checkpoint(self, path: str, message: str) -> CheckpointRef:
        return CheckpointRef(kind="none", ref="", detail="profile has no workspace")

    def publish(self, path: str) -> CheckpointRef:
        return CheckpointRef(kind="none", ref="", detail="profile has no workspace")


_BINDINGS = {"git": GitDriver, "copyin": CopyinDriver, "none": NoneDriver}


def driver_for(name: str):
    """Resolve a profile's workspace_driver. Fails closed on an unknown one."""
    try:
        return _BINDINGS[name]()
    except KeyError:
        raise WorkspaceError(
            f"unknown workspace driver {name!r}; known: "
            f"{', '.join(sorted(_BINDINGS))}"
        ) from None


def create_child_worktree(parent_path: str, child_id: str) -> str:
    """E7c `build`: give a child its own worktree inside the parent's repo.

    A worktree rather than a copy: it shares the object store, so creating
    one is cheap and merging back is a normal git operation rather than a
    directory diff.

    Placed under the parent's path so attenuation's is_path_inside check
    holds — a child whose scope sits outside its parent's is a widening,
    and E7b refuses it.
    """
    if not os.path.isdir(os.path.join(parent_path, ".git")):
        raise WorkspaceError(
            f"{parent_path!r} is not a git workspace; the build flavour "
            "needs one to branch from")
    wt_root = os.path.join(parent_path, ".worktrees")
    os.makedirs(wt_root, exist_ok=True)
    path = os.path.join(wt_root, child_id)
    if os.path.isdir(path):
        return path
    branch = f"subagent/{child_id}"
    _git(parent_path, "worktree", "add", "-b", branch, path)
    return path


def merge_child_worktree(parent_path: str, child_path: str,
                         child_id: str) -> CheckpointRef:
    """Merge a child's branch back, then remove its worktree.

    The parent merges; the child cannot commit and cannot push. A child
    that could merge its own work would be finishing the story, which is
    not its errand.
    """
    branch = f"subagent/{child_id}"
    # Commit whatever the child left, on the child's branch — it had no
    # story_commit, so its work is uncommitted by design.
    _git(child_path, "add", "-A")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            cwd=child_path, capture_output=True)
    if staged.returncode == 0:
        _cleanup_worktree(parent_path, child_path, branch)
        return CheckpointRef(kind="git", ref="", detail="child changed nothing")
    _git(child_path, "commit", "-m", f"sub-agent {child_id}")

    merge = subprocess.run(["git", "merge", "--no-ff", "-m",
                            f"merge sub-agent {child_id}", branch],
                           cwd=parent_path, capture_output=True)
    if merge.returncode != 0:
        # Leave the branch in place: a conflict is the parent's to resolve,
        # and deleting the child's work to tidy up would lose it.
        detail = merge.stdout.decode(errors="replace").strip()[:300]
        subprocess.run(["git", "merge", "--abort"], cwd=parent_path,
                       capture_output=True)
        return CheckpointRef(kind="git", ref="",
                             detail=f"conflict, branch {branch} kept: {detail}")

    sha = _git(parent_path, "rev-parse", "HEAD")
    _cleanup_worktree(parent_path, child_path, branch)
    return CheckpointRef(kind="git", ref=sha, detail=f"merged {branch}")


def _cleanup_worktree(parent_path: str, child_path: str, branch: str) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", child_path],
                   cwd=parent_path, capture_output=True)
    subprocess.run(["git", "branch", "-D", branch],
                   cwd=parent_path, capture_output=True)
