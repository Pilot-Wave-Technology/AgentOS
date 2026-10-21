"""A shell variable in a Cloud Build script must be written `$$NAME`.

THE DEFECT THIS CATCHES, found by pushing it. Cloud Build expands `$NAME`
as its OWN substitution before bash ever sees the script, so an inline
`$RUNNING` is not an empty variable at runtime — it is a build that never
starts:

    invalid value for 'build.substitutions':
    key in the template "RUNNING" is not a valid built-in substitution

No steps, no logs bucket, and `gcloud builds log` answers "Build does not
specify logsBucket" rather than anything about the cause. The only place
the reason appears is `statusDetail` on the build record.

WHY THIS IS A TEST AND NOT A BUILD STEP. The build cannot check itself:
it is rejected at parse time, before step one, so any guard living inside
the pipeline is a guard that cannot run for exactly the failure it exists
to catch. It has to be checked somewhere the config is read without being
executed — here, where `run.sh` and a laptop both reach it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# `$NAME` or `${NAME}`, but NOT the second `$` of an escaped `$$NAME`,
# and not `$(` command substitution, which Cloud Build leaves alone.
REFERENCE = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# What Cloud Build actually provides. Anything else without a leading
# underscore is refused, whatever the author meant by it.
BUILT_IN = {
    "PROJECT_ID", "PROJECT_NUMBER", "BUILD_ID", "LOCATION",
    "COMMIT_SHA", "SHORT_SHA", "REVISION_ID",
    "REPO_NAME", "REPO_FULL_NAME", "BRANCH_NAME", "TAG_NAME", "REF_NAME",
    "TRIGGER_NAME", "TRIGGER_BUILD_CONFIG_PATH",
    "SERVICE_ACCOUNT", "SERVICE_ACCOUNT_EMAIL",
}

ROOT = pathlib.Path(__file__).resolve().parents[3]
CONFIGS = sorted(ROOT.glob("cloudbuild*.yaml"))


def test_there_are_configs_to_check():
    """A glob that matches nothing passes every assertion below it."""
    assert CONFIGS, "no cloudbuild*.yaml found; this suite is checking air"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_every_dollar_name_is_a_substitution_that_exists(config):
    """User substitutions start with `_`; everything else must be a
    built-in or an escaped shell variable."""
    text = config.read_text(encoding="utf-8")
    bad = sorted({
        name for name in REFERENCE.findall(text)
        if not name.startswith("_") and name not in BUILT_IN
    })
    assert not bad, (
        f"{config.name}: {', '.join('$' + b for b in bad)} will be read by "
        f"Cloud Build as a substitution and REFUSE THE BUILD before any "
        f"step runs. Write shell variables as $$NAME.")


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_every_underscore_substitution_is_declared(config):
    """`${_THING}` that no `substitutions:` block defines is empty at
    runtime, not an error — a step quietly builds `:latest` instead of
    the tag it meant, or deploys to a namespace called nothing."""
    import yaml

    text = config.read_text(encoding="utf-8")
    declared = set((yaml.safe_load(text).get("substitutions") or {}))
    used = {n for n in REFERENCE.findall(text) if n.startswith("_")}
    assert used <= declared, (
        f"{config.name}: {', '.join(sorted(used - declared))} used and not "
        f"declared in substitutions:")


def test_the_pattern_would_actually_have_caught_it():
    """The regex is the whole guard, so it is asserted directly rather
    than only through the files it happens to scan today."""
    caught = set(REFERENCE.findall('for IMG in $RUNNING; do echo "$IMG"'))
    assert {"RUNNING", "IMG"} <= caught

    # Escaped and built-in forms must NOT be reported.
    assert "RUNNING" not in REFERENCE.findall('echo "$$RUNNING"')
    assert REFERENCE.findall("${_IMAGE}:$COMMIT_SHA") == ["_IMAGE",
                                                          "COMMIT_SHA"]
    # Command substitution is Cloud Build's blind spot too, and must not
    # be flagged: `$(kubectl ...)` reaches bash intact.
    assert REFERENCE.findall("X=$(kubectl get pods)") == []
