"""A node install survives the trip to the Verifier.

THE HOLE. `env_install(manager="npm")` ran `npm install -g`, which
writes into the image's own tree. The Verifier starts from the
contract's digest and has no way to carry an image mutation, so every
npm delta was recorded `reproducible = false` and the model was told:

    a npm install cannot be reproduced by the Verifier from a clean
    base. It is recorded, and the checks will run WITHOUT it.

A Node story that installed a test runner ad-hoc and then wrote a check
needing it therefore got `error` in verification, every time, with
nothing it could do about it. pip had a replay and npm did not, and the
difference was never the language -- it was `-g`.

The fix is a local install on both sides and one captured directory
carrying both managers. What these tests hold down:

  * the executor stops installing globally, so there IS something to
    carry
  * the capture installs both managers into the one directory it takes
    away, and refuses to hand back half an environment
  * the archive splits into TWO roots, because node finds `node_modules`
    by walking up and not by reading a variable
  * `/tmp/node_modules` is on that walk from the tree at `/tmp/verify`,
    for `import` as well as `require`
"""
from __future__ import annotations

import io
import tarfile

import pytest

from kernel.verifier import deps_layout
from kernel.verifier.environment import REPLAYABLE, packages_for


def _delta(seq, manager, packages, reproducible=True):
    return {"seq": seq, "manager": manager, "packages": packages,
            "reproducible": reproducible}


def _tar(names):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as t:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 3
            t.addfile(info, io.BytesIO(b"abc"))
    return out.getvalue()


def _names(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as t:
        return sorted(t.getnames())


# ── what the model is promised ──────────────────────────────────────────

def test_npm_is_replayable_and_apt_still_is_not():
    """The tool's NOTE is generated from this set, so it is the promise.

    `apt` stays out on purpose: it really does mutate the image and
    there is no prefix form of it.
    """
    assert "npm" in REPLAYABLE
    assert "pip" in REPLAYABLE
    assert "apt" not in REPLAYABLE


def test_the_executor_no_longer_installs_globally():
    """`-g` is the whole defect. A directory travels; an image does not."""
    command = deps_layout.npm_install('"$PWD"', ["typescript"])
    assert " -g" not in command and "--global" not in command
    assert '--prefix "$PWD"' in command


def test_npm_is_never_left_to_choose_its_own_prefix():
    """npm's default prefix WALKS UP looking for a package.json.

    In a workspace root holding neither a package.json nor a
    node_modules it would climb out of the workspace entirely and
    install somewhere nothing reads. pip's `--target` has no such
    behaviour, which is why only npm has to say it.
    """
    assert "--prefix" in deps_layout.npm_install("/deps", ["jest"])


def test_a_version_specifier_is_not_shell_syntax():
    """`@scope/pkg@^1.2.3` and `pkg>=1` both reach npm intact.

    The pip side of this was measured on execution 197: five files named
    `=0.110.0` and friends in the workspace root, one of them holding
    the output that should have gone back to the model.
    """
    command = deps_layout.npm_install("/deps", ["@scope/p@^1.2.3", "a>=1"])
    # QUOTED, not stripped: a specifier is a legitimate thing to ask for.
    # What must not survive is a BARE `>`, which `sh` reads as a redirect.
    assert "'a>=1'" in command
    assert "'@scope/p@^1.2.3'" in command
    assert " a>=1" not in command


# ── the deltas, split by manager ────────────────────────────────────────

def test_each_manager_gets_its_own_packages_in_order():
    deltas = [_delta(1, "pip", ["fastapi"]), _delta(2, "npm", ["jest"]),
              _delta(3, "pip", ["fastapi==0.110.0"]),
              _delta(4, "npm", ["typescript"])]
    assert packages_for(deltas, "pip") == ["fastapi", "fastapi==0.110.0"]
    assert packages_for(deltas, "npm") == ["jest", "typescript"]


def test_an_unreproducible_delta_is_not_replayed():
    deltas = [_delta(1, "npm", ["jest"], reproducible=False)]
    assert packages_for(deltas, "npm") == []


def test_packages_arriving_as_json_text_are_parsed_for_npm_too():
    """asyncpg hands a jsonb column back as a STRING, and `extend` on a
    string extends by CHARACTER. The pip path was measured failing this
    way on execution 202; npm reads the same column."""
    deltas = [_delta(1, "npm", '["jest", "typescript"]')]
    assert packages_for(deltas, "npm") == ["jest", "typescript"]


# ── the archive, and its two roots ──────────────────────────────────────

def test_the_node_tree_leaves_the_python_root():
    """`/tmp/deps/node_modules` would be invisible to node.

    Node's resolver walks UP from the importing file. The tree lands at
    `/tmp/verify`, so the walk passes `/tmp/node_modules` and never
    `/tmp/deps/node_modules`. Re-rooting is what puts it on the path.
    """
    out = _names(deps_layout.rebase(_tar([
        "deps/", "deps/fastapi/__init__.py",
        "deps/node_modules/jest/index.js",
        "deps/node_modules/.bin/jest"])))
    assert "node_modules/jest/index.js" in out
    assert "node_modules/.bin/jest" in out
    assert "deps/fastapi/__init__.py" in out
    assert not any(n.startswith("deps/node_modules") for n in out)


def test_the_python_root_is_still_where_pythonpath_points():
    from kernel.verifier.kinds import DEPS_PATH

    assert DEPS_PATH == "/tmp/" + deps_layout.PY_PREFIX


def test_the_node_root_is_on_the_walk_up_from_the_tree():
    """The one property the whole re-rooting exists to produce."""
    from kernel.verifier.kinds import TREE_PATH

    assert deps_layout.NODE_DIR == "/tmp/" + deps_layout.NODE_PREFIX
    assert TREE_PATH.startswith(deps_layout.DEPS_PARENT + "/")


def test_a_capture_from_a_different_root_is_still_normalised():
    """A base image whose pip target differs must not land elsewhere."""
    out = _names(deps_layout.rebase(_tar(["site/fastapi/__init__.py"])))
    assert out == ["deps/fastapi/__init__.py"]


def test_an_empty_capture_stays_empty():
    assert deps_layout.rebase(b"") == b""


# ── what the criterion container is told ────────────────────────────────

def test_the_replayed_executables_are_on_the_path():
    """`jest` named bare in a criterion resolves nowhere without this."""
    from kernel.verifier.kinds import _path

    assert _path("", b"tar") == deps_layout.NODE_BIN


def test_a_prepared_mount_still_wins_over_the_replay():
    """A repo that built node_modules from a committed manifest is the
    more authoritative environment; the ad-hoc replay is the fallback."""
    from kernel.verifier.kinds import _path

    got = _path("/tmp/verify/repos/a/node_modules/.bin", b"tar").split(":")
    assert got[0] == "/tmp/verify/repos/a/node_modules/.bin"
    assert got[-1] == deps_layout.NODE_BIN


def test_no_replay_adds_no_path():
    from kernel.verifier.kinds import _path

    assert _path("", None) == ""
    assert _path("/x/bin", None) == "/x/bin"


# ── the one capture that carries both ───────────────────────────────────

class _Driver:
    """Enough of a substrate to see what the capture was asked to run."""

    CAPTURE_MODEL = "archive"
    egress_proxy = ""

    def __init__(self):
        self.spec = None

    async def run_ephemeral_capture(self, spec, capture, timeout):
        self.spec = spec
        assert capture == deps_layout.CONTAINER_DEPS
        return {"exit_code": 0, "output": "", "timed_out": False,
                "archive": _tar(["deps/node_modules/jest/index.js"])}


async def _build(deltas):
    from kernel.verifier.environment import build_deps

    driver = _Driver()
    tar, report = await build_deps(driver, "img", deltas)
    return driver, tar, report


@pytest.mark.asyncio
async def test_a_story_that_only_used_npm_still_gets_a_replay():
    """It used to return an empty tar and `ok` before running anything,
    because the package list was pip's alone."""
    driver, tar, report = await _build([_delta(1, "npm", ["jest"])])
    assert driver.spec is not None, "the capture never ran"
    assert report["ok"] and tar
    assert report["npm"] == ["jest"] and report["pip"] == []


@pytest.mark.asyncio
async def test_both_managers_install_into_the_one_captured_directory():
    driver, _, _ = await _build([_delta(1, "pip", ["fastapi"]),
                                 _delta(2, "npm", ["jest"])])
    command = driver.spec.command[-1]
    assert "pip install" in command and "npm install" in command
    assert f"--target {deps_layout.CONTAINER_DEPS}" in command
    assert f"--prefix {deps_layout.CONTAINER_DEPS}" in command


@pytest.mark.asyncio
async def test_a_failed_pip_never_lets_npm_run():
    """`&&`, not `;`. Half an environment reported `ok` would fail the
    criterion on the missing half as if the work were wrong."""
    driver, _, _ = await _build([_delta(1, "pip", ["fastapi"]),
                                 _delta(2, "npm", ["jest"])])
    steps = driver.spec.command[-1]
    assert steps.index("pip install") < steps.index("npm install")
    assert "&&" in steps
    assert ";" not in steps


@pytest.mark.asyncio
async def test_npm_gets_its_prefix_created():
    """pip's `--target` makes the directory; npm's `--prefix` does not,
    so an npm-only story would install into nothing."""
    driver, _, _ = await _build([_delta(1, "npm", ["jest"])])
    assert f"mkdir -p {deps_layout.CONTAINER_DEPS}" in driver.spec.command[-1]


@pytest.mark.asyncio
async def test_nothing_recorded_means_no_container_at_all():
    driver, tar, report = await _build([])
    assert driver.spec is None and tar == b"" and report["ok"]


@pytest.mark.asyncio
async def test_apt_is_still_reported_as_skipped():
    """F6 counts divergence. A delta that vanishes is one nobody sees."""
    _, _, report = await _build([_delta(1, "npm", ["jest"]),
                                 _delta(2, "apt", ["curl"])])
    assert [d["manager"] for d in report["skipped"]] == ["apt"]
