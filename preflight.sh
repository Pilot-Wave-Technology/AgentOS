#!/usr/bin/env bash
#
# ./preflight.sh — is this installation actually provisioned?
#
#   ./preflight.sh                 # check everything
#   PROJECT=my-proj ./preflight.sh # if gcloud's default is not it
#
# Exit 0 means every access, role, grant, secret and label AgentOS
# needs is present. Exit 1 means something is missing OR could not be
# determined, and it prints which, and the command that fixes it.
#
# ── RUN IT BEFORE AND AFTER EVERY STAGE OF THE DEPLOY ────────────────
#
# Not once at the end. Each file in deploy/ says where it belongs. The
# cost of running it is seconds; the cost of not running it is a
# deployment that comes up, serves some requests, and fails on the ones
# you did not test.
#
# ── WHY THIS EXISTS ──────────────────────────────────────────────────
#
# Standing up a second environment beside the first found eleven things
# that were not in the repository at all: they had been done by hand,
# once, years earlier, and nothing noticed they were missing because
# nothing ever asked. Every one of them shipped a GREEN BUILD. The test
# suite passes identically against an installation that cannot serve a
# request, because none of this is in the code.
#
# The failures are quiet and they are specific. A missing
# default-privileges rule means one endpoint 500s after some LATER
# deploy adds a table. A missing ASSISTANT_API_TOKEN reports itself as
# E_KERNEL_UNREACHABLE, which reads as a network problem. A missing
# node taint means another workload's pods land on your nodes and the
# only symptom is that things got slower.
#
# ── "COULD NOT ASK" IS A FAILURE, NOT A PASS ─────────────────────────
#
# Every check has three outcomes rather than two: ok, MISS, and
# UNKNOWN. Four separate defects during one build had the same shape --
# a question that could not fail visibly answered "no" -- so a check
# that cannot be RUN is not a check that passed. UNKNOWN is fatal and
# prints differently, so you do not spend an afternoon debugging the
# wrong thing. See scripts/infra/preflight/report.sh.
#
# ── WHAT THIS WRAPPER ADDS ───────────────────────────────────────────
#
# The checks live in scripts/infra/, where they are shared with
# multi-environment installations. This sets the one thing a
# single-environment install has to say -- that its resources carry no
# environment suffix, so the checker asks for `database-url` and
# `control_panel` rather than `<env>-database-url` and
# `control_panel_<env>`. Getting that wrong does not fail quietly: it
# reports a correct installation as almost entirely MISSING.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# ONE ENVIRONMENT, SO NO SUFFIXES. prerequisites.sh creates
# `database-url`, `control_panel` and `<prefix>-db`; names.sh produces
# exactly those names under this flag. Set it to empty only if you
# deliberately run several AgentOS environments in one project and
# named their resources accordingly.
export AGENTOS_SINGLE_ENV="${AGENTOS_SINGLE_ENV-1}"

# The deployment's name. It is the Kubernetes namespace and the
# Artifact Registry repository; it is NOT a suffix on anything while
# AGENTOS_SINGLE_ENV is set. Keep it the same as `name_prefix` in
# terraform.tfvars.
NAME_PREFIX="${NAME_PREFIX:-agentos}"
export NAME_PREFIX

export PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
export REGION="${REGION:-us-central1}"

if [ -z "${PROJECT:-}" ] || [ "$PROJECT" = "(unset)" ]; then
  echo "ERROR: no project. Either run" >&2
  echo "         gcloud config set project YOUR_PROJECT" >&2
  echo "       or pass it:" >&2
  echo "         PROJECT=YOUR_PROJECT ./preflight.sh" >&2
  echo "" >&2
  echo "       Not guessing is the point: a preflight pointed at the" >&2
  echo "       wrong project reports a perfectly good installation as" >&2
  echo "       entirely missing." >&2
  exit 1
fi

CHECKS="${HERE}/scripts/infra/env_preflight.sh"
if [ ! -f "$CHECKS" ]; then
  echo "ERROR: ${CHECKS} not found." >&2
  echo "       Run this from the root of your AgentOS checkout." >&2
  exit 1
fi

exec bash "$CHECKS" "$NAME_PREFIX"
