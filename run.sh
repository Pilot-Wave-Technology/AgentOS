#!/usr/bin/env bash
# Cumulative stage gate runner.
# Usage: ./run.sh stage-b | stage-c | ...
# Each stage runs everything up to and including itself.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESET_BUDGET=60   # seconds — B2 criterion

# ── S11: strip comments before source-pin comparison ─────────────────────────
# Usage: strip_comments < file.py
#   Removes Python/shell # comments so that:
#   - a comment edit does not fail a pin
#   - commenting code out cannot make a pin pass
strip_comments() {
    sed 's/#.*//'
}

# ── helpers ───────────────────────────────────────────────────────────────────

# container_bash — run a script inside a compose service, stripping Windows
# CRLF line endings first. Volume-mounted files from Windows have \r\n which
# breaks shebangs (bash\r is not found, sh runs instead, pipefail fails).
# Usage: container_bash <service> <script-path>
container_bash() {
    # EXACTLY TWO ARGUMENTS, AND THE SCRIPT MUST EXIST.
    #
    # This used to take "$1" and "$2" and ignore the rest, which made a
    # command look like a call it could serve:
    #
    #     container_bash control-plane python -m pytest some_test.py -q
    #
    # `script` is then "python", the body runs `tr -d '\r' < "python" |
    # bash`, tr writes "No such file or directory" to stderr, bash reads
    # an empty pipe, and the PIPELINE EXITS 0. The section printed its
    # heading, produced a stderr line nobody reads as fatal, and passed
    # having run nothing. Three call sites were doing exactly that —
    # G4b's token accounting in stage_b, and both Stage K invariant
    # suites — so those tests had not run in as long as they had been
    # written that way, while the gate reported them green.
    #
    # Both halves are needed. Counting the arguments catches the command
    # form; testing for the file catches a renamed or deleted script,
    # which fails the same silent way. Use container_run for a command.
    if [ "$#" -ne 2 ]; then
        echo "container_bash: takes <service> <script-path>, got $#: $*" >&2
        echo "  to run a command in the container, use container_run" >&2
        return 2
    fi
    local service="$1" script="$2"
    (cd "$REPO_ROOT/infra/e0" && \
        docker compose exec -T "$service" bash -c \
            "test -f \"$script\" || { echo \"container_bash: no such script: $script\" >&2; exit 127; }; tr -d '\r' < \"$script\" | bash")
}

# container_run — run a COMMAND inside a compose service.
#
# The counterpart to container_bash, and the reason it can now refuse a
# command instead of silently swallowing one. No CRLF stripping: there
# is no file to strip, and the arguments are passed through as given
# rather than through a shell, so nothing needs quoting twice.
container_run() {
    local service="$1"; shift
    (cd "$REPO_ROOT/infra/e0" && docker compose exec -T "$service" "$@")
}

section() {
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════════════"
}

# timed_reset — run e0.sh reset, fail if it exceeds RESET_BUDGET seconds.
# Usage: timed_reset <label>
timed_reset() {
    local label="$1"
    local start end elapsed
    start=$(date +%s)

    section "$label"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh reset)

    end=$(date +%s)
    elapsed=$((end - start))
    echo "  ⏱  ${label}: ${elapsed}s (budget: ${RESET_BUDGET}s)"
    if [ "$elapsed" -gt "$RESET_BUDGET" ]; then
        echo "FATAL: ${label} took ${elapsed}s — exceeds ${RESET_BUDGET}s budget" >&2
        exit 1
    fi
}

# ── Stage B ───────────────────────────────────────────────────────────────────
# Chains: preflight, CI lint, timed resets with marker proof, proxy test,
#         schema suite, unit suite, teardown.

stage_b() {
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                     Stage B gate                           ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # ── 1. Docker version preflight (host-side) ──────────────────────────
    section "B1 · Docker version preflight (>= 29.7.2)"
    bash "$REPO_ROOT/infra/e0/preflight.sh" --version-only

    # ── 1b. Preflight: build EVERY image, outside the timed section ─────
    # The reasoning below was already written here and applied to one
    # image out of three.
    #
    # "Runs OUTSIDE the timed section so a fresh-checkout build does not
    # blow B2's 60-second reset budget." True, and it built `executor`
    # only. A change to infra/e0/Dockerfile — the control plane — leaves
    # its image stale, so the FIRST `e0.sh reset` rebuilds it inside the
    # timed window and B2 fails with "took 85s, exceeds 60s". Which it
    # did, the first time that Dockerfile changed.
    #
    # The budget is about whether a RESET is fast enough to be used
    # dozens of times a day. It was never about whether an image builds
    # quickly, and measuring one as the other makes the gate fail for a
    # reason it has no opinion about.
    #
    # `docker compose build` with no service builds everything with a
    # `build:` section and is a no-op for the rest. Guarded because
    # executor/ may not exist on pre-E branches.
    if [ -d "$REPO_ROOT/executor" ]; then
        section "B1 · Image preflight build (all services)"
        (cd "$REPO_ROOT/infra/e0" && docker compose build)
    fi

    # ── 2. CI lint: no environ.get/getenv with defaults ──────────────────
    section "B1 · environ.get CI lint"
    # S10 Python lint: infra/ and kernel/
    local lint_paths=("$REPO_ROOT/infra/" "$REPO_ROOT/kernel/")
    bash "$REPO_ROOT/infra/e0/ci/check_no_environ_default.sh" "${lint_paths[@]}"

    # S10 TypeScript lint: executor/ excluding vendored src/agents/sandbox/
    # Guard: executor/ may not exist on pre-E branches.
    if [ -d "$REPO_ROOT/executor" ]; then
        section "B1 · S10 TypeScript lint (executor/)"
        # process.env.X ?? 'default' is the S10 bug in TypeScript:
        # empty string is non-nullish, so ?? returns "" while || gives the default.
        # Vendored code in executor/src/agents/sandbox/ is exempt.
        local ts_s10_hits
        ts_s10_hits=$(grep -rn 'process\.env\.\w*\s*??' "$REPO_ROOT/executor/" \
            --include='*.ts' \
            --exclude-dir='node_modules' \
            | grep -v 'src/agents/sandbox/' || true)
        if [ -n "$ts_s10_hits" ]; then
            echo "FATAL: S10 violation — process.env.X ?? found in executor/ (non-vendored):" >&2
            echo "$ts_s10_hits" >&2
            echo "" >&2
            echo "Use process.env.X || 'default' instead (empty string is falsy with ||)." >&2
            exit 1
        fi
        echo "  ✓ No process.env ?? in executor/ (vendored src/agents/sandbox/ exempt)"
    fi

    # ── 3. First reset (brings up the stack fresh) ───────────────────────
    timed_reset "B2 · Reset 1 of 2"

    # ── 4. Marker plant — prove the volume exists before the reset ───────
    #
    # A FILE IN THE WORKSPACE VOLUME, not a table in a database.
    #
    # This planted `_reset_marker` in E0's own Postgres via
    # `docker compose exec -T db psql`. Commit 548239c0 — "E0 has no
    # database of its own" — deleted the db and migrate services on
    # 7 September; E0 connects out to the control panel's Postgres now.
    # So the step failed with `service "db" is not running`, and because
    # run.sh is `set -euo pipefail` and every gate is cumulative, stage_c
    # through stage_l could not reach a single one of their own
    # assertions from that day on. The chain was not failing, it was not
    # running.
    #
    # The PROPERTY is untouched and still worth asserting: `e0.sh reset`
    # is `docker compose down -v`, and a `-v` that quietly stopped
    # dropping volumes would leave every later stage running against
    # whatever the last one left behind — which is the failure mode this
    # gate exists to rule out before anything else runs.
    #
    # e6_scratch is what there is to drop: a named volume mounted at
    # /workspace in the control plane. The control panel's database is
    # deliberately NOT dropped by a reset and must never be — it is not
    # E0's to reset.
    section "B2 · Marker plant"
    container_run control-plane \
        sh -c 'echo planted > /workspace/_reset_marker'
    echo "  ✓ /workspace/_reset_marker planted"

    # ── 4b. Prove the query works before relying on its absence ──────────
    #   If this fails, the gate fails — we never trust a check we have
    #   not seen succeed. An absence proves nothing until the presence
    #   has been observed by the same means.
    local marker_before
    marker_before=$(container_run control-plane \
        sh -c 'test -f /workspace/_reset_marker && echo t || echo f')
    marker_before=$(echo "$marker_before" | tr -d '[:space:]')
    if [ "$marker_before" != "t" ]; then
        echo "FATAL: /workspace/_reset_marker not found after planting — check path broken" >&2
        exit 1
    fi
    echo "  ✓ marker confirmed present (check path works)"

    # ── 5. Second reset (must drop the volume, killing the marker) ───────
    timed_reset "B2 · Reset 2 of 2"

    # ── 6. Marker check — the file must be gone ──────────────────────────
    #   No || true: a failure to look fails the gate rather than being
    #   indistinguishable from "marker gone".
    section "B2 · Marker proof"
    local marker_after
    marker_after=$(container_run control-plane \
        sh -c 'test -f /workspace/_reset_marker && echo t || echo f')
    marker_after=$(echo "$marker_after" | tr -d '[:space:]')
    if [ "$marker_after" = "t" ]; then
        echo "FATAL: /workspace/_reset_marker survived reset — volume was NOT dropped" >&2
        exit 1
    fi
    echo "  ✓ marker gone — the volume was dropped"

    # ── 7. Proxy allowlist test (5 checks) ───────────────────────────────
    section "B3 · Socket proxy allowlist test"
    container_bash control-plane ci/test_proxy.sh

    # ── 8. Schema assertion suite (empty stub — runs inside container) ───
    section "Schema assertion suite"
    container_bash control-plane ci/test_schema.sh

    # ── 9. Unit test suite (empty stub — runs inside container) ──────────
    section "Unit test suite"
    container_bash control-plane ci/test_unit.sh

    # ── 10. Teardown ─────────────────────────────────────────────────────
    section "G4b · Token accounting, and the rates record"
    container_run control-plane python3 ci/test_unit_usage.py
    container_bash control-plane ci/check_pricing_record.sh

    section "G7b · Provider conformance — one seam, every adapter"
    container_bash control-plane ci/test_g7b_conformance.sh

    section "Fb4b · Provider fault tolerance"
    container_bash control-plane ci/test_fb4b_faults.sh
    container_bash control-plane ci/test_fb4b_admission.sh

    section "G7 · The substrate axis — one driver, one seam"
    container_bash control-plane ci/test_g7_driver_axis.sh

    section "G7c · Delegation and the parent's context"
    container_bash control-plane ci/test_g7c_delegation.sh

    section "G9 · Recovery against the harness that ships"
    container_bash control-plane ci/test_g9_recovery_real.sh

    section "H · Conductor, HITL, projections"
    container_bash control-plane ci/test_h1b_contract_config.sh
    container_bash control-plane ci/test_h2_hitl_policy.sh
    container_bash control-plane ci/test_h5_batching.sh
    container_bash control-plane ci/test_h6_hot_park.sh
    container_bash control-plane ci/test_h7_stall.sh
    container_bash control-plane ci/test_h3h4_projection_addressing.sh

    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                  Stage B gate: GREEN                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

# ── Stage C ───────────────────────────────────────────────────────────────────
# Cumulative: runs stage_b first, then Stage C's own checks.
# C1 lands schema assertions in test_schema.sh (already in stage_b).
# C2–C7 add their checks here.

stage_c() {
    # ── cumulative: everything through B ───────────────────────────────────
    stage_b

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                     Stage C gate                           ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # ── bring the stack up fresh for C-specific checks ─────────────────────
    timed_reset "C · Stack reset"

    # ── C-specific checks (C3–C7 add here) ─────────────────────────────────
    # C1's schema assertions already run in stage_b via test_schema.sh.

    # ── C2 · Null executor ─────────────────────────────────────────────────
    section "C2 · Null executor"
    container_bash control-plane ci/test_null_executor.sh

    # ── C3 · Docker driver ───────────────────────────────────────────────
    section "C3 · Docker driver"
    container_bash control-plane ci/test_docker_driver.sh

    # ── C4 · Conductor ─────────────────────────────────────────────────
    section "C4 · Conductor"
    container_bash control-plane ci/test_conductor.sh

    # ── C6 · Reconcile ───────────────────────────────────────────────────
    section "C6 · Reconcile"
    container_bash control-plane ci/test_reconcile.sh

    # ── C7 · Generation fencing ──────────────────────────────────────────
    section "C7 · Generation fencing"
    container_bash control-plane ci/test_c7.sh

    # ── Teardown ───────────────────────────────────────────────────────────
    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                  Stage C gate: GREEN                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

# ── Stage D ───────────────────────────────────────────────────────────────────
# Cumulative: runs stage_c first, then Stage D's own checks.

stage_d() {
    # ── cumulative: everything through C ───────────────────────────────────
    stage_c

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                     Stage D gate                           ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # ── bring the stack up fresh for D-specific checks ─────────────────────
    timed_reset "D · Stack reset"

    # ── D0 · Chaos suite ─────────────────────────────────────────────────
    section "D0 · Chaos suite"
    container_bash control-plane ci/test_chaos.sh

    # ── Teardown ───────────────────────────────────────────────────────────
    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                  Stage D gate: GREEN                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

# ── Stage E ───────────────────────────────────────────────────────────────────
# Cumulative: runs stage_d first, then Stage E's own checks.
# E1: standalone create/exec/destroy, rule 10 validation.
# E2: worker/sandbox credential boundary.
# E3: harness predicates (token budget, step budget, lease clock, snapshots).
# E4: interceptor chain (6 stages, corrective denials).
# E5: model loop + propose_completion.
# E5b: model-provider seam (LR1-LR8 + normaliser conformance).

stage_e() {
    # ── cumulative: everything through D ───────────────────────────────────
    stage_d

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                     Stage E gate                           ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # ── E5b preflight: API keys required for live adapter tests ──────────
    # E0 stays cloud-free (GOAL_BRIEF §9) — no gcloud calls here.
    # Keys are fetched once by a human from Secret Manager
    # (claude-api-key-feature, gemini-api-key-feature) and written to
    # infra/e0/.env, which docker compose auto-loads.
    if [ -f "$REPO_ROOT/infra/e0/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        . "$REPO_ROOT/infra/e0/.env"
        set +a
    fi
    if [ -z "${ANTHROPIC_API_KEY:-}" ] || [ -z "${GEMINI_API_KEY:-}" ]; then
        echo "FATAL: ANTHROPIC_API_KEY or GEMINI_API_KEY not set." >&2
        echo "  E5b makes live adapter calls to discharge LR1, LR4 and test 10." >&2
        echo "  Write infra/e0/.env with both keys — see the ES3 cycle." >&2
        exit 1
    fi

    # ── P3 · provider_state.raw leak check (static, no container) ──────────
    section "P3 · provider_state.raw leak check"
    bash "$REPO_ROOT/infra/e0/ci/check_no_provider_state_leak.sh" \
        "$REPO_ROOT/kernel/" "$REPO_ROOT/infra/" "$REPO_ROOT/executor/"

    # ── bring the stack up fresh for E-specific checks ─────────────────────
    timed_reset "E · Stack reset"

    # ── E1 · Standalone executor ──────────────────────────────────────────
    section "E1 · Standalone executor"
    container_bash executor ci/test_e1.sh

    # ── E1 · Module-load guard ────────────────────────────────────────────
    section "E1 · Module-load guard"
    container_bash executor ci/test_e1_load.sh

    # ── E1 · TypeScript type-check ────────────────────────────────────────
    section "E1 · TypeScript type-check (tsc --noEmit)"
    container_bash executor ci/test_e1_typecheck.sh

    # ── E2 · Credential boundary ──────────────────────────────────────────
    section "E2 · Credential boundary"
    container_bash executor ci/test_e2.sh

    # ── E3 · Harness predicates ────────────────────────────────────────────
    section "E3 · Harness predicates"
    container_bash control-plane ci/test_e3.sh

    # ── E4 · Interceptor chain (Python, ported from TS in cycle 1b) ────────
    section "E4 · Interceptor chain"
    container_bash control-plane ci/test_e4.sh

    # ── E5 · Model loop + propose_completion ─────────────────────────────
    section "E5 · Model loop + propose_completion"
    container_bash control-plane ci/test_e5.sh

    # ── E5 · Module-load guard (kernel/) ───────────────────────────────
    section "E5 · Module-load guard (kernel/)"
    container_bash control-plane ci/test_e5_load.sh

    # ── E5b · Model-provider seam ────────────────────────────────────────
    section "E5b · Model-provider seam (LR1-LR8 + normaliser)"
    container_bash control-plane ci/test_e5b.sh

    # ── E5 · Live end-to-end (real bridge) ───────────────────────────────
    section "E5 · Live end-to-end (real bridge)"
    container_bash control-plane ci/test_e5_live.sh

    # ── P4 · Interceptor chain conformance table ──────────────────────
    section "P4 · Chain conformance table"
    container_bash control-plane ci/test_e5_chain_conformance.sh

    # ── P4 · Interceptor chain wiring ──────────────────────────────────
    section "P4 · Interceptor chain wiring"
    container_bash control-plane ci/test_e5_chain.sh

    # ── P5 · story_commit ───────────────────────────────────────────────
    section "P5 · story_commit"
    container_bash control-plane ci/test_e5_commit.sh

    # ── P1 · Real adapter live tests ───────────────────────────────────
    section "P1 · Real adapter live tests"
    container_bash control-plane ci/test_e5_adapters.sh

    # ── P6 · Killable leg runner ────────────────────────────────────────
    section "P6 · Killable leg runner"
    container_bash control-plane ci/test_e5_kill.sh

    # ── 1d · Two-leg journal (generation in idem_key) ──────────────────
    section "1d · Two-leg journal"
    container_bash control-plane ci/test_e5_journal.sh

    # ── E6b · Brief generator ────────────────────────────────────────────
    section "E6b · Brief generator"
    section "E6e · Profiles (data-driven: prompts, skills, tools)"
    container_bash control-plane ci/test_profiles.sh

    section "E6f · Profile wiring (surface from the profile, not the caller)"
    container_bash control-plane ci/test_e6f_wiring.sh

    section "E7b · Sub-agent attenuation (child ⊆ parent)"
    container_bash control-plane ci/test_e7b_subagent.sh

    section "E7 · Artifact store (bounded outputs, kept in full)"
    container_bash control-plane ci/test_e7_artifacts.sh

    section "E7d · Workspace drivers (git is one binding)"
    container_bash control-plane ci/test_e7d_workspace.sh

    section "E6h/E6i · Park and resume (plan approval, ask_human)"
    container_bash control-plane ci/test_e6hi_park.sh

    section "E5c/E6d · Orientation floor and brief content"
    container_bash control-plane ci/test_e5c_e6d_brief.sh

    section "E8 · Chaos stub (every refusal reachable by accident)"
    container_bash control-plane ci/test_e8_chaos.sh

    section "Guard regressions (relative paths, cumulative floor)"
    container_bash control-plane ci/test_e9b_guard_regressions.sh

    section "E9 · The executor model, two legs vs a single-leg control"
    container_bash control-plane ci/test_e9_executor_model.sh

    container_bash control-plane ci/test_e6b_brief.sh

    # ── E6 · Rehydration ─────────────────────────────────────────────────
    section "E6 · Rehydration"
    container_bash control-plane ci/test_e6_rehydrate.sh

    # ── E6c · Progress memory ────────────────────────────────────────────
    section "E6c · Progress memory"
    container_bash control-plane ci/test_e6c_progress.sh

    # ── E6c · Progress memory, real model ──────────────────────────────
    section "E6c · Progress memory (real model)"
    container_bash control-plane ci/test_e6c_progress_live.sh

    # ── Teardown ───────────────────────────────────────────────────────────
    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                  Stage E gate: GREEN                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

stage_eb() {
    # ── cumulative: everything through E ───────────────────────────────────
    stage_e

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                     Stage Eb gate                          ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # stage_e tears the stack down before declaring itself green, so a
    # cumulative stage that follows it has nothing to exec into. Bring it
    # back up rather than making stage_e leave it running — a stage that
    # cleans up after itself is the property worth keeping.
    section "Bring the stack back up (stage_e tears it down)"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh up)

    section "Eb2 · How an errand ends (R3)"
    container_bash control-plane ci/test_eb2_errand_terminal.sh

    section "Eb6 ⛔ · The profile-diff proof"
    container_bash control-plane ci/test_eb6_profile_diff.sh

    section "Eb4 · Errands cross leg boundaries (live)"
    container_bash control-plane ci/test_eb4_errand_legs.sh

    section "H1a · The worker creates the real sandbox"
    container_bash control-plane ci/test_h1a_real_sandbox.sh

    section "H1c · Per-container bridge routing"
    container_bash control-plane ci/test_h1c_port_routing.sh

    section "H1 · The proposal mailbox (three intent vocabularies)"
    container_bash control-plane ci/test_h1_mailbox.sh

    section "H1 · The mailbox is read (steer, cancel, replan, answer)"
    container_bash control-plane ci/test_h1_intake.sh

    section "H1c · Admission slots come back; a run can be abandoned"
    container_bash control-plane ci/test_h1c_slots_and_cancel.sh

    section "H1d · One worker per leg; a dead leg still ends"
    container_bash control-plane ci/test_h1d_one_worker_per_leg.sh

    section "H0w · The worker loop (conductor to executor)"
    container_bash control-plane ci/test_h0w_worker_loop.sh

    section "H0w live · an errand, conductor to DONE"
    container_bash control-plane ci/smoke_h0w_live.sh

    section "H0v live · a story through the console, start to VERIFYING"
    container_bash control-plane ci/smoke_h0v_story.sh

    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                  Stage Eb gate: GREEN                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}


stage_f() {
    # ── cumulative: everything through Eb ──────────────────────────────────
    stage_eb

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                      Stage F gate                          ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # stage_eb tears the stack down before declaring itself green.
    section "Bring the stack back up (stage_eb tears it down)"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh up)

    section "F1 · Contracts, criteria, and who may write a verdict"
    container_bash control-plane ci/test_f1_contracts.sh

    section "F2 · The Verifier, and the attacks it must survive"
    container_bash control-plane ci/test_f2_verifier.sh

    section "F2b · The adjudicator — a story can reach DONE"
    container_bash control-plane ci/test_f2b_adjudicator.sh

    section "F2c · Releasing the compute of a VERIFYING execution"
    container_bash control-plane ci/test_f2c_release.sh

    section "F2d · Declared services, and the network that stays shut"
    container_bash control-plane ci/test_f2d_services.sh

    section "F3 · Four-valued verdicts and the flake quorum"
    container_bash control-plane ci/test_f3_quorum.sh

    section "F4 ⛔ · The reward-hacking trap set"
    container_bash control-plane ci/test_f4_traps.sh

    section "F5 · Reproducing the executor's environment"
    container_bash control-plane ci/test_f5_env.sh

    section "F5c · Committed setups define the environment"
    container_bash control-plane ci/test_f5c_setups.sh

    section "F6 · Exec/verify parity, reported per run"
    container_bash control-plane ci/test_f6_parity.sh

    section "F7 ⛔ · The Verifier against a real repository"
    container_bash control-plane ci/test_f7_real_repo.sh

    section "F12 · A human accepts a story over the Verifier"
    container_bash control-plane ci/test_f12_accept.sh

    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                   Stage F gate: GREEN                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}


stage_g() {
    # ── cumulative: everything through F ───────────────────────────────────
    stage_f

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                      Stage G gate                          ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # HOST-SIDE, and first. The socket proxy the control-plane reaches
    # Docker through has IMAGES: 0, so nothing inside the container can
    # check that the image every criterion runs in is the image the
    # recipe describes. If this is wrong, every result below it is
    # about an image nobody can rebuild.
    section "G0 · The fixture image has a recipe"
    bash "$REPO_ROOT/infra/e0/ci/check_fixture_recipe.sh"

    section "G0 · The fixture image is load-bearing"
    container_bash control-plane ci/test_g0_fixture_image.sh

    section "G1b · Did the tree affect this check at all?"
    container_bash control-plane ci/test_g1b_inert.sh

    section "G1 · The graded corpus, graded"
    container_bash control-plane ci/test_g1_corpus.sh

    section "G2 · Traps, and the mechanisms they must fire"
    container_bash control-plane ci/test_g2_traps.sh

    section "G3 · The metrics harness, on runs with known answers"
    container_bash control-plane ci/test_g3_metrics.sh

    section "G3b · Who made this commit"
    container_bash control-plane ci/test_g3b_identity.sh

    section "G4 · Tool failures are typed"
    bash "$REPO_ROOT/infra/e0/ci/check_typed_tool_errors.sh"

    section "G4 · Orientation tools and one error vocabulary"
    container_bash control-plane ci/test_g4_tools.sh

    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                   Stage G gate: GREEN                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

# ── dispatch ──────────────────────────────────────────────────────────────────

stage_h() {
    # ── cumulative: everything through G ───────────────────────────────────
    stage_g

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                      Stage H gate                          ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # WHY THIS GATE DID NOT EXIST UNTIL NOW, which is worth a paragraph
    # because the shape recurs. BUILD_ORDER specifies `./run.sh stage-h`
    # and nothing implemented it: `grep -c stage_h run.sh` returned 0.
    # Twenty-seven H suites were on disk, fifteen were wired into
    # stage_b and stage_eb -- gates named after other stages -- and the
    # twelve below were wired to nothing at all and had not run in
    # months.
    #
    # They were not obsolete. Eight passed on the first attempt. The
    # four that failed were failing for reasons that had nothing to do
    # with the properties they assert: two could not import
    # gateway_surface because it lived in app/ and E0 ships only
    # kernel/; one resolved the repo root by counting parent
    # directories, which lands on `/` when the tree is grafted at /app;
    # one matched reserved verbs by substring and could not tell
    # `ask_executor` from `exec`.
    #
    # Every one of those is the same failure: a check that cannot run
    # looks exactly like a check that passes, and the way it gets
    # resolved is that the suite quietly leaves the gate. That is why
    # these are here and not in a list somewhere.
    #
    # stage_g tears the stack down before declaring itself green, and
    # every suite below needs a database and a live control plane.
    section "Bring the stack back up (stage_g tears it down)"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh up)

    # ── the conductor half ─────────────────────────────────────────────
    section "H1e · A person changes what a story is for, at any point"
    container_bash control-plane ci/test_h1e_amend.sh

    section "H1f · A cancelled run releases its compute, whatever goal it is in"
    container_bash control-plane ci/test_h1f_container_reap.sh

    # ── the assistant surface ──────────────────────────────────────────
    section "H8 · The assistant's tools, and the acts it cannot perform"
    container_bash control-plane ci/test_h8_assistant_surface.sh

    section "H8a ⛔ · Approving a plan is a human act"
    container_bash control-plane ci/test_h8a_approval_authority.sh

    section "H8b · Two prompt files, zero skills, no tool that is gone"
    container_bash control-plane ci/test_h8b_prompt.sh

    section "H8c ⛔ · It reads projections; it does not drive execution"
    container_bash control-plane ci/test_h8c_no_driving.sh

    section "H8e · A card announces itself, without a person asking first"
    container_bash control-plane ci/test_h8e_notifier.sh

    section "H10 · The kernel tool surface over HTTP, and nothing else"
    container_bash control-plane ci/test_h10_assistant_api.sh

    # ── what a person can see and ask ──────────────────────────────────
    section "H12 · Seeing the files an executor is changing"
    container_bash control-plane ci/test_h12_workspace_view.sh

    section "H13 ⛔ · A person can ASK, and asking cannot authorise work"
    container_bash control-plane ci/test_h13_ask.sh

    section "H13 · Test stories and errands worth running"
    container_bash control-plane ci/test_h13_seed.sh

    section "H14 · The assistant can see the work, and what is stuck"
    container_bash control-plane ci/test_h14_progress_and_retry.sh

    section "Teardown"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh down)

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                   Stage H gate: GREEN                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

stage_fb() {
    stage_g

    section "Fb1 · Task pathologies"
    container_bash control-plane ci/test_fb1_task_pathologies.sh

    section "Fb2 · Test pathologies"
    container_bash control-plane ci/test_fb2_test_pathologies.sh

    section "Fb3 · Budget"
    container_bash control-plane ci/test_fb3_budget.sh

    section "Fb4 · Provider — malformed and empty"
    container_bash control-plane ci/test_fb4_provider.sh

    section "Fb5 · Human — a refusal is a verdict"
    container_bash control-plane ci/test_fb5_human.sh

    section "Fb6 · Concurrency, with real executors"
    container_bash control-plane ci/test_fb6_concurrency.sh

    section "Fb7 · Infrastructure"
    container_bash control-plane ci/test_fb7_infrastructure.sh

    section "Fb8 · Sub-agent"
    container_bash control-plane ci/test_fb8_subagent.sh

    section "Fb9 · Errand-specific"
    container_bash control-plane ci/test_fb9_errand.sh

    section "Fb10 ⛔ · Every gate against a deliberately broken build"
    bash "$REPO_ROOT/infra/e0/ci/redbuild.sh"

    echo ""
    echo "Stage Fb complete."
}

stage_k() {
    # Through H, not G. This used to chain stage_g, which meant the
    # whole cumulative chain reached K having never run a single
    # Stage H assertion -- K's subject is one assistant across many
    # stories, and H is where that assistant's surface is proven.
    stage_h

    section "K · One assistant, many stories — the surfaces that must stay single"
    bash "$REPO_ROOT/infra/e0/ci/check_stage_k_surfaces.sh"

    # stage_g tears the stack down before declaring itself green, and the
    # invariants below need a database. Running them on the host instead
    # would be worse than not running them: asyncpg may be absent there,
    # `importorskip` would skip the whole module, and the gate would report
    # green having asserted nothing.
    section "Bring the stack back up (stage_g tears it down)"
    (cd "$REPO_ROOT/infra/e0" && bash e0.sh up)

    section "K · Stage K invariants (identity fold, ceiling, docs-only workspace)"
    container_run control-plane \
        python -m pytest app/services/tests/test_stage_k_invariants.py -q

    section "K · One memory turn at a time per home"
    container_run control-plane \
        python -m pytest app/services/tests/test_memory_home_lock_sql.py -q

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                   Stage K gate: GREEN                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

stage_l() {
    stage_k

    section "L · What the goal knows — decisions, findings, alignment"
    container_run control-plane \
        python -m pytest app/services/tests/test_stage_l_knowledge.py -q

    # The knowledge tests prove the LOGIC. This proves the writes are
    # PERMITTED: every Stage L call site is best-effort, so a missing
    # grant turns the whole stage off in silence (KER15, six swallowed
    # `permission denied for table stories` and every surface empty).
    python -m pytest app/services/tests/test_stage_l_grants_sql.py -q

    # And the writers CALLED, not re-implemented. The two gates above
    # prove the logic and the permission; neither ever invoked
    # update_brief, which shipped declaring $2 and passing one argument.
    python -m pytest app/services/tests/test_go_brief_writers_sql.py -q

    # Legs run at once, and the number is configuration. The fan-out is
    # tested in kernel/worker/concurrency.py rather than through the
    # loop, which imports the model SDKs.
    python -m pytest kernel/worker/tests/test_leg_concurrency.py -q

    # Run controls, and checks that name the mount they run in. The
    # scoping test asserts no entry point accepts a story_id — a control
    # that did would reach any run in the goal from any conversation.
    python -m pytest app/services/tests/test_kernel_run_control_sql.py -q
    python -m pytest kernel/tools/tests/test_check_paths.py -q
    python -m pytest kernel/harness/tests/test_steer_message.py -q
    python -m pytest kernel/workspace/tests/test_browse_resolve.py -q
    python -m pytest kernel/mailbox/tests/test_hold_validation.py -q
    python -m pytest app/services/tests/test_executor_activity.py -q

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                   Stage L gate: GREEN                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
}

case "${1:-}" in
    stage-b)  stage_b ;;
    stage-c)  stage_c ;;
    stage-d)  stage_d ;;
    stage-e)  stage_e ;;
    stage-eb) stage_eb ;;
    stage-f)  stage_f ;;
    stage-g)  stage_g ;;
    stage-h)  stage_h ;;
    stage-fb) stage_fb ;;
    stage-k)  stage_k ;;
    stage-l)  stage_l ;;
    check-tokens)
        shift
        container_bash control-plane ci/check_token_reconciliation.sh "$@" ;;
    redbuild)
        shift
        bash "$REPO_ROOT/infra/e0/ci/redbuild.sh" "$@" ;;
    check-pricing)
        shift
        container_bash control-plane ci/check_pricing_record.sh "$@" ;;
    baseline-g3)
        shift
        container_bash control-plane ci/baseline_g3.sh "$@" ;;
    *)
        echo "Usage: $0 stage-b | stage-c | stage-d | stage-e | stage-eb | stage-f | stage-g | stage-fb | stage-k | stage-l | baseline-g3 | check-tokens | check-pricing | redbuild" >&2
        echo "" >&2
        echo "Each stage runs everything up to and including itself." >&2
        echo "Available: stage-b stage-c stage-d stage-e stage-eb stage-f" >&2
        exit 1
        ;;
esac
