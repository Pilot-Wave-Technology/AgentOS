from pydantic import field_validator
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
    # STRIPPED AT THE BOUNDARY -- see _strip_shared_token below. This
    # value is compared byte-for-byte against a header built by callers
    # that read the same secret through a shell, and a shell strips
    # trailing newlines where Cloud Run does not.
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

    # ── ONE SECRET, TWO READERS, DIFFERENT BYTES ─────────────────────
    #
    # Every gateway -> control-panel call in the `next` environment was
    # refused, and the caller was told "HTTP 405 Method Not Allowed".
    # The token was right; the BYTES were not.
    #
    # Cloud Run mounts a secret's payload verbatim, so a value stored
    # with a trailing newline arrives here as token-plus-newline. Every
    # other reader of the same secret goes through a shell --
    #
    #   OPENCLAW_GATEWAY_TOKEN=$(gcloud secrets versions access latest ...)
    #
    # in the VM's startup script, redeploy.sh and
    # scripts/infra/deploy_execution_plane.sh -- and command
    # substitution strips trailing newlines. So the gateway, the
    # conductor and the executors all hold "<token>" and this process
    # held token-plus-newline, and hmac.compare_digest is (correctly)
    # not in the business of being lenient about that.
    #
    # IT FAILED IN ONE DIRECTION ONLY, which is why it read as anything
    # but a credential problem. The gateway TRIMS the token it receives
    # (resolveSharedConnectAuth in agent_os_openclaw's auth-context.ts), so
    # the control panel's connections were accepted, agents ran, and
    # chat worked -- while every call back the other way was refused.
    #
    # Normalising here rather than at each comparison means the value
    # SENT to the gateway (app/openclaw.py) and the value COMPARED
    # against (app/internal_auth.py) cannot disagree, and a secret
    # rotated by someone typing `echo` into a file is not an outage.
    # WHICH TAXIMETER SCORER SERVES THE PANEL. v1 counts raw events and
    # takes each goal's weight from a number an LLM wrote on it; v2
    # counts decisions in the stories you own, items and priority
    # points, and weighs a goal by what it actually delivered.
    #
    # A SETTING RATHER THAN A DEPLOY, so an environment can be put back
    # on v1 without a code change if v2 turns out to be wrong about
    # something, and the two can be compared on the same data.
    #
    # THE DEFAULT IS v2 AS OF S6, AND NOT BEFORE. It stayed v1 through
    # S4 and S5 deliberately: the dashboard still said "Weightage ...
    # set by an LLM" for a weight no LLM sets any more, and v2 numbers
    # under v1 explanations are worse than either on its own. The text
    # is rendered from the same constants the scorer uses now, so the
    # two cannot drift apart again.
    taximeter_scoring: str = "v2"

    @field_validator("taximeter_scoring", mode="after")
    @classmethod
    def _known_scorer(cls, v: str) -> str:
        """Refuse an unknown value at startup rather than silently
        falling back. A typo in an env var would otherwise look exactly
        like a deliberate choice of v1, and nobody would find it until
        they wondered why the new scores never appeared."""
        got = (v or "").strip().lower()
        if got not in ("v1", "v2"):
            raise ValueError(
                f"taximeter_scoring must be 'v1' or 'v2', not {v!r}")
        return got

    @field_validator("openclaw_auth_token", mode="after")
    @classmethod
    def _strip_shared_token(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("*", mode="after")
    @classmethod
    def _whitespace_is_unset(cls, v):
        """A whitespace-only setting is NOT configured. Make that true.

        SECRET MANAGER REJECTS AN EMPTY PAYLOAD, so `prerequisites.sh`
        creates unprovided secrets with a single space -- there has to be
        SOME version or Cloud Run refuses to deploy a service that
        references the secret, which is a real constraint and not a
        mistake.

        The mistake was the next sentence. `CONFIG.md`, the deploy guide
        and the script's own comment all stated that "consumers treat
        whitespace-only as unset". No consumer did: every guard in the
        codebase is `if not key`, and `" "` is truthy. So an
        unconfigured Slack, GitHub or OpenAI credential read as
        CONFIGURED, and the feature behind it failed later and
        elsewhere -- a 401 from a vendor, or a webhook that verified
        every signature against one space.

        One validator makes the sentence true, and it is on `"*"` for a
        reason: the next secret somebody adds inherits the behaviour
        without having to know this paragraph exists.
        """
        return "" if isinstance(v, str) and v.strip() == "" else v

    # GCS — overridable per deployment via GCS_WORKSPACE_BUCKET_PREFIX /
    # GCS_RESOURCE_BUCKET. Defaults are this deployment's real buckets; the public
    # (AgentOS) build replaces every default here with a placeholder + setup doc.
    gcs_workspace_bucket_prefix: str = "${WORKSPACE_BUCKET_PREFIX}"
    gcs_resource_bucket: str = "${RESOURCE_BUCKET}"
    # Bucket where a removed agent's MEMORY.md/memory/sessions are archived before
    # deletion. Override via AGENT_ARCHIVE_BUCKET.
    agent_archive_bucket: str = "${ARCHIVE_BUCKET}"
    # Private bucket holding the VS Code extension .vsix files + a `latest.json`
    # manifest ({version, object, notes}). The control plane streams these to
    # teammates via /extension/*, so the bucket stays private (only the CP SA
    # reads it). Override via EXTENSION_DIST_BUCKET.
    extension_dist_bucket: str = ""
    # Object-key prefix (channel) WITHIN that bucket, so one bucket holds a
    # separate artifact set per environment — prod/, dev/, feature/. Each
    # deployment serves only its own channel's latest.json + .vsix, so promoting
    # never clobbers another env's artifact. Set per-env in cloudbuild*.yaml via
    # EXTENSION_DIST_PREFIX. Empty = bucket root (legacy layout).
    extension_dist_prefix: str = ""

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

    # Local development master switch (LOCAL_SERVER=true). SINGLE source of truth
    # for "am I running the local dev stack?". When true, cloud services are
    # swapped for local replicas — currently just the Firebase Auth Emulator
    # standing in for real Firebase. GCS is never emulated locally (real
    # buckets via ADC in both modes; see local-development/README.md). All of
    # that behaviour is gated behind this one flag via app/local_dev.py;
    # nothing local-only ever activates unless this is true. MUST stay False
    # in every deployed environment.
    local_server: bool = False
    # Local-replica endpoint, only consulted when local_server is true.
    # (firebase-admin also reads FIREBASE_AUTH_EMULATOR_HOST natively; local_dev.py
    # keeps os.environ in sync with this value.)
    firebase_auth_emulator_host: str = "localhost:9099"
    # Replaces the "~" in every "~/.openclaw/workspace-<id>" path sent to the
    # gateway (see app/local_dev.py:agent_workspace_path). Empty = unchanged
    # behavior (the literal "~", expanded by the gateway's own OS home dir).
    # Only meaningful when local_server is true; irrelevant in production.
    local_workspace_root: str = ""

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
    memory_v2_snapshot_enabled: bool = False      # workspace snapshot/restore on reap + restore-on-provision
    memory_v2_user_memory_enabled: bool = False   # USER_MEMORY.md cross-goal sharing
    memory_v2_periodic_snapshot_hours: int = 6    # periodic snapshot cadence
    # The 6h periodic snapshot loop (a safety net) is now gated SEPARATELY from the
    # event-driven snapshot/restore above, so the local↔web memory sync (which needs
    # snapshot_enabled for the pre-reap + restore-on-provision paths) doesn't force the
    # heavy periodic loop on. Default OFF everywhere.
    memory_v2_periodic_snapshot_enabled: bool = False

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
    # Multi-repo stories (research/MULTI_REPO_STORIES_PLAN.md). When true, a story may
    # be granted N repos: each WRITE grant becomes its own git node / branch / PR, and
    # the workspace clones one directory per repo. Ships OFF so every phase of the
    # rollout is behaviour-preserving — with it false the platform keeps the historical
    # "exactly one repo per story" rule. Requires a gateway that accepts a repo LIST in
    # `shca.workspace.setup` (plan Phase G), so do NOT enable before that redeploy.
    multi_repo_enabled: bool = False
    # Git-credential broker (research/MULTI_REPO_STORIES_PLAN.md M5). When true, story
    # workspaces get a callback credential helper that fetches a freshly minted token
    # from /internal/git-credential per git operation, instead of an installation
    # token embedded in each clone's .git/config. Ships OFF: the gateway must be
    # redeployed with the G.6 helper AND this endpoint must be reachable from the VM
    # before flipping it, or git auth breaks for every operator story.
    git_credential_broker_enabled: bool = False
    # Absolute base URL the story workspace uses to reach this control panel. Only
    # needed when git_credential_broker_enabled is on — the VM is not the CP, so it
    # cannot infer the address from the request. e.g. https://control-panel-dev.../
    control_panel_public_url: str = ""
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

    # Message-thread pagination (GO thread + the read-only agent channels).
    # Set explicitly per environment in cloudbuild*.yaml's control-panel step;
    # these defaults only apply if that is ever removed. Local dev overrides
    # them lower via local.values to exercise paging on a small database.
    # See app/routers/thread_paging.py.
    thread_initial_message_limit: int = 30
    thread_earlier_page_size: int = 30

    # Ledger provenance audit: when ON, ledger.journal.append_event logs a warning
    # if an event's actor_id has no row in `actors` (i.e. attribution won't
    # resolve). Positive-cached + run before the advisory lock, so steady-state
    # cost is ~zero and it never extends the ledger critical section. Default ON
    # (safe everywhere); flip to False via LEDGER_ACTOR_AUDIT=false to silence.
    ledger_actor_audit: bool = True

    # Available LLM models shown in dropdowns.
    #
    # NO PRE-3-SERIES GEMINI IS OFFERED. The 2.5 ids were here until
    # 2026-10-18; a model nobody should start on does not belong in a
    # dropdown, and leaving them listed is how a new agent lands on one
    # by a click. An agent already pinned to 2.5 keeps running — its
    # `agents.model` row is untouched and the gateway rewrites the wire
    # id (`opencode-zen-models.ts`) — so this stops new selections
    # without moving anything live.
    available_models: list[str] = [
        "google/gemini-3.8-flash",
        "anthropic/claude-opus-5-5",
        "anthropic/claude-sonnet-5-5",
        "anthropic/claude-haiku-4-5",
        # NO OPENAI ENTRY. `openai/gpt-4o` was offered here while nothing
        # in AgentOS reads OPENAI_API_KEY -- picking it gave an agent that
        # looked configured and failed on its first turn. The key is still
        # carried end to end, so re-add an id here the day a provider
        # actually resolves it.
        "claude-cli/opus",
        "claude-cli/sonnet",
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
