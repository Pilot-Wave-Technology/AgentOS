"""The env replay's sidecar needs the metadata server. Measured on 205.

WHAT HAPPENED. A story installed fastapi, uvicorn, pydantic, pytest and
httpx. The verifier replayed them in a two-container Job: a WORK
container that runs the install, and a capture SIDECAR that uploads the
resulting directory with `storage.Client()`.

    pip:      exit 0, five packages
    sidecar:  OSError: Project was not passed and could not be
              determined from the environment

`storage.Client()` is built with no arguments ON PURPOSE -- Workload
Identity -- so the project id and the token both come from
169.254.169.254. The Job wore `agentos.io/egress: proxy`, whose policy
admits `egress-proxy:3128` and DNS and nothing else.

So the archive came back empty, `build_deps` returned `ok=False`, and
every criterion became `error` -- correctly, since a check that was never
given its dependencies did not run and found nothing wrong. Three
retries, then back to the executor, which was asked to repair work that
was never broken. Execution 205 looped VERIFYING -> RUNNING until it was
cancelled by hand.

THE HALF-FIX THAT CAME BEFORE. Execution 202 hit the same OSError and the
answer was `NO_PROXY` naming 169.254.169.254, so the SDK would stop
sending a link-local address through squid. That was necessary and not
sufficient: NO_PROXY decides whether a client proxies an address, and the
NetworkPolicy decides whether the packet is allowed at all. The variable
was fixed, the rule was not, and the symptom did not change -- which is
why this file asserts the POLICY and not the variable.

WHY A THIRD CLASS RATHER THAN ONE MORE RULE ON `proxy`. That label also
selects the EXECUTION pod, and the sandbox there shares the pod's network
namespace: metadata would let model-authored code mint a Workload
Identity token for `agentos-execution`. That is precisely the property
the two-uid pod design rests on. The capture Job already runs as a third
identity, `agentos-capture`, so the grant now follows a boundary that had
already been drawn.

AND IT IS A WIDENING. The package list being replayed is model-chosen,
and a hostile package runs its own code at install time. What that code
could reach is a token for `agentos-capture` -- so the containment is
that account's IAM, and the last test here records what it has to be.
"""
from __future__ import annotations

import pytest

yaml = pytest.importorskip("yaml")

from kernel.drivers.k8s_egress import (LABEL, VIA_CAPTURE,   # noqa: E402
                                       VIA_PROXY, VIA_STORE)

METADATA = "169.254.169.254/32"


def _policies():
    from app.services.tests.test_k8s_spec import _rendered

    return {d["metadata"]["name"]: d
            for d in yaml.safe_load_all(_rendered())
            if d and d["kind"] == "NetworkPolicy"}


def _reaches_metadata(policy) -> bool:
    for rule in policy["spec"].get("egress") or []:
        for to in rule.get("to") or []:
            if (to.get("ipBlock") or {}).get("cidr") == METADATA:
                return True
    return False


def _selector(policy) -> str:
    return policy["spec"]["podSelector"]["matchLabels"][LABEL]


# ── the rule that was missing ──────────────────────────────────────────

def test_there_is_a_policy_for_the_capture_class():
    policies = _policies()
    assert "capture-egress" in policies, (
        "the capture Job has no policy of its own, so it falls back to "
        "default-deny and its sidecar cannot authenticate at all")
    assert _selector(policies["capture-egress"]) == VIA_CAPTURE


def test_the_capture_class_may_reach_the_metadata_server():
    """The whole point. Without this the install succeeds and the upload
    dies, which reads as a broken story rather than a broken cluster."""
    assert _reaches_metadata(_policies()["capture-egress"])


def test_the_capture_class_still_has_the_full_port():
    """It runs the tree's own setup script and a package install, so the
    metadata rule is IN ADDITION to the proxy, never instead of it."""
    ports = [p["port"] for rule in _policies()["capture-egress"]["spec"]["egress"]
             for p in rule.get("ports") or []]
    assert 3128 in ports


# ── and the containment it must not cost ───────────────────────────────

def test_the_execution_pod_is_still_refused_the_metadata_server():
    """THE REASON THIS IS A SEPARATE POLICY, and the assertion that
    stops somebody 'simplifying' it back into one.

    `workload-egress-via-proxy` selects the execution pod, whose sandbox
    shares the network namespace. A token minted there is a token
    model-authored code holds."""
    policy = _policies()["workload-egress-via-proxy"]
    assert _selector(policy) == VIA_PROXY
    assert not _reaches_metadata(policy), (
        "the sandbox shares this pod's network namespace -- metadata "
        "here lets model-authored code mint a Workload Identity token")


def test_a_criterion_is_still_the_narrowest_class_of_all():
    """A criterion may reach the store to fetch the tree it checks, and
    the forge never: one that can reach the internet can fetch a version
    of itself that passes."""
    policy = _policies()["verifier-egress-to-store"]
    assert _selector(policy) == VIA_STORE
    ports = [p["port"] for rule in policy["spec"]["egress"]
             for p in rule.get("ports") or []]
    assert 3128 not in ports, "the criterion has the executor's reach"


def test_the_three_classes_are_three_distinct_policies():
    """One label per policy. Two classes sharing a selector is how the
    capture Job inherited a restriction written for the execution pod in
    the first place."""
    selectors = [_selector(_policies()[name]) for name in
                 ("workload-egress-via-proxy", "verifier-egress-to-store",
                  "capture-egress")]
    assert sorted(selectors) == sorted({VIA_PROXY, VIA_STORE, VIA_CAPTURE})


# ── and it has to hold for every caller, not just the one measured ────

def test_every_caller_verifies_through_one_function():
    """WHY ONE POLICY COVERS EVERY CALLER. Nothing is a second
    implementation of verification: each caller goes through
    `verify_claim`, so they share the env replay, the setup preparation,
    and therefore this egress class.

    Asserted because the reverse would be invisible: a caller that grew
    its own container path would keep passing every test here while
    hitting the exact OSError on a story nobody ran twice.

    THE DRY RUN WAS ONE OF THE THREE and it is gone (E9, plan 5.6). The
    inert probe takes its place in this list -- it is the other caller
    that runs criteria against a tree that is not the claim's."""
    import pathlib

    from kernel.verifier import adjudicator, inert

    for module in (adjudicator, inert):
        src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "verify_claim(" in src, (
            f"{module.__name__} no longer verifies through verify_claim; "
            f"it has a second path, and this file only covers the first")


def test_both_kinds_of_capture_job_travel_the_same_seam():
    """There are TWO, and only one of them was measured on 205.

      * `build_deps` replays the executor's recorded installs.
      * `prepare._prepare_mount` runs a committed manifest's setup --
        the `requirements.txt` path, which is I3 and is hit far more
        often than a mid-run install.

    Both reach `driver.run_ephemeral_capture`, so both get the label
    from `capture_manifest`. If either grew its own manifest builder it
    would need its own policy, and would not have one."""
    import pathlib

    from kernel.verifier import environment, prepare

    for module in (environment, prepare):
        src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "driver.run_ephemeral_capture(" in src, (
            f"{module.__name__} builds capture pods some other way")


def test_the_capture_label_is_named_once():
    """The label and the proxy address are one decision, and the whole
    of `k8s_egress` exists because they were spelled apart. Only
    `capture_manifest` may name the class for this pod; `run_capture`
    derives its env from the same constant."""
    import pathlib

    from kernel.drivers import k8s_capture, k8s_ephemeral

    manifest = pathlib.Path(k8s_capture.__file__).read_text(encoding="utf-8")
    runner = pathlib.Path(k8s_ephemeral.__file__).read_text(encoding="utf-8")
    assert "LABEL: VIA_CAPTURE" in manifest
    assert "fetch_env(VIA_CAPTURE)" in runner
    # And not the executor's class, which is the thing being moved away
    # from. Comment lines stripped: the note above each of these EXPLAINS
    # the old value, and an absence assertion will happily fire on the
    # sentence saying why it is absent.
    code = chr(10).join(
        line for line in (manifest + chr(10) + runner).splitlines()
        if not line.lstrip().startswith("#"))
    assert "VIA_PROXY" not in code, (
        "the capture Job is back on the execution pod's egress class")


def test_every_class_the_proxy_is_dialled_on_is_admitted_on_that_port():
    """BOTH SIDES OF ONE CONNECTION, and the first draft of this change
    only did one of them.

    `capture-egress` let the new class OUT to egress-proxy:3128, and
    `egress-proxy-ingress` still admitted 3128 from `proxy` alone. The
    packet leaves and is dropped on arrival -- and a default-deny drop
    does not reset, so `pip install` HANGS instead of failing. That is
    worse than the OSError the change exists to remove: a clean error
    took an afternoon to trace, and a hang takes the leg's whole wall
    clock and looks like a slow package index.

    Stated over every class rather than for `capture`, so a fourth one
    cannot repeat it."""
    policies = _policies()
    ingress = policies["egress-proxy-ingress"]

    admitted = set()
    for rule in ingress["spec"]["ingress"]:
        for src in rule.get("from") or []:
            label = (src.get("podSelector") or {}).get(
                "matchLabels", {}).get(LABEL)
            for port in rule.get("ports") or []:
                admitted.add((label, port["port"]))

    for name, policy in policies.items():
        if not name.endswith(("-egress", "egress-via-proxy")):
            continue
        klass = policy["spec"]["podSelector"]["matchLabels"].get(LABEL)
        if klass is None:
            continue
        for rule in policy["spec"].get("egress") or []:
            to_proxy = any((t.get("podSelector") or {}).get(
                "matchLabels", {}).get("app") == "egress-proxy"
                for t in rule.get("to") or [])
            if not to_proxy:
                continue
            for port in rule.get("ports") or []:
                assert (klass, port["port"]) in admitted, (
                    f"{klass!r} may dial the proxy on {port['port']} but "
                    f"egress-proxy-ingress does not admit it from that "
                    f"class -- the packet is dropped, and a drop hangs")


def test_the_capture_identity_is_not_the_execution_identity():
    """The blast radius of the widening above is whatever
    `agentos-capture` can do, so it must not be a reused account.

    The ServiceAccount OBJECTS are applied out of band -- they carry
    Workload Identity annotations binding them to distinct GCP accounts
    and are not rendered from this file. What this manifest decides is
    which name each Job is given, and that is what is asserted: three
    names, all different. A capture Job handed `agentos-execution` would
    reach the metadata server AS the execution identity, which is the
    containment above undone by a copied line."""
    from app.services.tests.test_k8s_spec import _rendered

    wanted = ("AGENTOS_K8S_SERVICE_ACCOUNT",
              "AGENTOS_K8S_VERIFIER_SERVICE_ACCOUNT",
              "AGENTOS_K8S_CAPTURE_SERVICE_ACCOUNT")
    env = {}
    for doc in yaml.safe_load_all(_rendered()):
        if not doc or doc["kind"] != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for item in container.get("env") or []:
                if item["name"] in wanted:
                    env[item["name"]] = item.get("value")

    assert set(env) == set(wanted), (
        f"a Job identity is unset; found {sorted(env)}")
    assert len(set(env.values())) == 3, (
        f"the three Job identities are not distinct: {env}")
