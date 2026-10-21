#!/usr/bin/env bash
#
# report.sh — the three outcomes a check can have, and why there are
# three rather than two.
#
# THE WHOLE POINT OF THIS FILE IS `unknown`. Standing `dev` up beside
# `next` produced the same bug four separate times, in four unrelated
# places: a question that could not fail visibly answered "no".
#
#   * `env_label_of` piped gcloud to `python3`, which on Windows is the
#     Store stub. It read empty, empty read as "unlabelled", and four
#     correctly-labelled node pools were reported as broken.
#   * The execution-plane deploy took a missing label as permission to
#     fall back to unscoped pools -- so the failure mode of "I could
#     not tell" was "put dev's pods on next's nodes".
#   * A kernel tool call returned E_KERNEL_UNREACHABLE. The token was
#     missing; an AUTH failure was reported as a NETWORK one.
#   * A stale comment said a secret's absence "MEANS OFF, NOT BROKEN".
#     It meant broken: the pod called leg_dsn() unconditionally.
#
# In all four, code that could not establish a fact quietly substituted
# the negative and carried on. So: a check that cannot be RUN is not a
# check that passed, and it is not the same as a check that failed
# either -- the operator needs to know which. `unknown` is fatal like
# `miss`, and prints differently so nobody debugs the wrong thing.
#
# Sourced, not executed.

PF_MISS=0
PF_UNKNOWN=0
PF_OK=0

# ── WHICH COMMAND TO TELL THEM TO RUN ────────────────────────────────
#
# The checks are shared between our multi-environment installations and
# a single-environment one, and the two are driven by different
# commands. A `fix:` line naming `scripts/infra/gke_standard_pools.sh`
# is right for us and actively misleading for anyone else: run bare,
# without AGENTOS_SINGLE_ENV, it provisions under the suffixed names
# and the preflight goes on reporting exactly the same findings.
#
# A remedy that does not work is worse than no remedy. The operator
# runs it, nothing changes, and the natural conclusion is that the
# checker is broken.
fix_setup() {   # the execution plane: cluster, pools, identities, deploy
  if [ -n "${AGENTOS_SINGLE_ENV-}" ]; then printf './execution-plane.sh'
  else printf '%s' "$1"; fi
}
fix_secrets() { # secrets prerequisites.sh creates
  if [ -n "${AGENTOS_SINGLE_ENV-}" ]; then printf './prerequisites.sh'
  else printf '%s' "$1"; fi
}

# A REMEDY THAT CANNOT WORK IS WORSE THAN NONE, and `fix_secrets` was
# giving one. It sent EVERY missing secret to `prerequisites.sh`, but
# `execution-sqlproxy-key` is deliberately not created there: it is a
# key for a service account that does not exist until the execution
# plane has been stood up, and prerequisites.sh says so where it
# creates the other two. So the operator ran the suggested command,
# nothing changed, and the obvious conclusion was that the checker was
# broken.
fix_exec_secret() {
  if [ -n "${AGENTOS_SINGLE_ENV-}" ]; then printf './execution-plane.sh'
  else printf '%s' "$1"; fi
}

# ok WHAT DETAIL — the fact holds.
ok()      { PF_OK=$((PF_OK + 1));      printf '  [ ok ] %-10s %s\n' "$1" "${2-}"; }

# miss WHAT DETAIL [FIX] — the fact was established and is false.
miss()    { PF_MISS=$((PF_MISS + 1));  printf '  [MISS] %-10s %s\n' "$1" "${2-}"
            [ -n "${3-}" ] && printf '         fix: %s\n' "$3"; return 0; }

# skip WHAT DETAIL — deliberately not checked HERE; established elsewhere.
# Not counted, not fatal. Only for a check the running account is not meant
# to perform (a build account that has no business reading a key).
skip()    { printf '  [ -- ] %-10s %s\n' "$1" "${2-}"; return 0; }

# unknown WHAT DETAIL — the check could not be performed. FATAL.
unknown() { PF_UNKNOWN=$((PF_UNKNOWN + 1)); printf '  [ ?? ] %-10s %s\n' "$1" "${2-}"
            printf '         could not determine -- treating as failure\n'; return 0; }

# NO `cut`. It trimmed this to 72 COLUMNS by counting BYTES wherever the
# locale is C (every Linux build container), so it sliced the three-byte
# `─` in half and the whole report stopped being valid UTF-8 -- every
# reader that decodes it, the test included, fell over on a header.
section() { printf '\n── %s ──────────\n' "$*"; }

# pf_verdict — the exit status of the whole preflight.
pf_verdict() {
  printf '\n%d ok, %d missing, %d undetermined\n' "$PF_OK" "$PF_MISS" "$PF_UNKNOWN"
  if [ "$PF_MISS" -gt 0 ] || [ "$PF_UNKNOWN" -gt 0 ]; then
    printf 'PREFLIGHT FAILED -- refusing to deploy into an environment that is not fully provisioned.\n' >&2
    return 1
  fi
  printf 'PREFLIGHT PASSED\n'
}
