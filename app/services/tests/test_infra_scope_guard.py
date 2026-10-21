"""The infra pipeline cannot reach another environment.

WHY THIS GUARD GETS TESTS. It is the thing standing between a one-word
edit and production, and it is the kind of check that is easy to write in
a way that passes everything. Its failure mode is silence: a guard that
never refuses looks exactly like a pipeline that is correctly scoped.

Every case below is one a substitution could actually produce.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

GUARD = (pathlib.Path(__file__).resolve().parents[3]
         / "scripts" / "check_infra_scope.py")


def _guard():
    spec = importlib.util.spec_from_file_location("_infra_scope", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_real_target_set_passes():
    """A guard that refuses the correct build is one someone disables."""
    g = _guard()
    assert not g.offences("next", [
        "control_panel_next", "control-panel-staging", "next-database-url",
        "credential-encryption-key-next", "github-app-key",
        "firebase-service-account", "control-panel-db"])


@pytest.mark.parametrize("target", [
    "control-panel-prod",
    "prod-database-url",
    "control-panel-dev",
    "credential-encryption-key-dev",
])
def test_another_environments_resource_is_refused(target):
    g = _guard()
    bad = g.offences("next", [target])
    assert bad and "scoped to" in bad[0], bad


def test_a_name_carrying_both_environments_is_refused():
    """THE CASE A NAIVE CHECK PASSES. `control-panel-prod-staging` contains
    "next", so a guard that only looked for the right word would wave it
    through — while the resource it names is production's."""
    g = _guard()
    bad = g.offences("next", ["control-panel-prod-staging"])
    assert bad and "prod" in bad[0], bad


def test_an_unscoped_name_is_refused():
    """A resource that does not name its environment cannot be checked,
    and 'cannot be checked' must not read as 'fine'."""
    g = _guard()
    bad = g.offences("next", ["control-panel"])
    assert bad and "not scoped" in bad[0], bad


def test_the_pipeline_refuses_to_build_production_at_all():
    """The outer bound, and the reason BUILDABLE is a list rather than
    "whatever the caller passes". Even with every target correctly
    named, a pipeline may only build an environment on that list — and
    prod is deliberately not on it, whatever a trigger says.

    `dev` JOINED THE LIST when dev got an execution plane of its own.
    That widened what a typo can reach by exactly one environment and
    left the sentence above true of production, which is the one this
    guard was written for.
    """
    g = _guard()
    bad = g.offences("prod", ["control-panel-prod"])
    assert bad and "may build" in bad[0], bad


def test_dev_is_buildable_and_its_real_targets_pass():
    """The same assertion `test_the_real_target_set_passes` makes for
    `next`, for the environment that joined it.

    A guard that refuses the correct build is one someone disables, and
    dev's names are the ones most likely to trip the second rule by
    accident: every one of them has to carry `dev` as a word and none of
    them may carry another environment's.
    """
    g = _guard()
    assert not g.offences("dev", [
        "control-panel-dev", "agent-management-dev", "council-worker-dev",
        "git-engine-dev", "scheduler-dev", "task-runner-dev",
        "agentos-dev", "dev-database-url", "dev-database-url-conductor",
        "credential-encryption-key-dev", "agentos-dev-artifacts",
        "firebase-service-account", "control-panel-db"])


def test_feature_is_buildable_and_its_real_targets_pass():
    """The third environment on the shared cluster and instance. Its
    names must carry `feature` as a word and nobody else's."""
    g = _guard()
    assert not g.offences("feature", [
        "control-panel-feature", "agent-management-feature",
        "council-worker-feature", "git-engine-feature", "scheduler-feature",
        "task-runner-feature", "agentos-feature", "feature-database-url",
        "feature-database-url-conductor", "agentos-feature-artifacts",
        "firebase-service-account", "control-panel-db"])


def test_a_feature_build_may_not_touch_dev_or_next():
    g = _guard()
    for target in ("agentos-dev", "dev-database-url-conductor",
                   "agentos-staging", "control-panel-staging"):
        assert g.offences("feature", [target]), target


def test_a_dev_build_may_not_touch_next():
    """THE DIRECTION THAT DID NOT EXIST BEFORE. Until dev was buildable
    the guard only ever had to keep `next` away from everywhere else;
    now there are two pipelines that can reach the same cluster and the
    same Cloud SQL instance, and the interesting mistake is a dev build
    aimed at next's namespace or next's conductor DSN.
    """
    g = _guard()
    for target in ("agentos-staging", "next-database-url-conductor",
                   "control-panel-staging"):
        bad = g.offences("dev", [target])
        assert bad and "next" in bad[0], (target, bad)


def test_shared_resources_are_allowed_and_named():
    """firebase-service-account and the GitHub App are shared on purpose
    and carry no env scope, so the scoping rule would reject them for the
    wrong reason. They are read, never created or modified.

    The Cloud SQL INSTANCE is in the same position: `next` gets a new
    DATABASE on the instance prod and dev already share, and creating a
    database does not modify the instance.
    """
    g = _guard()
    assert not g.offences("next", sorted(g.SHARED))


def test_substring_matches_do_not_count_as_scope():
    """`next` inside another word is not a scope. Without this, the
    word-boundary logic could be dropped and every test above would
    still pass."""
    g = _guard()
    assert g._word("control-panel-staging", "next")
    assert g._word("next-database-url", "next")
    assert not g._word("nextdoor-service", "next")


def test_an_outsiders_environment_is_buildable_once_declared(monkeypatch):
    """The allowed set is ours by default, and theirs when they say so.

    The list used to be the literal `("next", "dev", "feature")`, so an
    installation that is not ours failed on step one of its first infra
    build -- the guard refusing the only environment it was ever aimed
    at, and saying the fix was to use that environment's own pipeline,
    which was the pipeline it had just refused.
    """
    monkeypatch.setenv("AGENTOS_BUILDABLE_ENVS", "staging, myenv")
    g = _guard()
    assert g.BUILDABLE == ("staging", "myenv")
    assert not g.offences("myenv", ["agentos-myenv", "myenv-database-url"])


def test_declaring_a_set_does_not_unlock_prod(monkeypatch):
    """The teeth survive the change, which is the whole question.

    Declaring an environment must not widen anything else: `prod` is
    buildable only if somebody writes `prod` into the variable on
    purpose, and a target scoped to a DIFFERENT environment is still
    refused inside a declared one.
    """
    monkeypatch.setenv("AGENTOS_BUILDABLE_ENVS", "myenv")
    g = _guard()
    assert g.offences("prod", ["control-panel-prod"])
    bad = g.offences("myenv", ["control-panel-myenv-prod"])
    assert bad and "prod" in bad[0], bad


def test_the_declared_environment_is_recognisable_as_a_scope(monkeypatch):
    """A buildable env must also be a KNOWN env.

    If it were not folded in, a target scoped to the operator's own
    environment would read as carrying no scope at all, and the SHARED
    exemption would be the only thing keeping the build alive.
    """
    monkeypatch.setenv("AGENTOS_BUILDABLE_ENVS", "myenv")
    g = _guard()
    assert "myenv" in g.KNOWN_ENVS
    assert g.KNOWN_ENVS.count("myenv") == 1
    bad = g.offences("dev", ["agentos-myenv"])
    assert bad and "myenv" in bad[0], bad
