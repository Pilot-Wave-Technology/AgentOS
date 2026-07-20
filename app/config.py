from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # GCP / Firebase
    firebase_project_id: str = "YOUR_GCP_PROJECT_ID"
    firebase_service_account_json: str = ""   # JSON string or file path
    firebase_web_api_key: str = "YOUR_FIREBASE_WEB_API_KEY"

    # PostgreSQL (Cloud SQL via Auth Proxy)
    database_url: str = "postgresql://multi_agent_cp:pass@localhost:5432/multi_agent_state_ledger"

    # OpenClaw Gateway
    openclaw_ws_url: str = "ws://VM-INTERNAL-IP:18793"
    openclaw_auth_token: str = "YOUR_OPENCLAW_AUTH_TOKEN"
    # Per-agent scoped tokens (shared-token fix — SHARED_TOKEN_SECURITY_FIX_PLAN.md).
    # HMAC signing key for per-agent JWTs (minted AND verified by the control plane only; never
    # leaves the backend). Empty = minting disabled -> shared-bearer fallback.
    agent_token_signing_key: str = ""
    # Per-agent JWT auth on /internal/*. SECURE BY DEFAULT (was False during rollout;
    # prod/dev already set it True via env). ON = accept per-agent JWTs + expose agent
    # identity for ownership/self-approval checks (get_agent_principal). Set False only
    # for a legacy shared-token-only deployment.
    agent_scoped_tokens_enabled: bool = True
    # Ownership enforcement for per-agent JWTs. ON = 403 on cross-goal. SECURE BY
    # DEFAULT. Set False for SHADOW (log-only) during a migration.
    agent_authz_enforce: bool = True
    # Credential broker (#3) enforcement: a tool may only fetch credentials its
    # manifest declares. SECURE BY DEFAULT (ON = 403 on a non-granted credential).
    # A legit tool never requests a credential it doesn't declare. Set False for
    # SHADOW (log-only) if migrating existing tools' manifests.
    custom_tools_broker_enforce: bool = True
    # P0-A.2 cutover: when True, verify_internal_bearer REJECTS the shared token
    # (per-agent JWT required) on all /internal/* EXCEPT the bootstrap mint endpoint
    # (verify_bootstrap_bearer). Enable ONLY after `deprecated_legacy_token`
    # telemetry is ~zero and the IA Bash wrappers are migrated/unused — flipping
    # early 401s a still-legacy caller. Default False (safe rollout).
    agent_reject_shared_token: bool = False

    # GCS — overridable per deployment via GCS_WORKSPACE_BUCKET_PREFIX /
    # GCS_RESOURCE_BUCKET. Defaults are this deployment's real buckets; the public
    # (AgentOS) build replaces every default here with a placeholder + setup doc.
    gcs_workspace_bucket_prefix: str = "${WORKSPACE_BUCKET_PREFIX}"
    gcs_resource_bucket: str = "${RESOURCE_BUCKET}"
    # Bucket where a removed agent's MEMORY.md/memory/sessions are archived before
    # deletion. Override via AGENT_ARCHIVE_BUCKET.
    agent_archive_bucket: str = "${ARCHIVE_BUCKET}"

    # Cloud Run task-runner job — generic async executor (currently: document
    # pre-rendering for PDF/DOCX/XLSX/CSV). Fired via run.jobs.run; the job polls
    # the DB for pending work. Empty = disabled (local dev).
    # Set TASK_RUNNER_JOB_NAME=task-runner-<env> in Cloud Build.
    task_runner_job_name: str = ""

    # Bootstrap — first user with this email auto-creates as admin (skip signup)
    bootstrap_admin_email: str = ""

    # Internal API key for PEP endpoints (gateway → control panel)
    internal_api_key: str = ""

    # GitHub App (for generating short-lived repo tokens)
    github_app_id: str = ""
    github_app_private_key_secret: str = ""   # Secret Manager path to .pem
    github_app_installation_id: str = ""      # Installation ID for the org
    github_org: str = "Pilot-Wave-Technology"  # GitHub org/owner hosting goal repos (override GITHUB_ORG)

    # Gemini API (for conversation summarization)
    gemini_api_key: str = ""

    # App
    app_secret_key: str = "CHANGE_ME_USE_SECRETS_MANAGER_IN_PROD"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"  # Set LOG_LEVEL=DEBUG for verbose gateway/DB logs

    # Slack integration (optional — leave empty to disable)
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""
    # Per-environment command suffix so dev/prod/feature Slack apps don't clash.
    # Set SLACK_COMMAND_SUFFIX=dev|prod|feature in each Cloud Build env.
    # Commands become /goal-<suffix>, /story-<suffix>, etc.
    # Leave empty to use plain /goal, /story, etc. (single-env or local dev).
    slack_command_suffix: str = ""

    # Dispatch mode: "chat_send" (current) or "acp" (Phase 4)
    dispatch_mode: str = "chat_send"

    # Kill switch — stuck-after-pause detection. Once execution is paused
    # (graceful), an in-flight turn should wrap up shortly. If it is still
    # running this many minutes after the pause, the controller emits an
    # `agent.stuck_after_pause` ledger event so the admin UI can surface a
    # "force-kill?" affordance. IA turns are short; phase turns legitimately run
    # longer (the cli-spawn ceiling is 30 min), so its threshold is higher.
    stuck_after_pause_ia_minutes: int = 3
    stuck_after_pause_phase_minutes: int = 5

    # GCP OAuth — Connect-via-Google flow for SA setup.
    # Register an OAuth 2.0 client in Google Cloud Console with
    # redirect_uri = <your-app-url>/gcp/oauth/callback.
    # gcp_vm_sa_email is the Cloud Run (or VM) SA that must receive
    # roles/iam.serviceAccountTokenCreator on every newly provisioned SA.
    gcp_oauth_client_id: str = ""
    gcp_oauth_client_secret: str = ""
    gcp_oauth_redirect_url: str = ""     # e.g. https://your-app.run.app/gcp/oauth/callback
    gcp_vm_sa_email: str = ""            # Cloud Run SA email

    # Custom tools — Fernet key for encrypting tool_credentials.encrypted_value.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Add as a Cloud Run secret: see documentation/deployment/CREDENTIAL_ENCRYPTION_KEY.md
    credential_encryption_key: str = ""

    # ── memory_v2 rollout feature flags ──────────────────────────────────────
    # Default OFF — flip per-flag once the corresponding agent_os_openclaw
    # changes are deployed and verified.
    memory_v2_bridge_enabled: bool = False        # ledger event → memory file bridge
    memory_v2_flush_enabled: bool = False         # story-complete + pre-reap flushes
    memory_v2_snapshot_enabled: bool = False      # workspace snapshot/restore on reap
    memory_v2_user_memory_enabled: bool = False   # USER_MEMORY.md cross-goal sharing
    memory_v2_periodic_snapshot_hours: int = 6    # periodic snapshot cadence

    # Goal Review Council — Phase 1. When true, THIS process runs the council
    # dispatcher loop (claims runs, spawns reviewer RAs, delivers to GO).
    # Co-located in the web tier for Phase 1; promotable to a separate worker.
    council_dispatcher_enabled: bool = False

    # Debug: when true, reviewer RA agents + their workspaces are NOT deleted
    # after a stage terminates, so the docs/ inputs and transcript can be
    # inspected (e.g. to diagnose read-tool failures). Reviewers will
    # accumulate on the gateway until cleared — leave OFF in normal operation.
    council_keep_ra_workspaces: bool = False

    # Security Council — P0. When true, THIS process runs the security council
    # dispatcher loop (claims branch review runs, spawns shell-capable aspect
    # agents that scan a server-prepared read-only checkout, delivers to every
    # active goal that has the branch as a resource). Hosted alongside the Goal
    # Review Council dispatcher in the council-worker service. Manual trigger only.
    security_dispatcher_enabled: bool = False
    # Reserved for the future scheduled security sweep (heavier than the goal
    # loop — weekly when enabled). Off for P0: security reviews are manual.
    security_schedule_enabled: bool = False
    # Debug: keep aspect-agent workspaces (with the cloned checkout + scanner
    # output) after a stage terminates, to inspect scans. OFF in normal operation.
    security_keep_agent_workspaces: bool = False
    # Platform Research Council. When true, THIS process ensures the __platform__
    # goal + convener on startup and runs the worker reaper. Gates the whole
    # council so it ships dormant and is enabled per-environment (dev first).
    platform_council_enabled: bool = False
    # Scheduled autonomous sweeps (model + tool research on a cadence). OFF by
    # default — the council ships with only the manual button + on-demand chat;
    # flip per-env once proven. Independent of platform_council_enabled (which is
    # the master gate; the scheduler is a no-op unless BOTH are true).
    platform_schedule_enabled: bool = False
    # Days between scheduled sweeps when platform_schedule_enabled is true.
    platform_schedule_interval_days: int = 7
    # Weekly Independent Platform Research discovery sweep (breadth scout → discovery
    # feed). OFF by default; the "Run Research Sweep" button triggers it on demand.
    platform_discovery_schedule_enabled: bool = False
    platform_discovery_interval_days: int = 7
    # Deterministic research-queue drainer (runs in the council-worker alongside the
    # goal-review + security dispatchers). Drains platform_discoveries one-at-a-time
    # end-to-end: claim oldest 'new' (priority DESC, queued_at ASC) → one research
    # sweep → reviewed | failed. OFF by default. This is the ONLY thing that turns
    # discoveries into research (the old daily deep-dive scheduler was removed).
    platform_research_dispatcher_enabled: bool = False
    # A discovery stuck 'researching' longer than this (sweep died / never linked) is
    # reconciled to 'failed' by the drainer watchdog. Slightly above the sweep's own
    # 30-min stale-fail so the sweep gets the first chance to terminate cleanly.
    platform_research_watchdog_minutes: int = 40
    # Recreate the platform agents (platform-steward + platform-shca) from scratch
    # once per build — the ONLY reliable way to refresh their gateway tool scope,
    # since agents.update's tools field is a no-op on the gateway (only agents.create
    # honors tools). Gated by platform_build_sha so it fires once per deploy, not on
    # every container scale-up. Flip to false to keep the agents (and their memory)
    # across builds.
    platform_agents_recreate_on_build: bool = False
    # Build identifier (set to $SHORT_SHA in cloudbuild). Recreate fires once per
    # distinct value. Empty → recreate on every boot (fallback; logs a warning).
    platform_build_sha: str = ""
    # This control panel's own public base URL — the council's custom http_action
    # tools POST to {control_panel_url}/internal/platform/*. Set per-env.
    control_panel_url: str = ""
    # Platform Research Council — the repo it clones to research platform config
    # (models/tools/agents). Set to this deployment's control-plane repo.
    platform_repo_name: str = "agentos"
    platform_clone_url: str = "https://github.com/Pilot-Wave-Technology/AgentOS.git"

    # Goal Progress Summary — periodic GO-driven documents per active goal.
    # When true, the GoalSummaryScheduler runs in this process (council-worker
    # or main tier). Requires control_panel_url for tool seeding.
    goal_summary_enabled: bool = False
    # Dashboard background refresh cadences (seconds). ONE loop drives both:
    #  • Every health tick it refreshes the cheap, live-ish gateway_cache
    #    (health/readiness/presence/sessions/cron) so the health strip stays
    #    current — a gateway up/down change shows within this window.
    #  • Every mat-view tick (a multiple of the health tick) it additionally
    #    rebuilds the expensive cost/fleet/SLO materialized views. Those change
    #    slowly and the heavy REFRESH is the cost driver, so hourly is plenty;
    #    anyone needing them sooner uses the admin Refresh button (same code path,
    #    refresh_materialized_views(), which also refreshes the gateway_cache).
    dashboard_health_refresh_interval_seconds: int = 300
    dashboard_matview_refresh_interval_seconds: int = 3600
    # Management service URL — dashboard, settings, and admin live here.
    # Injected into main service templates so sidebar links cross over correctly.
    # Empty in local dev (falls back to local routes in templates).
    management_service_url: str = ""
    # Main service public URL — used by the management service to redirect
    # unauthenticated requests to the main service login page.
    # Empty in local dev (management service falls back to /login locally).
    main_service_url: str = ""
    # Idle window (minutes) before a warm Platform Research worker is reaped
    # ("no messages within this time -> delete the worker"). Also reaped at 80%
    # context. Convener respawns on demand.
    platform_worker_idle_minutes: int = 30

    # GO Git Dependency-Graph Manager. When true, the git-engine worker's tick
    # actually drives the per-repo lanes (branch cut / sync / merge queue). Ships
    # OFF: the engine deploys idle (leader election + git_dispatch listener only)
    # until the lane work lands and this is enabled per-environment (dev first).
    gitgraph_enabled: bool = False
    # HMAC secret (X-Hub-Signature-256) for the git-engine coexistence webhooks
    # (POST /webhooks/github/{repoId} on the web tier). Empty = signature check
    # skipped (dev). The webhook only wakes the engine (NOTIFY git_dispatch); the
    # engine's idempotent passes re-derive everything from live state.
    github_webhook_secret: str = ""

    # Web-tier gate for the Security Council MANUAL TRIGGER (goal-thread button +
    # POST /goals/{pid}/security/run). Independent of security_dispatcher_enabled
    # (which runs in the council-worker, not the web tier). Set true where the
    # manual trigger should be available (dev); left false ships it dormant (prod):
    # the button is hidden and the endpoint refuses, so no orphan runs are enqueued.
    security_ui_enabled: bool = False

    # Global concurrency ceiling for claude-code story DAGs (see
    # TASK_DAG_CEILING_PLAN.md). A "DAG" = one claude-code story cycle: a parent
    # task plus its executor='claude-code-phase' subtasks. At most
    # `task_dag_ceiling` DAGs execute concurrently; the rest wait (FIFO).
    # Enforcement is gated SEPARATELY so we can ship shadow-first: with
    # task_dag_ceiling_enforce=False the scheduler only LOGS what it would admit /
    # defer (zero dispatch change); flip to True to actually defer over-ceiling
    # DAGs. Env: TASK_DAG_CEILING, TASK_DAG_CEILING_ENFORCE.
    task_dag_ceiling: int = 4
    task_dag_ceiling_enforce: bool = False

    # Ledger provenance audit: when ON, ledger.journal.append_event logs a warning
    # if an event's actor_id has no row in `actors` (i.e. attribution won't
    # resolve). Positive-cached + run before the advisory lock, so steady-state
    # cost is ~zero and it never extends the ledger critical section. Default ON
    # (safe everywhere); flip to False via LEDGER_ACTOR_AUDIT=false to silence.
    ledger_actor_audit: bool = True

    # Available LLM models shown in dropdowns
    available_models: list[str] = [
        "google/gemini-2.5-flash",
        "google/gemini-2.5-pro",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
        "openai/gpt-4o",
        "claude-cli/opus",
        "claude-cli/sonnet",
        "claude-cli/haiku",
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
