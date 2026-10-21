"""Ec4d — the whole credential road, walked without a cloud.

    granted_identity      the grant is the boundary
      -> provider.mint    the WORKER mints, never the sandbox
        -> write_lease    a file under the workspace, not an env value
          -> pointer      AGENTOS_CREDENTIALS, safe to print
            -> release    the file goes when the run ends

Everything except the mint is ordinary software that can be wrong
without a cloud to be wrong in. The stub provider exists so this road is
walked on a laptop before the system holds anything worth stealing.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

asyncpg = pytest.importorskip("asyncpg")
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
SA = "cf-invoker@project-a.iam.gserviceaccount.com"


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
    pytest.skip("no local database")


def _run(body):
    async def _go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            with tempfile.TemporaryDirectory() as ws:
                await body(conn, ws)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(_go())


async def _granted(conn, with_identity=True):
    tag = os.urandom(4).hex()
    goal, story = f"zz-ci-{tag}", f"zzs{tag[:6]}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'t','in_progress','alice')", story, goal)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, state) "
        "VALUES ('story','coding',$1,$2,'RUNNING') RETURNING id", goal, story)
    if with_identity:
        for rid, kind, meta in (
                ("env_a", "gcp_environment",
                 {"target_project_id": "project-a", "target_region": "us-central1"}),
                ("sa_a", "gcp_test_sa", {"sa_email": SA})):
            await conn.execute(
                """INSERT INTO goal_resources (id, goal_id, kind, name,
                                               metadata, created_by, status)
                   VALUES ($1,$2,$3,$1,$4::jsonb,'alice','active')""",
                rid, goal, kind, json.dumps(meta))
            await conn.execute(
                "INSERT INTO story_resources (story_id, resource_id, relation) "
                "VALUES ($1,$2,'needs')", story, rid)
    return eid


def test_a_run_with_no_grant_gets_nothing_quietly():
    """The ordinary case, and it must not raise. Almost no run needs
    cloud reach."""
    async def body(conn, ws):
        from kernel.credentials.issue import issue

        eid = await _granted(conn, with_identity=False)
        assert await issue(conn, eid, ws, "stub") is None
        assert not os.path.exists(os.path.join(ws, ".agentos"))
    _run(body)


def test_the_lease_is_written_and_the_token_is_not_returned():
    """What comes back is safe to log. The secret is in the file."""
    async def body(conn, ws):
        from kernel.credentials.issue import issue
        from kernel.workspace.credentials import (holds_secret,
                                          read_lease)

        eid = await _granted(conn)
        out = await issue(conn, eid, ws, "stub")
        assert out["account"] == SA
        assert out["scope"] == "project-a"
        assert out["region"] == "us-central1"
        assert out["granted_via"] == "sa_a"
        # THE POINT: no token VALUE in the return. Checked against the
        # secret itself, not the word "token" -- the pointer is legitimately
        # called CLOUDSDK_AUTH_ACCESS_TOKEN_FILE and names a `.token` path,
        # and a substring check flagged those as a leak. A guard that fires
        # on the safe thing gets loosened until it stops firing on the
        # dangerous one.
        lease = read_lease(ws, out["lease_id"])
        secret = lease["access_token"]
        assert secret and secret not in json.dumps(out), (
            "the minted token itself came back in the return value")
        assert not holds_secret(json.dumps(out))

        assert lease["access_token"].startswith("ya29.")
        # Provenance in the file, so an orphan answers "whose is this".
        assert lease["agentos_account"] == SA
        assert lease["agentos_execution_id"] == eid
        assert lease["agentos_granted_via"] == "sa_a"
    _run(body)


def test_the_lease_file_is_not_world_readable():
    """It holds a live credential, and the workspace is shared: the
    sandbox runs as its own uid and the Verifier as `nobody`.

    POSIX ONLY, because Windows does not carry POSIX modes and
    `write_lease` says so in an honest fallback rather than pretending.
    On a Windows host the mode is asserted in the CODE below instead, and
    the real check runs in the Linux container where the worker actually
    writes.
    """
    async def body(conn, ws):
        import stat

        from kernel.credentials.issue import issue
        from kernel.workspace.credentials import lease_path

        eid = await _granted(conn)
        out = await issue(conn, eid, ws, "stub")
        if os.name != "posix":
            pytest.skip("POSIX modes are not carried on this host")
        mode = os.stat(lease_path(ws, out["lease_id"])).st_mode
        assert not mode & stat.S_IROTH, "the lease is world-readable"
        assert not mode & stat.S_IWOTH
    _run(body)


def test_write_lease_restricts_the_file_and_its_directory():
    """The static half of the test above, so a Windows host still fails
    if the chmod is ever dropped."""
    import io as _io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = _io.open(root / "kernel/workspace/credentials.py",
                   encoding="utf-8").read()
    body = src[src.index("def write_lease"):]
    body = body[:body.index("\ndef ", 10)]
    assert "os.chmod(directory, stat.S_IRWXU)" in body, (
        "the lease directory is not restricted to its owner")
    assert "stat.S_IRUSR | stat.S_IWUSR" in body, (
        "the lease file is not restricted to owner read/write")
    # And a host that cannot apply them must say so rather than pass
    # silently: the permission is not the control it looks like there.
    assert body.count("logger.warning") >= 2


def test_release_deletes_the_lease():
    async def body(conn, ws):
        from kernel.credentials.issue import issue, release
        from kernel.workspace.credentials import lease_path, read_lease

        eid = await _granted(conn)
        out = await issue(conn, eid, ws, "stub")
        path = lease_path(ws, out["lease_id"])
        assert os.path.exists(path)
        assert await release(ws, eid, "stub") is True
        assert not os.path.exists(path)
        assert read_lease(ws, out["lease_id"]) is None
        # Releasing twice is not an error: cleanup runs from more than
        # one place and must be idempotent.
        assert await release(ws, eid, "stub") is False
    _run(body)


def test_a_cross_project_grant_raises_rather_than_minting():
    """The authorisation refusal must survive the whole road, not be
    swallowed into "no credential" somewhere along it."""
    async def body(conn, ws):
        from kernel.credentials.authorize import NotAuthorised
        from kernel.credentials.issue import issue

        eid = await _granted(conn)
        await conn.execute(
            "UPDATE goal_resources SET metadata = "
            "jsonb_set(metadata,'{target_project_id}','\"other-project\"') "
            "WHERE id = 'env_a'")
        with pytest.raises(NotAuthorised):
            await issue(conn, eid, ws, "stub")
        assert not os.path.exists(os.path.join(ws, ".agentos", "credentials"))
    _run(body)


# ── the shape of what the sandbox is told ────────────────────────────────

def test_the_sandbox_is_given_a_posix_path():
    """The worker may run on Windows; the sandbox is Linux whatever the
    worker runs on. This built the pointer with `os.path.join` and handed
    the container a path with backslashes -- one filename, not a
    directory, read as a missing credential rather than a broken one."""
    from kernel.workspace.credentials import sandbox_env

    got = sandbox_env()["AGENTOS_CREDENTIALS"]
    assert got == "/workspace/.agentos/credentials", got
    assert "\\" not in got
    assert sandbox_env("/srv/ws/")["AGENTOS_CREDENTIALS"] == \
        "/srv/ws/.agentos/credentials"


def test_the_pointer_is_the_only_credential_name_the_sandbox_carries():
    """`_safe_env` is an allowlist precisely so a leaked variable is not
    forwarded. Adding a secret to it would make the allowlist the leak."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/bridge/tools_core.py", encoding="utf-8").read()
    block = src[src.index("def _safe_env"):]
    block = block[:block.index("\n\n\n")] if "\n\n\n" in block else block
    assert "AGENTOS_CREDENTIALS" in block
    for forbidden in ("ACCESS_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                      "_SECRET", "_PASSWORD"):
        assert forbidden not in block, f"{forbidden} is in the allowlist"


def test_a_stub_token_cannot_be_written_by_a_real_provider():
    """Cheaper to catch here than to debug an authentication failure
    against a live API that reads like a misconfigured account."""
    from kernel.credentials.stub import is_stub_token

    assert is_stub_token("ya29.STUB-NOT-A-REAL-CREDENTIAL-abc")
    assert not is_stub_token("ya29.a0ARrdaM-real-looking")


def test_the_stub_token_is_shaped_so_the_leak_guards_see_it():
    """A stub token that looked like `fake-token` would sail past
    `holds_secret`, and the guards would be proven by nothing."""
    async def body(conn, ws):
        from kernel.credentials.issue import issue
        from kernel.workspace.credentials import holds_secret, read_lease

        eid = await _granted(conn)
        out = await issue(conn, eid, ws, "stub")
        token = read_lease(ws, out["lease_id"])["access_token"]
        assert holds_secret(token), (
            "the stub token does not match the scrubber, so every leak "
            "guard test using it proves nothing")
    _run(body)


# ── the sweep, which is the second line behind the TTL ───────────────────

def test_the_sweep_releases_finished_runs_and_spares_live_ones():
    """An execution reaches a terminal state in five different places,
    and releasing in each is one fact written five times -- four stay
    right and the fifth leaks. The sweep also covers what no exit path
    can: a worker killed mid-run never reaches any of its exits, which is
    exactly when a credential is left with nobody tracking it."""
    async def body(conn, ws):
        from kernel.credentials.issue import lease_id_for
        from kernel.credentials.sweep import sweep
        from kernel.workspace.credentials import lease_path, write_lease

        gid = "zz-sw-" + os.urandom(3).hex()
        await conn.execute(
            "INSERT INTO goals (id,name,created_by) VALUES ($1,'t','a')", gid)
        done = await conn.fetchval(
            "INSERT INTO kernel.executions (kind,domain,goal_id,state,"
            "workspace_path) VALUES ('story','coding',$1,'DONE',$2) "
            "RETURNING id", gid, ws)
        live = await conn.fetchval(
            "INSERT INTO kernel.executions (kind,domain,goal_id,state,"
            "workspace_path) VALUES ('story','coding',$1,'RUNNING',$2) "
            "RETURNING id", gid, ws)
        for eid in (done, live):
            write_lease(ws, lease_id_for(eid), {"access_token": "ya29.STUB-x"})

        out = await sweep(conn)
        assert out["released"] >= 1
        assert not os.path.exists(lease_path(ws, lease_id_for(done)))
        # THE HALF THAT MATTERS MOST. A sweep that took a live run's
        # credential would break the run it was protecting.
        assert os.path.exists(lease_path(ws, lease_id_for(live)))
        # Idempotent: cleanup runs from more than one place.
        assert (await sweep(conn))["released"] == 0
    _run(body)


def test_the_provider_default_is_the_stub():
    """A deployment that has not asked for real cloud credentials must
    not get them by accident. The stub fails loudly against a real API
    rather than quietly succeeding at something."""
    import os as _os

    from kernel.worker.runner import _credential_provider

    was = _os.environ.pop("AGENTOS_CREDENTIAL_PROVIDER", None)
    try:
        assert _credential_provider() == "stub"
        _os.environ["AGENTOS_CREDENTIAL_PROVIDER"] = "gcp"
        assert _credential_provider() == "gcp"
        _os.environ["AGENTOS_CREDENTIAL_PROVIDER"] = "   "
        assert _credential_provider() == "stub"
    finally:
        _os.environ.pop("AGENTOS_CREDENTIAL_PROVIDER", None)
        if was is not None:
            _os.environ["AGENTOS_CREDENTIAL_PROVIDER"] = was


def test_the_sandbox_pointer_passes_the_env_guard():
    """`env_guard` refuses anything credential-shaped in the executor
    env. The pointer must pass it -- if it did not, the fix would be to
    weaken the guard, which is how the allowlist becomes the leak."""
    from kernel.conductor.env_guard import assert_no_credentials
    from kernel.conductor.sandbox_spec import _credentials_pointer

    env = {"BRIDGE_PORT": "9100", "WORKSPACE": "/workspace",
           **_credentials_pointer("/workspace")}
    assert_no_credentials(env)
    assert env["AGENTOS_CREDENTIALS"] == "/workspace/.agentos/credentials"


def test_the_sdk_variables_reach_the_sandbox():
    """F6 — THIS TEST WAS GREEN FOR THE WRONG REASON.

    It asserted `_credentials_pointer`, the function that IS called,
    while the one that sets GOOGLE_APPLICATION_CREDENTIALS --
    `credential_env` -- was defined and called by nothing. So a run held
    a perfectly good lease that no SDK could find, and the executor
    prompt promised "the cloud SDKs are already pointed at it" while
    they were not.

    Found by errand 74262e1f reading its own environment. Asserted here
    on the variable a REAL SDK reads, not the one we happen to set."""
    import inspect

    from kernel.conductor import sandbox_spec

    src = inspect.getsource(sandbox_spec.build_sandbox_spec)
    assert "credential_pointers" in src, (
        "build_sandbox_spec ignores the SDK pointers, so a lease is "
        "unreachable by gcloud/boto/az")

    # And the provider-declared names are the ones tooling reads.
    from kernel.credentials.provider import provider_for

    lease = "/workspace/.agentos/credentials/exec-1-cloud.json"
    for cloud, variable in (("aws", "AWS_SHARED_CREDENTIALS_FILE"),
                            ("azure", "AZURE_ACCESS_TOKEN_FILE")):
        assert provider_for(cloud).sandbox_pointers(lease)[variable] == lease

    # GCP IS THE EXCEPTION, AND IT IS THE WHOLE LESSON. It used to point
    # GOOGLE_APPLICATION_CREDENTIALS at the lease, and google-auth
    # REFUSES that file -- `authorized_user` requires client_id,
    # client_secret and refresh_token, so `google.auth.default()` raised
    # DefaultCredentialsError and the run reported no credentials while
    # holding a good token. Pointing the variable at an unparseable file
    # is worse than leaving it unset: a library that would have fallen
    # back instead fails hard.
    #
    # gcloud reads a file containing the raw token and nothing else.
    gcp = provider_for("gcp").sandbox_pointers(lease)
    assert gcp == {"CLOUDSDK_AUTH_ACCESS_TOKEN_FILE":
                   "/workspace/.agentos/credentials/exec-1-cloud.token"}
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in gcp, (
        "pointing google-auth at the record makes it fail hard")


def test_pointers_come_from_the_grant_not_the_transport():
    """`sidecar` mints for whichever cloud the grant names; asking IT
    which variables to set would name the wrong ones."""
    import inspect

    from kernel.credentials import issue

    src = inspect.getsource(issue.pointers_for)
    assert "granted_identity" in src
    assert 'identity["cloud"]' in src
    # And a run with no grant must get nothing rather than a default.
    assert "return {}" in src


def test_the_guard_drops_a_value_but_keeps_a_selector():
    """NEARLY A SELF-INFLICTED OUTAGE. The first version of this guard
    required every pointer to be a path, which drops `AWS_PROFILE=default`
    -- a section name inside the INI file, not a file -- leaving boto
    reading an unnamed profile. A guard that breaks the thing it protects.

    So: refuse anything credential-SHAPED anywhere, and require a path
    only of variables that NAME a file."""
    from unittest.mock import patch

    from kernel.credentials import issue

    class Offering:
        def __init__(self, d):
            self.d = d

        def sandbox_pointers(self, lease_file):
            return self.d

    lease = "/workspace/.agentos/credentials/exec-7-cloud.json"

    # A real AWS offering survives intact -- profile included.
    with patch.object(issue, "provider_for", lambda c: Offering(
            {"AWS_SHARED_CREDENTIALS_FILE": lease, "AWS_PROFILE": "default"})):
        out = issue.sdk_pointers("aws", 7)
    assert out == {"AWS_SHARED_CREDENTIALS_FILE": lease,
                   "AWS_PROFILE": "default"}

    # A file variable holding something that is not a path is dropped.
    with patch.object(issue, "provider_for", lambda c: Offering(
            {"AWS_SHARED_CREDENTIALS_FILE": "AKIAIOSFODNN7EXAMPLE"})):
        assert issue.sdk_pointers("aws", 7) == {}

    # And a secret is dropped whatever it is called.
    with patch.object(issue, "provider_for", lambda c: Offering(
            {"AWS_SESSION_TOKEN": "ya29.a0ARrdaM-EXAMPLE-0123456789abcdef"})):
        assert issue.sdk_pointers("aws", 7) == {}


def test_only_one_function_computes_pointers():
    """This was briefly THREE -- `issue` for the lease report,
    `pointers_for` for the sandbox env, and `credential_env` for nobody
    -- and only the dead one carried the guard above. N declarations of
    one fact, with the check living in the copy that never ran."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    spec = io.open(root / "kernel/conductor/sandbox_spec.py",
                   encoding="utf-8").read()
    assert "def credential_env" not in spec, (
        "the dead pointer-builder is back in sandbox_spec")

    src = io.open(root / "kernel/credentials/issue.py", encoding="utf-8").read()
    assert src.count(".sandbox_pointers(") == 1, (
        "a second place resolves provider pointers; the guard only "
        "protects the one that calls sdk_pointers")
    # Both live callers go through it.
    assert "pointers = sdk_pointers(" in src
    assert "return sdk_pointers(" in src


def test_the_shell_allowlist_carries_every_sdk_pointer():
    """F6, SECOND HALF — THE POINTER HAS TO SURVIVE THE SHELL.

    `build_sandbox_spec` sets the SDK variables on the sandbox container,
    which is necessary and was not sufficient: every shell the executor
    runs goes through `_safe_env` in `kernel/bridge/tools_core.py`, an
    ALLOWLIST, and it had never heard of GOOGLE_APPLICATION_CREDENTIALS.
    So the run held a valid lease, correct IAM and a correct container
    environment, and `gcloud` reported no credentials — which sends
    everybody to read the IAM policy, where nothing is wrong.

    The allowlist is RESTATED there rather than imported, because that
    module runs inside the sandbox where `kernel.credentials` is not
    shipped. This is the guard that stops the two drifting: every name
    any provider emits must be on it."""
    import io as _io
    import pathlib
    import re

    from kernel.credentials.provider import provider_for

    emitted = set()
    for cloud in ("gcp", "aws", "azure", "stub"):
        emitted |= set(provider_for(cloud).sandbox_pointers(
            "/workspace/.agentos/credentials/exec-1-cloud.json"))

    root = pathlib.Path(__file__).resolve().parents[3]
    src = _io.open(root / "kernel/bridge/tools_core.py",
                   encoding="utf-8").read()
    i = src.index("SDK_POINTER_VARS = (")
    allowed = set(re.findall(r'"([A-Z_]+)"', src[i:src.index(")", i)]))

    missing = sorted(emitted - allowed)
    assert not missing, (
        f"a provider emits {missing} and the sandbox shell allowlist "
        f"drops them, so the SDK will report no credentials")

    # And the allowlist must actually be consulted by _safe_env.
    body = src[src.index("def _safe_env"):src.index("env.setdefault")]
    assert "SDK_POINTER_VARS" in body, (
        "SDK_POINTER_VARS is declared and _safe_env ignores it — the "
        "same shape as `scrub` sitting unwired")


def test_the_sandbox_never_receives_the_workers_own_key():
    """THE HOLE THAT OPENS WHEN YOU ALLOWLIST A CREDENTIAL NAME.

    The worker now sets GOOGLE_APPLICATION_CREDENTIALS for itself — it
    points at the runtime SA key it mints FROM (/run/secrets/...). The
    sandbox sets the same variable, pointing at its own per-execution
    LEASE under the workspace. Same name, two very different files, and
    only one of them may ever be inside a sandbox.

    `build_sandbox_spec` constructs `env` explicitly rather than copying
    `os.environ`, which is what keeps them apart. Asserted here because
    the previous commit put that variable on `_safe_env`'s allowlist, so
    if the sandbox ever DID inherit the worker's environment, the
    allowlist would now wave the worker's key path straight through."""
    import os
    from unittest.mock import patch

    from kernel.conductor.sandbox_spec import build_sandbox_spec

    class _Driver:
        def sandbox_attachments(self, path, subpath):
            return {"volume_mounts": (), "binds": [], "network_mode": "bridge",
                    "allowed_bind_prefixes": []}

    worker_key = "/run/secrets/e0-runtime-sa.json"
    lease = "/workspace/.agentos/credentials/exec-7-cloud.json"
    with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": worker_key,
                                 "ANTHROPIC_API_KEY": "sk-worker-only"},
                    clear=False):
        spec = build_sandbox_spec(
            _Driver(), 7, bridge_port=9100, image="x", workspace_subpath="e7",
            credential_pointers={"GOOGLE_APPLICATION_CREDENTIALS": lease})

    env = spec.env if hasattr(spec, "env") else spec["env"]
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == lease
    assert worker_key not in env.values(), (
        "the worker's own key path reached the sandbox environment")
    assert "ANTHROPIC_API_KEY" not in env, (
        "build_sandbox_spec is copying os.environ; every worker "
        "credential is now in the sandbox")


def test_the_gcp_lease_is_a_record_and_the_token_is_its_own_file():
    """THE THIRD BREAK IN THE SAME CHAIN, found wiring real credentials.

    Pointer unset (fixed), pointer stripped by the shell allowlist
    (fixed), and then: the file itself in a shape no SDK can parse. All
    three produced the identical symptom -- "no credentials" with correct
    IAM and a valid token on disk -- which is why each one hid the next.
    """
    from kernel.credentials.gcp import GcpProvider

    minted = {"token": "ya29.real-looking", "account": "sa@p.iam",
              "scope": "proj-a", "expires_at": "2026-01-01T00:00:00Z"}
    p = GcpProvider()

    record = p.lease_payload(minted, {"cloud": "gcp"})
    assert record["kind"] == "gcp_access_token"
    assert record.get("type") != "authorized_user", (
        "the lease claims an ADC shape google-auth will refuse")

    side = p.sidecar_files(minted, {"cloud": "gcp"})
    assert side == {".token": "ya29.real-looking"}, (
        "gcloud needs the raw token alone in a file")


def test_revoke_removes_the_token_file_too():
    """A revoke that deletes the record and leaves the credential is
    worse than none: it reads as done."""
    import os
    import tempfile

    from kernel.workspace.credentials import (revoke, sidecar_path,
                                              write_lease, write_sidecar)

    with tempfile.TemporaryDirectory() as ws:
        write_lease(ws, "exec-5-cloud", {"kind": "gcp_access_token"})
        tok = write_sidecar(ws, "exec-5-cloud", ".token", "ya29.secret")
        assert os.path.exists(tok)
        assert revoke(ws, "exec-5-cloud") is True
        assert not os.path.exists(tok), "the raw token survived revoke"
        assert not os.path.exists(sidecar_path(ws, "exec-5-cloud", ".token"))
