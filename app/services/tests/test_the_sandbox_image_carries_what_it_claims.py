"""B3 -- the image the work happens in, and the traps in changing it.

One file builds this image twice: E0's preflight builds it locally and
`cloudbuild-next.yaml` builds it as the production sandbox base. So a
mistake here is a mistake in both rooms, and the ones that have actually
happened are the ones tested for.

Nothing here builds an image. These are the properties a reader would
otherwise have to re-derive from a Dockerfile every time it moves.
"""

from __future__ import annotations

import re

FIXTURE = "infra/e0/fixtures/Dockerfile"


def _text(path=FIXTURE):
    return open(path, encoding="utf-8").read()


# -- the trailing `|| true` that shipped an image with no git ----------

def test_no_run_chain_ends_in_or_true():
    """`|| true` is left-associative over the WHOLE chain, so a failed
    `apt-get install git` still exited 0 and the image shipped without
    git. It matters more now: `pnpm --version` is the only evidence the
    corepack step worked."""
    for line in _text().splitlines():
        stripped = line.strip()
        assert not stripped.endswith("|| true"), line


def test_every_install_chain_ends_in_a_version_check():
    """The evidence that the install worked, rather than the exit code
    of the last `rm`."""
    src = _text()
    assert "&& node --version && npm --version && pnpm --version" in src
    assert "&& rustc --version && cargo --version" in src


# -- both stages on the same Debian release ----------------------------

def test_every_from_stage_is_pinned_by_digest():
    """Two verifications of the same contract must see the same thing,
    and a `python:` or `node:` tag is a moving target -- during an
    earlier increment the local copy and the one the fixture was built
    from were already different builds of the same version."""
    froms = re.findall(r"^FROM\s+(\S+)", _text(), re.M)
    assert froms, "no FROM lines"
    for ref in froms:
        assert "@sha256:" in ref, f"{ref} is a tag, not a digest"
        digest = ref.split("@sha256:", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"{ref} is not a well-formed digest")


def test_the_debian_release_is_recorded_beside_each_digest():
    """A digest says nothing a person can read. The unsuffixed
    `python:3.X-slim` tag has historically defaulted to bookworm, and a
    mixed pair leaves `COPY --from=node` bringing a binary linked
    against a newer glibc than the base provides -- so the tag each
    digest came from is in the comment above it, and both say trixie."""
    lines = _text().splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("FROM "):
            continue
        provenance = " ".join(lines[max(0, i - 2):i])
        assert "trixie" in provenance, (
            f"nothing above {line!r} says which Debian release it is")


def test_the_file_says_how_to_move_a_pin():
    """A pin nobody knows how to move gets worked around instead."""
    src = _text()
    assert "RepoDigests" in src
    assert "PINNED BY DIGEST" in src
    assert "AGENTOS_REBUILD_FIXTURE" in src, (
        "moving a pin without rebuilding the fixture tests the old image")


# -- what the image now carries ----------------------------------------

def test_rust_is_in_the_image():
    """It arrived for Python 3.14's Rust-backed wheels and STAYS after the
    return to 3.12, for two reasons that did not leave with it:
    `setups.REGISTRY` recognises Cargo.lock and Cargo.toml now, so a Rust
    repository gets an environment rather than none; and `.crates.io` has
    been on the egress allowlist all along with nothing able to reach
    it."""
    src = _text()
    assert re.search(r"^\s+rustc cargo", src, re.M)


def test_pytest_is_pinned_to_three_components():
    match = re.search(r"pytest==(\d+)\.(\d+)\.(\d+)", _text())
    assert match, "pytest is not pinned to major.minor.patch"


def test_the_pytest_pin_names_f4_as_the_suite_to_run():
    """Its real risk is not red tests: F4's reward-hacking suite uses
    `addopts` manipulation AS AN ATTACK, so a change in ini precedence
    can flip a security finding rather than a colour."""
    src = _text()
    window = src[:src.index("pytest==")]
    assert "F4" in window[-1200:]


def test_corepack_is_the_mechanism_and_the_bake_is_only_a_cache():
    """A baked pnpm self-manages versions unless
    `manage-package-manager-versions=false` -- and setting that trades a
    network fetch for running a version the repo did not pin. There is
    also no single baked yarn that is right: `--immutable` is Berry,
    Classic wants `--frozen-lockfile`."""
    src = _text()
    assert "corepack enable" in src
    assert "corepack prepare" in src
    instructions = [l for l in src.splitlines()
                    if l.strip() and not l.lstrip().startswith("#")]
    assert not any("manage-package-manager-versions" in l
                   for l in instructions), (
        "that setting overrides the version the repo pinned")


# -- the passwd entry --------------------------------------------------

def test_uid_65532_has_an_identity():
    """Without it `pwd.getpwuid(os.getuid())` raises KeyError and
    everything built on it FAILS rather than degrading:
    `getpass.getuser()`, git's author auto-detection, some npm and ssh
    paths, and a shell printing `I have no name!`."""
    src = _text()
    assert "nonroot:x:65532:65532" in src
    assert "nonroot:x:65532:" in src          # the group


def test_its_home_is_tmp_and_not_a_directory_that_does_not_exist():
    """The passwd home is only read when $HOME is unset, which every
    container avoids -- but a fallback reachable only in the untested
    case must give the SAME answer as the explicit value, not a second
    broken one. /home/nonroot exists in neither sense."""
    line = next(l for l in _text().splitlines() if "nonroot:x:65532:65532" in l)
    assert ":/tmp:" in line
    instructions = [l for l in _text().splitlines()
                    if l.strip() and not l.lstrip().startswith("#")]
    assert not any("/home/nonroot" in l for l in instructions)


def test_the_identity_is_proved_before_the_layer_commits():
    assert "pwd.getpwuid(65532)" in _text()


def test_the_image_still_sets_no_home():
    """It is the sandbox AND the Verifier's prepare container; a HOME on
    the image gives those two a different one than the setup container."""
    src = _text()
    assert not re.search(r"^ENV HOME", src, re.M)
    assert not re.search(r"^\s+HOME=", src, re.M)


# -- the install's floor, now that source builds are possible ----------

def test_the_dependency_install_has_its_own_floor():
    from kernel.drivers import k8s_workspace as kw

    assert float(kw.SETUP_FLOOR_CPUS) > float(kw.INIT_CPUS), (
        "a single-threaded Rust build on half a core spends "
        "setup_budget_s on waiting")
    assert kw.SETUP_FLOOR_MEMORY_MB > kw.INIT_MEMORY_MB, (
        "a C++ translation unit at 2 GB is an OOM in the one container "
        "that most needs not to have one")


def test_the_floor_is_still_a_floor():
    """A story on a larger tier keeps its own allocation."""
    from kernel.drivers.k8s_workspace import setup_resources

    class Big:
        cpus = 8
        memory_mb = 32768

    got = setup_resources(Big())
    assert got["requests"]["cpu"] == "8"
    assert got["requests"]["memory"] == "32768Mi"


def test_a_spec_naming_no_size_still_gets_the_floor():
    from kernel.drivers.k8s_workspace import (SETUP_FLOOR_CPUS,
                                              SETUP_FLOOR_MEMORY_MB,
                                              setup_resources)

    class Bare:
        pass

    got = setup_resources(Bare())
    assert got["requests"]["cpu"] == f"{float(SETUP_FLOOR_CPUS):g}"
    assert got["requests"]["memory"] == f"{SETUP_FLOOR_MEMORY_MB}Mi"


# -- the false pass this change is likeliest to produce ----------------

def test_preflight_offers_a_deliberate_rebuild():
    """`preflight.sh` builds the fixture ONLY WHEN ABSENT. Edit the
    Dockerfile, run the suites on a machine that has run E0 before, and
    you test the old image and get a green."""
    src = _text("infra/e0/preflight.sh")
    assert "AGENTOS_REBUILD_FIXTURE" in src
    assert "docker rmi" in src


# -- the contrast image stays where several suites need it -------------

def test_the_no_contract_fallback_stays_on_the_old_python():
    """SHIM_IMAGE is the contrast image several E0 suites rely on; moving
    it with the fixture would move both sides of their comparison."""
    from kernel.conductor.sandbox_spec import SHIM_IMAGE

    assert SHIM_IMAGE == "python:3.12-slim"
