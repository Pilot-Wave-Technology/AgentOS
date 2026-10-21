"""Phase 1 — a brownfield repository gets an environment, a draft, or a
diagnosis, instead of silence.

WHAT A REPOSITORY AgentOS HAD NEVER SEEN GOT. Discovery matched no
manifest, so the tree got no environment; the scout that exists to draft
one returned nothing; and the first check failed on an import with
nothing anywhere saying why. Three separate causes, and the largest was
one missing string.

  the scout      `quick.available()` is `GEMINI_API_KEY` AND
                 `AGENTOS_ASSIST == "1"`. The key reaches the pod
                 through `model_key_env`; the flag was not on
                 `POD_ENV_ALLOWLIST`, so `available()` was False in
                 every execution pod and the scout, the assessor, the
                 investigator and the answerer were all dark there.
  the registry   five manifests, all of them `requirements.txt` or a
                 JS lockfile. Modern Python puts its dependencies in
                 `pyproject.toml`, which matched nothing.
  the refusals   named the Verifier, which is off by default, so a
                 model reading them saw a warning about something that
                 was not running.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── the flag the scout gates on reaches the pod ────────────────────────

def test_the_assist_flag_travels_to_the_execution_pod():
    """The scout runs in the leg. Without this it is dark there, and a
    dark feature and a feature with nothing to say look identical."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    assert "AGENTOS_ASSIST" in POD_ENV_ALLOWLIST


def test_the_context_budget_still_does_not_travel():
    """`AGENTOS_ASSIST_CONTEXT_TOKENS` trips the credential guard, which
    matches TOKEN as a substring and is right to. It is the assessor's
    alone; the leg's budget comes from the model registry."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    assert "AGENTOS_ASSIST_CONTEXT_TOKENS" not in POD_ENV_ALLOWLIST


def test_nothing_that_looks_like_a_credential_joined_the_list():
    """The guard this list is policed by, restated here so a future
    addition is caught in this file too."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    smells = [n for n in POD_ENV_ALLOWLIST
              if "API_KEY" in n or "TOKEN" in n or "SECRET" in n]
    assert not smells, f"{smells} reach the pod's env by name"


# ── modern Python is discovered ────────────────────────────────────────

@pytest.mark.parametrize("manifest", [
    "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock",
])
def test_the_registry_knows_modern_python(manifest):
    from kernel.verifier.setups import REGISTRY

    assert manifest in {row["manifest"] for row in REGISTRY}


def test_no_row_names_a_tool_the_image_does_not_ship():
    """THE DEPENDENCY THIS PHASE MUST NOT HAVE. The sandbox image ships
    python, pip, pytest, node/npm/pnpm/corepack and git -- no uv, no
    poetry, no pipenv. A row invoking one directly would fail as
    `command not found`, which reads as a broken repository, and would
    make Phase 1 secretly depend on the image work in Phase 2. Each of
    these bootstraps its own tool with pip first."""
    from kernel.verifier.setups import REGISTRY

    shipped = ("python3", "pip", "corepack", "pnpm", "yarn", "npm", "sh")
    for row in REGISTRY:
        command = row["command"]
        for tool in ("uv", "poetry", "pipenv"):
            if f" {tool} " not in command:
                continue
            assert f"pip install --no-input -q {tool}" in command, (
                f"{row['manifest']} calls {tool} without installing it, "
                f"and the image does not ship it")
    assert shipped  # the list is documentation for the next reader


def test_the_python_rows_are_mutually_exclusive():
    """`_mount` returns EVERY match, so without a supersede a repo with
    `requirements.txt` and `poetry.lock` runs two installs into one tree
    and whichever finishes last owns `.venv/bin`."""
    from kernel.verifier.setups import REGISTRY

    rows = {r["manifest"]: r.get("superseded_by", ()) for r in REGISTRY}

    for lock in ("uv.lock", "poetry.lock", "Pipfile.lock"):
        assert lock in rows["requirements.txt"], (
            f"requirements.txt does not yield to {lock}")
        assert lock in rows["pyproject.toml"]
    assert "requirements.txt" in rows["pyproject.toml"], (
        "pyproject.toml is the least specific Python manifest and must "
        "yield to requirements.txt, exactly as package.json yields to "
        "its lockfiles")


def test_the_generic_python_install_is_editable():
    """A story edits the tree and then runs its checks against it. A
    non-editable install copies the package into site-packages, so the
    checks exercise the copy made before the work."""
    from kernel.verifier.setups import REGISTRY

    row = next(r for r in REGISTRY if r["manifest"] == "pyproject.toml")
    assert "-e '.[" in row["command"] or "-e ." in row["command"]
    assert row.get("warn"), (
        "a pyproject with no lockfile resolves at install time and says "
        "nothing about it, which is what package.json's warn exists for")


def test_no_row_writes_config_into_the_tree():
    """`poetry config --local` writes `poetry.toml` into the repository,
    and `story_commit`'s `git add -A` would commit it as the story's
    work. The env vars do the same job and leave nothing behind."""
    from kernel.verifier.setups import REGISTRY

    for row in REGISTRY:
        assert "config --local" not in row["command"], row["manifest"]


# ── apt is refused, with the route ─────────────────────────────────────

def test_apt_is_refused_and_says_where_to_go_instead():
    """THE REASON HAS TO BE TRUE ON BOTH SUBSTRATES. On Kubernetes the
    sandbox is non-root and apt never worked. On E0 `spec.user` is unset,
    so the container runs as the image's root and apt DID work -- which
    makes "you are not root" a falsehood there, and a refusal that lies
    on one substrate invites a model to work around it on that one. It
    argues from what SURVIVES: the container is discarded and the tree
    is rebuilt, so nothing apt did is carried either way."""
    from kernel.harness.executor_env_install import APT_REFUSAL

    assert "does not survive" in APT_REFUSAL, (
        "the refusal rests on the uid alone, which is false on E0")
    assert "rebuilt" in APT_REFUSAL
    assert "non-root" in APT_REFUSAL
    assert "BASE IMAGE" in APT_REFUSAL
    assert "ask_human" in APT_REFUSAL
    assert "pip or npm" in APT_REFUSAL, (
        "the refusal does not say what to do when the package is in "
        "fact a python or node one, which is the common mistake")


def test_apt_is_still_a_known_manager():
    """Dropping it from MANAGERS would make this indistinguishable from
    a typo -- 'not a manager this records' -- and the whole point is to
    say why and where instead."""
    from kernel.harness.executor_env_install import MANAGERS

    assert "apt" in MANAGERS


def test_the_tool_no_longer_offers_apt():
    """Refused at runtime AND not advertised: a schema that offers a
    manager which always refuses spends a leg to teach the model
    something the description could have said."""
    from kernel.tools.catalog import schemas_for

    schema = schemas_for(["env_install"])[0]
    enum = schema["parameters"]["properties"]["manager"]["enum"]
    assert enum == ["pip", "npm"], enum


# ── and the refusals are true whichever way the switch is set ──────────

def test_no_model_facing_refusal_promises_a_verifier():
    """Verification is a platform switch, off by default. A refusal that
    justifies itself by naming the Verifier reads as a warning about
    something that is not running -- and a model that believes the
    reason does not apply argues with the refusal instead of taking the
    route it offers."""
    import ast

    # DOCSTRINGS ARE NOT MODEL-FACING and may say whatever is true of
    # the design. Only strings the refusal is built from matter here.
    tree = ast.parse(_src("kernel/harness/env_guard.py"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str) or node.value in docstrings:
            continue
        assert "Verifier" not in node.value, (
            f"a model-facing string still names the Verifier: "
            f"{node.value[:70]!r}")


def test_the_refusal_names_what_is_actually_lost():
    """True in both modes: the tree is rebuilt between rounds, so an
    unrecorded install is gone by the next leg whether or not anything
    verifies it."""
    # THE SOURCE IS NOT THE MESSAGE: these refusals are built from
    # adjacent f-string fragments, so no sentence appears contiguously
    # in the file. Build the refusal and read what it says.
    from kernel.harness.env_guard import refusal

    said = refusal("pip install requests", "pip",
                   ("env_setup", "env_install"))
    assert "leaves no RECORD" in said, said
    assert "next leg rebuilds this tree" in said, said
    assert "Verifier" not in said, said


def test_the_env_tools_describe_the_record_not_the_verifier():
    body = _src("kernel/tools/catalog/files.py")
    i = body.index('"env_setup": _schema')
    j = body.index('"env_install": _schema')
    window = body[i:j + 2000]

    assert "Verifier" not in window, (
        "env_setup or env_install still explains itself by naming the "
        "Verifier, which is off by default")
    assert "RECORDED" in window


# ── the image can build, and the install has room to ───────────────────

def test_the_image_carries_a_compiler_and_the_headers_that_need_it():
    """Without these a dependency with no prebuilt wheel dies at
    `gcc: command not found`, and with apt refused the story has no way
    forward but a human round trip."""
    body = _src("infra/e0/fixtures/Dockerfile")

    for pkg in ("build-essential", "pkg-config", "libpq-dev",
                "libssl-dev", "libffi-dev", "libxml2-dev", "zlib1g-dev"):
        assert pkg in body, f"{pkg} is not in the sandbox image"


def test_the_image_still_sets_no_home():
    """PROPOSED TWICE AND REFUSED TWICE. This image is the sandbox AND
    the Verifier's prepare container; a HOME here would give those two a
    different home from the setup container, whose caches `mount_env`
    places explicitly. HOME belongs on the container."""
    import re

    body = _src("infra/e0/fixtures/Dockerfile")
    assert not re.search(r"^\s*ENV\s+HOME", body, re.M), (
        "ENV HOME is back in the shared image")


def test_the_image_says_it_is_not_bit_reproducible():
    """This file argues hard for digest pinning, and an apt layer is not
    pinned -- installing the toolchain upgrades libc6 on top of the
    pinned base. The claim survives where it is consumed, not at build
    time, and the next reader should not have to rediscover that."""
    body = _src("infra/e0/fixtures/Dockerfile")

    assert "NOT BIT-REPRODUCIBLE" in body
    assert "AGENTOS_SANDBOX_BASE_DIGEST" in body


def test_the_dependency_install_gets_the_tier_not_a_constant():
    """It used to take the clone's 0.5 vCPU / 2 GB on every rung. That
    is not IO-bound work -- it compiles now -- and with one story per
    node the machine is paid for and idle while it runs."""
    from kernel.drivers.k8s_workspace import setup_resources

    class _Spec:
        def __init__(self, cpus, memory_mb):
            self.cpus, self.memory_mb = cpus, memory_mb

    assert setup_resources(_Spec(4.0, 16384))["requests"] == {
        "cpu": "4", "memory": "16384Mi"}
    assert setup_resources(_Spec(8.0, 32768))["limits"] == {
        "cpu": "8", "memory": "32768Mi"}


def test_the_install_never_gets_less_than_it_used_to():
    """A spec that names no size must not make the install smaller than
    its floor -- and B3 raised that floor above the clone's.

    The image carries `cargo` now, so this container may run a Rust or
    C++ source build: at half a core that spends `setup_budget_s` on
    waiting, and at 2 GB a single translation unit is an OOM in the one
    place it is most needed.
    """
    from kernel.drivers.k8s_workspace import (INIT_CPUS, INIT_MEMORY_MB,
                                              SETUP_FLOOR_CPUS,
                                              SETUP_FLOOR_MEMORY_MB,
                                              setup_resources)

    class _Spec:
        cpus = 0
        memory_mb = 0

    got = setup_resources(_Spec())["requests"]
    assert got == {"cpu": f"{float(SETUP_FLOOR_CPUS):g}",
                   "memory": f"{SETUP_FLOOR_MEMORY_MB}Mi"}
    assert float(SETUP_FLOOR_CPUS) >= float(INIT_CPUS)
    assert SETUP_FLOOR_MEMORY_MB >= INIT_MEMORY_MB


def test_the_clone_is_still_small():
    """A clone IS IO-bound, and giving it a tier buys nothing. The two
    init containers do different jobs and keep different answers."""
    body = _src("kernel/drivers/k8s_workspace.py")
    i = body.index('"name": "workspace-compose"') if (
        '"name": "workspace-compose"' in body) else 0
    assert 'INIT_CPUS' in body, "the clone lost its own constant"
    assert "setup_resources(spec)" in body, (
        "the dependency install is not taking the tier")


# ── and the agents are told what they have ─────────────────────────────

def test_the_executor_is_told_its_environment(monkeypatch):
    """It was told NOTHING. The only description anywhere was a sentence
    in the scout's prompt, so a model could not know it had a compiler
    and could not know apt would be refused.

    B7 made it per image: the conductor names the resolved image on the
    spec and the prompt describes THAT one.
    """
    from kernel import env_text
    from kernel.base_image import ENVIRONMENT
    from kernel.profiles.loader import load_all
    from kernel.profiles.prompt import compose_system_prompt

    monkeypatch.setenv(env_text.ENV_VAR, "agentos-kernel/fixture:dev")
    profile = load_all()[("story", "coding")]
    assert ENVIRONMENT.strip() in compose_system_prompt(profile, "")


def test_an_executor_on_an_image_we_do_not_build_is_told_to_check(
        monkeypatch):
    """Describing an unknown image as if it were ours is worse than
    saying nothing: the model acts on specifics that are false."""
    from kernel import env_text
    from kernel.profiles.loader import load_all
    from kernel.profiles.prompt import compose_system_prompt

    monkeypatch.setenv(env_text.ENV_VAR, "rust:1.83-bookworm")
    profile = load_all()[("story", "coding")]
    prompt = compose_system_prompt(profile, "")
    assert env_text.UNKNOWN.strip() in prompt
    assert "A C AND RUST TOOLCHAIN IS PRESENT" not in prompt


def test_the_scout_and_the_executor_read_the_same_sentence():
    """The scout drafts a setup script FOR this image. One describing it
    from memory drafts against an environment nobody runs."""
    from kernel.base_image import ENVIRONMENT
    from kernel.worker.scout import PROMPT

    rendered = PROMPT.format(at=".", reason="r", files="f",
                             environment=ENVIRONMENT.strip())
    assert "TOOLCHAIN IS PRESENT" in rendered
    assert "python3+pip+venv, node 22" not in rendered, (
        "the scout still carries its own hardcoded inventory")


def test_the_stanza_matches_the_image_it_describes():
    """THE DRIFT THIS PREVENTS. A stanza is a claim about a Dockerfile,
    and the two live in different files. Same trick that keeps
    JOB_MAX_S and budget.MAX_S equal."""
    from kernel.base_image import ENVIRONMENT

    dockerfile = _src("infra/e0/fixtures/Dockerfile")
    # A STANZA NAMES BINARIES AND A DOCKERFILE NAMES PACKAGES, so the
    # pairing is explicit rather than a substring match -- `psql` comes
    # from `postgresql-client` and matching one against the other would
    # pass or fail for the wrong reason.
    for spoken, package in (("git-lfs", "git-lfs"),
                            ("pkg-config", "pkg-config"),
                            ("psql", "postgresql-client"),
                            ("sqlite3", "sqlite3"),
                            ("jq", "jq"),
                            ("uv", "uv"),
                            ("gcc", "build-essential")):
        assert spoken in ENVIRONMENT, (
            f"the stanza never mentions {spoken}")
        assert package in dockerfile, (
            f"the stanza promises {spoken} and the image lacks {package}")


def test_the_stanza_stays_short_enough_to_live_in_every_request():
    """It sits in the STABLE PREFIX of every request. Correctness is not
    the only bar -- length is paid on every step of every leg."""
    from kernel.base_image import ENVIRONMENT

    assert len(ENVIRONMENT) < 2000, (
        f"{len(ENVIRONMENT)} chars in the prefix of every request")


def test_the_prefix_is_still_constant_across_legs():
    """`compose_system_prompt`'s whole argument: anything time-varying
    here turns every step into a cache miss, priced at the difference
    between a $60 story and a $314 one."""
    from kernel.profiles.loader import load_all
    from kernel.profiles.prompt import compose_system_prompt

    profile = load_all()[("story", "coding")]
    assert compose_system_prompt(profile, "") == compose_system_prompt(
        profile, "")


def test_the_setup_container_has_somewhere_to_put_a_cache():
    """The third room. The sandbox gets HOME=/tmp and the prepare
    container sets its own; this one had neither, so HOME resolved to
    `/` and every tool caching under `~` failed on a path nobody
    chose."""
    body = _src("kernel/drivers/k8s_workspace.py")
    i = body.index('"name": "workspace-setup"')
    window = body[i:i + 3000]

    assert '"name": "HOME"' in window, "the setup container has no HOME"
    assert '"value": "/tmp"' in window
    assert "setup_resources(spec)" in window, (
        "and it is no longer the same block that takes the tier")


def test_the_clone_image_can_fetch_lfs_objects():
    """`workspace-compose` runs in the kernel image and clones the
    story's repositories. Without git-lfs a repo using it clones to
    pointer stubs and nothing fails."""
    assert "git-lfs" in _src("Dockerfile")


# -- a story's checks are tests, and tests need test tooling ----------

def test_the_python_install_asks_for_the_dev_extras():
    """`-e .` alone installs the package and not its dev group, so a
    repo whose pyproject sets `asyncio_mode` or `timeout` -- both of
    which need pytest plugins -- got an environment where pytest failed
    at CONFIG LOAD, before collecting anything.

    Nothing told the model why: the report named the manifest and not
    the command, `-q` suppressed the output, and the scout only fires
    when discovery found NOTHING. Someone new to the platform concludes
    it is broken.
    """
    from kernel.verifier.setups import REGISTRY

    row = next(r for r in REGISTRY if r["manifest"] == "pyproject.toml")
    for group in ("dev", "test", "tests"):
        assert group in row["command"], group


def test_the_node_rows_already_installed_dev_dependencies():
    """Which is the argument that this is a correction, not a new
    policy: `npm ci`, `pnpm install --frozen-lockfile` and `yarn install
    --immutable` all take devDependencies by default, so a Node story
    has had its test tooling all along and a Python story had not."""
    from kernel.verifier.setups import REGISTRY

    for manifest in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                     "package.json"):
        row = next(r for r in REGISTRY if r["manifest"] == manifest)
        assert "--omit=dev" not in row["command"], manifest
        assert "--production" not in row["command"], manifest


def test_asking_for_an_absent_extra_is_survivable():
    """No manifest parsing, which keeps this file what it is: filename
    matching and command strings. pip warns and exits 0 for an extra a
    project does not declare, so all three spellings can be asked for
    unconditionally."""
    from kernel.verifier.setups import REGISTRY

    row = next(r for r in REGISTRY if r["manifest"] == "pyproject.toml")
    # One pip invocation, not a chain of fallbacks -- a `||` ladder would
    # mask a real install failure behind the next attempt.
    assert row["command"].count("pip install") == 1
    assert "||" not in row["command"]


def test_the_setup_report_names_the_command_it_ran():
    """The deeper fault was invisibility, not the wrong flag: it hides
    the NEXT wrong command too."""
    import inspect

    from kernel.worker.envsetup import _line

    src = inspect.getsource(_line)
    assert 'setup.get("command")' in src
    assert "ran:" in src
