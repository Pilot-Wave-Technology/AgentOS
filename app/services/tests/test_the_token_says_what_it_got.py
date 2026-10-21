"""A credential must not claim more than the forge gave it.

MEASURED ON EXECUTION 204. Verification PASSED, the push was attempted
for the first time, and the forge refused it:

    remote: Write access to repository not granted ... error: 403

Two separate defects made that take three rounds to diagnose.

THE CAUSE. `_WRITE_PERMISSIONS` asked for `workflows: write` on every
credential. `app/services/local_diff.py::mint_push_token` asks for
`contents:write, metadata:read` and nothing else, and had been creating
branches and pushing against the SAME App, on the SAME repo, all week.
The only difference between the path that worked and the path that did
not was that one key. `workflows` is needed only to create or update
files under `.github/workflows/` -- which almost no story does.

THE REASON IT WAS INVISIBLE. `generate_installation_token` receives
GitHub's response, which states the permissions actually GRANTED, and
logged only the repo count and the expiry. So the router's
"issued write credential" line reported what it had ASKED FOR. A
narrowed token does not fail where it is minted; it fails later, as a
forge refusal, in a container, on a path that reads like the work being
wrong.
"""
from __future__ import annotations

import pathlib

from app.routers import git_credential


def test_the_pod_asks_for_what_the_working_path_asks_for():
    """The local path pushes successfully with exactly these two."""
    assert git_credential._WRITE_PERMISSIONS == {
        "contents": "write", "metadata": "read"}


def test_no_credential_asks_for_workflows():
    """Requesting a permission the installation does not hold poisons the
    whole request -- the token comes back narrowed and the failure lands
    three layers away as a 403."""
    for perms in (git_credential._WRITE_PERMISSIONS,
                  git_credential._READ_PERMISSIONS):
        assert "workflows" not in perms


def test_read_and_write_stay_distinct():
    """The router chooses between them by identity (`is`), so they must
    not collapse into equal dicts."""
    assert (git_credential._WRITE_PERMISSIONS
            != git_credential._READ_PERMISSIONS)
    assert git_credential._READ_PERMISSIONS["contents"] == "read"


# -- the granted-vs-asked log ------------------------------------------

SRC = pathlib.Path(
    git_credential.__file__).parents[1] / "github_tokens.py"
BODY = SRC.read_text(encoding="utf-8")


def test_the_mint_logs_what_was_granted_not_what_was_asked():
    assert 'data.get("permissions")' in BODY
    assert "granted=" in BODY


def test_a_narrowed_token_is_reported_at_error():
    """It must be loud. Every symptom of a narrowed token appears
    somewhere else, wearing someone else's error message."""
    assert "NARROWED" in BODY
    assert "_log.error" in BODY.split("granted = ", 1)[1]


def test_the_scoped_repositories_are_logged_by_full_name():
    """`repositories` is sent as bare NAMES, so an installation spanning
    two accounts with a same-named repo would scope somewhere nobody
    asked for. What came back is the only way to see that."""
    assert 'r.get("full_name")' in BODY
