"""B6b -- the registry grows to match the image, so the allowlist stops.

THE SHAPE OF THE BUG THIS PREVENTS. `setups.REGISTRY` recognised Python
and Node manifests and nothing else. A Rust repository discovered
`none`, got no environment, and its first check failed on a missing
crate with nothing saying why -- and a missing dependency looks exactly
like a blocked host, so whoever hits it reaches for the egress
allowlist. crates.io is already permitted. The allowlist grows for a
problem it was never the cause of.

So the fix is a registry row, and these tests hold the two things that
would make the row useless: a cache written where the sandbox cannot
see it, and the registry overwriting a file the repository owns.
"""

from __future__ import annotations

from kernel.verifier import setups
from kernel.verifier.mount_env import ALLOWED_ENV_KEYS


def _row(manifest):
    return next(r for r in setups.REGISTRY if r["manifest"] == manifest)


def _files(names):
    """A `read` over a flat set of manifest names."""
    def read(path):
        return b"x" if path in names else None
    return read


# -- discovery ---------------------------------------------------------

def test_a_rust_repo_is_discovered():
    found = setups._mount("", _files({"Cargo.toml", "Cargo.lock"}))
    assert found, "a Rust repository still gets no environment"


def test_the_lockfile_wins():
    """Both present would otherwise run two fetches into one tree."""
    found = setups._mount("", _files({"Cargo.toml", "Cargo.lock"}))
    assert [f["source"] for f in found] == ["Cargo.lock"]


def test_a_lockless_rust_repo_is_supported_and_says_so():
    """Refusing would strand the repo; silence would hide that the
    resolver runs twice and may answer twice."""
    found = setups._mount("", _files({"Cargo.toml"}))
    assert [f["source"] for f in found] == ["Cargo.toml"]
    assert "Cargo.lock" in found[0]["warn"]


def test_a_python_repo_is_unaffected():
    found = setups._mount("", _files({"requirements.txt"}))
    assert [f["source"] for f in found] == ["requirements.txt"]


def test_a_polyglot_repo_gets_both_environments():
    """A Rust crate with a Python test harness is one tree and two
    installs, which is what `_mount` returning every match is for."""
    found = setups._mount("", _files({"Cargo.lock", "uv.lock"}))
    assert {f["source"] for f in found} == {"Cargo.lock", "uv.lock"}


# -- the cache has to be somewhere both containers can reach -----------

def test_the_crate_cache_lands_in_the_tree():
    """The default is `$HOME/.cargo`, HOME is /tmp, and /tmp is the
    CONTAINER's own layer -- so a cache the setup container fills is one
    the sandbox never sees and every story fetches its graph twice. The
    tree is the one thing both share, which is why `.venv` works."""
    for manifest in ("Cargo.lock", "Cargo.toml"):
        command = _row(manifest)["command"]
        assert "CARGO_HOME=$PWD/.cargo" in command, manifest


def test_the_build_output_and_the_cache_stay_out_of_the_commit():
    """`story_commit` runs `git add -A`; a vendored crate tree in the
    diff is the `.venv` lesson again."""
    for manifest in ("Cargo.lock", "Cargo.toml"):
        command = _row(manifest)["command"]
        assert "echo target/ >>" in command, manifest
        assert "echo .cargo/ >>" in command, manifest


def test_the_sandbox_is_told_where_the_crates_went():
    command = _row("Cargo.lock")["command"]
    assert "CARGO_HOME=$TREE/.cargo" in command
    assert "CARGO_HOME" in ALLOWED_ENV_KEYS, (
        "the line would be dropped by parse_env and the cache unfound")


def test_a_committed_env_file_is_never_overwritten():
    """`.agentos/env` is the repository's file. Writing over one a repo
    committed would be the registry editing the story's work."""
    for manifest in ("Cargo.lock", "Cargo.toml"):
        command = _row(manifest)["command"]
        assert "[ -f .agentos/env ]" in command, manifest
        assert ">> .agentos/env" not in command, manifest


# -- the row must be runnable in the image that exists -----------------

def test_the_row_names_only_what_the_image_carries():
    """A row naming a binary the image lacks fails as `command not
    found`, which reads as a broken repo rather than a missing tool --
    the reason every Python row bootstraps its tool with pip."""
    image = open("infra/e0/fixtures/Dockerfile", encoding="utf-8").read()
    assert "rustc cargo" in image
    for manifest in ("Cargo.lock", "Cargo.toml"):
        assert _row(manifest)["command"].count("cargo ") >= 1


def test_the_locked_row_is_actually_locked():
    assert "--locked" in _row("Cargo.lock")["command"]
    assert "--locked" not in _row("Cargo.toml")["command"]


def test_the_binaries_a_rust_build_produces_are_on_the_path():
    for manifest in ("Cargo.lock", "Cargo.toml"):
        assert "target/debug" in _row(manifest)["bins"]


# -- what this does NOT claim ------------------------------------------

def test_go_ruby_and_php_are_still_unrecognised_and_that_is_honest():
    """The rule is "the toolchains the image now carries". The image
    gained Rust and kept it across the return to 3.12; it has no go, no
    ruby and no php, and a row for one of them would fail as `command
    not found` -- the exact misdiagnosis this step exists to remove."""
    manifests = {r["manifest"] for r in setups.REGISTRY}
    assert not manifests & {"go.mod", "Gemfile", "composer.json"}
