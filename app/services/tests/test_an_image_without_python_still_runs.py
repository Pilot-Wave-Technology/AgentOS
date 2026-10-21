"""B2 -- the sandbox's interpreter travels with the shim, not with the image.

THE FAILURE. A contract names a Go or Rust image. The pod starts, the
shim's command is `python <bridge>/shim.py`, there is no python, the
bridge never answers, and the story reads as a dead pod.

THERE IS NOTHING TO REFUSE ON, which is why this is shipping and not a
check: no route exists to inspect an image's contents before running it
(the Docker socket proxy runs without IMAGES=1 and preflight says not to
add it; on Kubernetes there is no registry client). All that exists is
the image's name, and a name allowlist would be wrong for exactly the
case `base_image` promises works.

These tests hold the three things that could quietly undo it: a bare
`python` creeping back into either substrate, the interpreter being
built but not carried into the final image, and the copy losing the
executable bit.
"""

from __future__ import annotations

import os

from kernel.conductor import sandbox_spec
from kernel.drivers import interpreter, k8s_bridge


# -- no bare `python`, in either room ----------------------------------

def test_neither_substrate_invokes_bare_python():
    """An image with `python3` and no `python` used to work locally and
    fail in the cluster, which is the worst shape a difference takes."""
    assert interpreter.FALLBACK == "python3"
    for shipped in (True, False):
        argv = interpreter.shim_command("/x", shipped=shipped)
        assert argv[0] != "python", argv


def test_the_fallback_is_the_same_name_on_both_paths():
    docker = interpreter.shim_command("/app/bridge", shipped=False)[0]
    pod = interpreter.shim_command(k8s_bridge.BRIDGE_DIR, shipped=False,
                                   bind="0.0.0.0")[0]
    assert docker == pod == interpreter.FALLBACK


# -- what the sandbox runs when the interpreter is shipped -------------

def test_the_shipped_runner_is_an_absolute_path_under_the_bridge():
    """Relative would resolve against the CONTRACT's working directory,
    which is the contract's business and not ours."""
    argv = interpreter.shim_command(k8s_bridge.BRIDGE_DIR, shipped=True,
                                    bind="0.0.0.0")
    assert argv[0].startswith(k8s_bridge.BRIDGE_DIR + "/")
    assert os.path.isabs(argv[0])
    assert argv[1] == f"{k8s_bridge.BRIDGE_DIR}/shim.py"
    assert argv[2:] == ["--bind", "0.0.0.0"]


def test_the_interpreter_rides_in_the_bridge_volume():
    """One volume: a sandbox that has the shim has the thing that runs it."""
    assert not interpreter.SHIPPED_BIN.startswith("/")
    assert interpreter.SHIPPED_BIN.startswith(interpreter.SHIPPED_SUBDIR + "/")


# -- the initContainer -------------------------------------------------

def test_the_init_container_copies_the_interpreter_when_there_is_one(
        monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv(interpreter.SOURCE_ENV, str(tmp_path))
    assert interpreter.available()

    command = k8s_bridge.init_container("kernel:img", 65532)["command"][-1]
    assert "kernel/bridge/." in command
    assert str(tmp_path) in command
    assert k8s_bridge.BRIDGE_DIR + "/" + interpreter.SHIPPED_SUBDIR in command


def test_nothing_changes_before_the_image_carries_one(monkeypatch, tmp_path):
    """This lands safely ahead of the image that ships an interpreter.

    Until then every sandbox falls back exactly as it does today, and
    the day the image ships one they all pick it up with no code change.
    """
    monkeypatch.setenv(interpreter.SOURCE_ENV, str(tmp_path / "absent"))
    assert not interpreter.available()

    command = k8s_bridge.init_container("kernel:img", 65532)["command"][-1]
    assert interpreter.SHIPPED_SUBDIR not in command
    assert k8s_bridge.sandbox_command()[0] == interpreter.FALLBACK


def test_the_same_answer_decides_the_copy_and_the_command(monkeypatch,
                                                          tmp_path):
    """The initContainer runs the conductor's image, so "do I have one to
    ship" and "will the copy find one" must be one question."""
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv(interpreter.SOURCE_ENV, str(tmp_path))
    command = k8s_bridge.init_container("kernel:img", 65532)["command"][-1]
    runner = k8s_bridge.sandbox_command()[0]
    assert interpreter.SHIPPED_SUBDIR in command
    assert runner.endswith(interpreter.SHIPPED_BIN)


def test_the_copy_is_cp_r_and_not_cp_a():
    """`-a` implies `-p`; only root may preserve another owner, and this
    container is deliberately not root. A complete, usable copy exited
    non-zero and the pod reported Init:Error."""
    command = interpreter.copy_command("/opt/agentos-bridge")
    assert "cp -r" in command
    assert "cp -a" not in command
    assert "cp -p" not in command and "cp -rp" not in command


# -- the image actually carries it -------------------------------------

def test_the_dockerfile_lands_it_where_the_code_looks_for_it():
    """Built in the builder stage and not copied forward is a silent miss:
    every sandbox would fall back for ever and nothing would say why."""
    src = open("Dockerfile", encoding="utf-8").read()
    assert interpreter.SOURCE_DIR in src
    final = src[src.rindex("\nFROM "):]
    assert f"COPY --from=builder {interpreter.SOURCE_DIR}" in final, (
        "the interpreter is built but never reaches the runtime image")


def test_the_dockerfile_proves_the_build_runs_from_its_new_prefix():
    """A build that is not relocatable for this arch must fail HERE, not
    three weeks later as a sandbox that starts and never answers."""
    src = open("Dockerfile", encoding="utf-8").read()
    assert f"{interpreter.SOURCE_DIR}/bin/python3 -c" in src


def test_it_is_a_relocatable_build_and_not_this_image_s_own_python():
    """`/usr/local/bin/python3` here is linked against this base's glibc,
    libssl and libffi; copied elsewhere it finds a different set."""
    src = open("Dockerfile", encoding="utf-8").read()
    assert "python-build-standalone" in src
    assert "install_only" in src


# -- the docker path is explicit about not shipping --------------------

def test_docker_asks_for_the_fallback_deliberately():
    """A bind mount has no equivalent: the host checkout holds the bridge,
    not a built runtime."""
    import inspect
    src = inspect.getsource(sandbox_spec)
    assert "interpreter.shim_command(BRIDGE_MOUNT, shipped=False)" in src
