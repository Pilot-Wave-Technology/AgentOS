"""Guards on the three things that made this package hard to use.

Each pins a change that is easy to undo by accident, and each failed
before this PR's review fixes.
"""
from __future__ import annotations

import inspect
import pathlib
import re

_PKG = pathlib.Path(__file__).resolve().parents[1]
_REPO = pathlib.Path(__file__).resolve().parents[3]


# ── the hooks must not run under the global lock ─────────────────────

def test_post_append_hooks_are_detached():
    """`pg_advisory_xact_lock` is TRANSACTION-scoped, so everything in
    `append_event` runs while the one global ledger lock is held.
    Awaiting a hook there makes any consumer's callback a bottleneck for
    every ledger writer in the system."""
    from agentos_ledger_client import journal

    src = inspect.getsource(journal.append_event)
    hooks = src[src.index("_post_append_hooks"):]
    assert "create_task" in hooks, (
        "a hook is awaited inside the advisory lock again")
    assert "await res" not in hooks, (
        "`await res` holds the global ledger lock for the hook's duration")


def test_a_failing_hook_cannot_break_the_append():
    from agentos_ledger_client import journal

    assert hasattr(journal, "_swallow")
    assert "exc_info=True" in inspect.getsource(journal._swallow), (
        "a detached hook that fails silently is unfindable")


# ── no hardcoded bucket, and no swallowed config failure ─────────────

def test_there_is_no_hardcoded_bucket():
    """It was `os.environ.get("GCS_RESOURCE_BUCKET",
    "${RESOURCE_BUCKET}")` inside a bare `except Exception`, so a
    configuration failure silently redirected ledger blobs instead of
    raising."""
    from agentos_ledger_client import blobs

    src = inspect.getsource(blobs)
    code = " ".join(
        ln.split("#")[0] for ln in src.splitlines())
    assert "${RESOURCE_BUCKET}" not in code
    assert "pwh" not in code, "the banned prefix is back"


def test_a_missing_bucket_raises():
    from agentos_ledger_client import blobs

    src = inspect.getsource(blobs.resource_bucket_name)
    assert "raise RuntimeError" in src
    assert "current()" in src, (
        "the bucket must come from the host's `configure()`, not from an "
        "import of app.config — see integration.py")
    # COMMENTS STRIPPED. The docstring and comments here explain what
    # this function no longer does, and they say the words `except` and
    # `${RESOURCE_BUCKET}` to do it. A guard that cannot tell an
    # explanation from the thing it forbids makes the code harder to
    # document than to get wrong — which is the third time that has
    # caught me in this file.
    code = " ".join(ln.split("#")[0] for ln in src.splitlines()
                    if not ln.strip().startswith(('"""', "'''")))
    code = code.split('"""')[-1] if '"""' in code else code
    assert "except" not in code, (
        "any except here is the swallowed-config-failure shape again")


# ── the two notes the extraction dropped ─────────────────────────────

def test_the_jsonb_ordering_note_survived():
    """Stops the next reader reimplementing the hash as a SQL function:
    jsonb orders keys by length, Python lexicographically, and every
    event ever written used the Python ordering."""
    doc = (_PKG / "src" / "agentos_ledger_client" / "chain.py").read_text(
        encoding="utf-8")
    assert "jsonb" in doc and "LENGTH" in doc
    assert "THE PYTHON ORDERING IS THE FORMAT" in doc


def test_the_actor_coverage_note_survived():
    """Stops the next reader extending the chain hash to cover the
    actor, which would invalidate every event already written."""
    doc = (_PKG / "src" / "agentos_ledger_client" / "chain.py").read_text(
        encoding="utf-8")
    assert "envelope_digest" in doc
    assert "deliberately NOT changed" in doc


# ── the schema ships with the wheel ──────────────────────────────────

def test_the_ddl_is_package_data():
    """`schema.sql` must live INSIDE the package and be declared, or it
    exists in the repository and is missing from every install."""
    assert (_PKG / "src" / "agentos_ledger_client" / "schema.sql").is_file()
    toml = (_PKG / "pyproject.toml").read_text(encoding="utf-8")
    assert "schema.sql" in toml, "schema.sql is not declared as package-data"


# ── the environments that broke on merge ─────────────────────────────

def test_e0_mounts_and_installs_the_package():
    """`kernel/ledger/chain.py` imports this package and E0 live-mounts
    `kernel/`. Without the mount and the install the import fails, and
    `worker/loop.py:_drain_ledger` catches it and logs one line a tick —
    so the kernel ledger stops with nothing saying so."""
    compose = _REPO / "infra" / "e0" / "docker-compose.yml"
    dockerfile = _REPO / "infra" / "e0" / "Dockerfile"
    if not compose.is_file():
        return                                   # not in the pwt tree
    text = compose.read_text(encoding="utf-8")
    assert "../../kernel:/app/kernel" in text
    assert "../../packages:/app/packages" in text, (
        "E0 mounts kernel/ but not packages/ — the kernel's ledger import "
        "fails and the drain goes quiet")
    assert "packages/agentos-ledger-client" in dockerfile.read_text(
        encoding="utf-8"), "the E0 image never installs the package"


def test_the_suite_collects_this_package():
    runner = _REPO / "scripts" / "ci" / "run_suite.sh"
    if not runner.is_file():
        return
    m = re.search(r'ROOTS="\$\{AGENTOS_TEST_ROOTS:-([^}]*)\}"',
                  runner.read_text(encoding="utf-8"))
    assert m and "packages" in m.group(1).split(), (
        "packages/ is not in the default test roots, so this file does "
        "not run in CI")


def test_a_plain_venv_can_import_the_package():
    """The repo root does not cover a `src` layout, so without this
    every test importing `app.ledger` failed at COLLECTION on a
    developer's machine and nowhere else."""
    # AT THE ROOT: a conftest applies downwards only, and tests
    # outside app/services/tests reach the ledger through imports too.
    conftest = _REPO / "conftest.py"
    if not conftest.is_file():
        return
    text = conftest.read_text(encoding="utf-8")
    assert "agentos-ledger-client" in text and "src" in text
