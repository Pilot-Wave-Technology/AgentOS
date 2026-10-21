data "google_project" "this" {
  project_id = var.project_id
}

# The gateway stack reserves its VM's internal address under this name.
data "google_compute_address" "gateway" {
  count   = var.openclaw_ws_url == "" ? 1 : 0
  name    = "${var.name_prefix}-gateway-ip"
  region  = var.region
  project = var.project_id
}

locals {
  # Derived resource names ---------------------------------------------------
  service_name    = var.name_prefix
  task_runner_job = "${var.name_prefix}-task-runner"

  # The public + agent-callback surface is <prefix>-web; the management
  # routers live in <prefix>-management. app.main's proxy_routes forwards
  # /settings/* to the latter, so these URLs must point at the right
  # service.
  web_service_name        = "${var.name_prefix}-web"
  management_service_name = "${var.name_prefix}-management"
  # Service account created by prerequisites.sh (not by terraform). Email is
  # deterministic from name_prefix + project.
  runtime_sa_email = "${var.name_prefix}-runtime@${var.project_id}.iam.gserviceaccount.com"

  openclaw_ws_url = (var.openclaw_ws_url != "" ? var.openclaw_ws_url
  : "ws://${data.google_compute_address.gateway[0].address}:18789")

  workspace_bucket_prefix = coalesce(var.workspace_bucket_prefix, "${var.name_prefix}-agent-ws")
  resource_bucket         = coalesce(var.resource_bucket, "${var.name_prefix}-agent-resources-${var.project_id}")
  extension_bucket        = "${var.name_prefix}-vscode-${var.project_id}"
  archive_bucket          = coalesce(var.archive_bucket, "${var.name_prefix}-agent-data-${var.project_id}")

  # Cloud Run v2 default URL is deterministic: <service>-<projectnumber>.<region>.run.app
  control_panel_url      = "https://${local.web_service_name}-${data.google_project.this.number}.${var.region}.run.app"
  management_service_url = "https://${local.management_service_name}-${data.google_project.this.number}.${var.region}.run.app"

  # Non-secret env vars for the service (secrets are wired separately). --------
  base_env = {
    APP_ENV                     = "production"
    LOG_LEVEL                   = var.flags.log_level
    FIREBASE_PROJECT_ID         = coalesce(var.firebase_project_id, var.project_id)
    FIREBASE_WEB_API_KEY        = var.firebase_web_api_key
    OPENCLAW_WS_URL             = local.openclaw_ws_url
    GCS_WORKSPACE_BUCKET_PREFIX = local.workspace_bucket_prefix
    GCS_RESOURCE_BUCKET         = local.resource_bucket
    # The VS Code extension channel (/extension). Bucket root, one channel.
    EXTENSION_DIST_BUCKET      = local.extension_bucket
    EXTENSION_DIST_PREFIX      = ""
    AGENT_ARCHIVE_BUCKET       = local.archive_bucket
    BOOTSTRAP_ADMIN_EMAIL      = var.bootstrap_admin_email
    GITHUB_ORG                 = var.github_org
    GITHUB_APP_ID              = var.github_app_id
    GITHUB_APP_INSTALLATION_ID = var.github_app_installation_id
    CONTROL_PANEL_URL          = local.control_panel_url
    MANAGEMENT_SERVICE_URL     = local.management_service_url
    MAIN_SERVICE_URL           = local.control_panel_url
    TASK_RUNNER_JOB_NAME       = var.enable_task_runner ? local.task_runner_job : ""
    AVAILABLE_MODELS           = jsonencode(var.available_models)
    # The Platform Research Council reads platform config from the AgentOS fork
    # (repo_url), NOT from the goal-repo org.
    PLATFORM_CLONE_URL = var.repo_url
    PLATFORM_REPO_NAME = trimsuffix(element(split("/", var.repo_url), length(split("/", var.repo_url)) - 1), ".git")
    # The background loops run in the `-scheduler` service, behind one
    # leader. Only the web tier reads this; it stops it running a second
    # copy of every reaper per instance.
    AGENTOS_SCHEDULER_LOOPS = "0"

    # THE CONTROL PANEL AND THE EXECUTION PLANE MUST AGREE on how work
    # lands, and the plane's half is in deploy/k8s/conductor.yaml. With
    # STORY_HOLDS_FOR_MERGE on the pod and off here, a finished run pushed
    # its branch and waited for a merge while the story stayed
    # `in_progress` -- so the git engine never opened the pull request.
    STORY_HOLDS_FOR_MERGE = "1"
    MULTI_REPO_ENABLED    = "true"
    # Pods upload artifacts THROUGH the control panel. Unset, it falls back
    # to the container's own disk: one ephemeral instance holds each file.
    ARTIFACT_STORE  = "gcs"
    ARTIFACT_BUCKET = "${var.name_prefix}-artifacts-${var.project_id}"
  }

  slack_env = var.slack_enabled ? {
    SLACK_CLIENT_ID      = var.slack_client_id
    SLACK_COMMAND_SUFFIX = var.slack_command_suffix
  } : {}

  oauth_env = var.gcp_oauth_enabled ? {
    GCP_OAUTH_CLIENT_ID    = var.gcp_oauth_client_id
    GCP_OAUTH_REDIRECT_URL = "${local.control_panel_url}/gcp/oauth/callback"
  } : {}

  flag_env = {
    AGENT_SCOPED_TOKENS_ENABLED          = tostring(var.flags.agent_scoped_tokens_enabled)
    AGENT_AUTHZ_ENFORCE                  = tostring(var.flags.agent_authz_enforce)
    CUSTOM_TOOLS_BROKER_ENFORCE          = tostring(var.flags.custom_tools_broker_enforce)
    AGENT_REJECT_SHARED_TOKEN            = tostring(var.flags.agent_reject_shared_token)
    CHAT_QUEUE_ENABLED                   = tostring(var.flags.chat_queue_enabled)
    CHAT_GATEWAY_ID                      = var.flags.chat_gateway_id
    MEMORY_V2_BRIDGE_ENABLED             = tostring(var.flags.memory_v2_bridge_enabled)
    MEMORY_V2_FLUSH_ENABLED              = tostring(var.flags.memory_v2_flush_enabled)
    MEMORY_V2_SNAPSHOT_ENABLED           = tostring(var.flags.memory_v2_snapshot_enabled)
    MEMORY_V2_USER_MEMORY_ENABLED        = tostring(var.flags.memory_v2_user_memory_enabled)
    SECURITY_UI_ENABLED                  = tostring(var.flags.security_ui_enabled)
    PLATFORM_COUNCIL_ENABLED             = tostring(var.flags.platform_council_enabled)
    PLATFORM_RESEARCH_DISPATCHER_ENABLED = tostring(var.flags.platform_research_dispatcher_enabled)
    PLATFORM_AGENTS_RECREATE_ON_BUILD    = tostring(var.flags.platform_agents_recreate_on_build)
    GITGRAPH_ENABLED                     = tostring(var.flags.gitgraph_enabled)
    TASK_DAG_CEILING                     = tostring(var.flags.task_dag_ceiling)
    TASK_DAG_CEILING_ENFORCE             = tostring(var.flags.task_dag_ceiling_enforce)
    LEDGER_ACTOR_AUDIT                   = tostring(var.flags.ledger_actor_audit)
  }

  service_env = merge(local.base_env, local.slack_env, local.oauth_env, local.flag_env)

  # PER SERVICE, AS THE INTERNAL DEPLOYMENTS SET THEM. One shared map gave
  # every service every switch, and three of them START something wherever
  # they are true: with COUNCIL_DISPATCHER_ENABLED and GOAL_SUMMARY_ENABLED
  # on the web tier as well, app/main.py ran a second council dispatcher and
  # a second goal-summary scheduler beside the council worker's.
  #
  # The schedulers and dispatchers belong to the council worker alone; the
  # rest is off by name, because "unset" reads differently in different
  # modules and these are the ones that act.
  not_the_council = {
    COUNCIL_DISPATCHER_ENABLED  = "false"
    COUNCIL_SCHEDULE_ENABLED    = "false"
    GOAL_SUMMARY_ENABLED        = "false"
    SECURITY_DISPATCHER_ENABLED = "false"
    SECURITY_SCHEDULE_ENABLED   = "false"
  }
  per_service_env = {
    web = merge(local.not_the_council, {
      # The assessor, investigator and answerer paths; the pods get the
      # same switch from deploy/k8s/conductor.yaml.
      AGENTOS_ASSIST      = "1"
      STORY_REVIEW_IN_POD = "1"
      TAXIMETER_SCORING            = "v2"
      THREAD_INITIAL_MESSAGE_LIMIT = "30"
      THREAD_EARLIER_PAGE_SIZE     = "30"
    })
    council = {
      COUNCIL_DISPATCHER_ENABLED     = tostring(var.flags.council_dispatcher_enabled)
      COUNCIL_SCHEDULE_ENABLED       = tostring(var.flags.council_schedule_enabled)
      GOAL_SUMMARY_ENABLED           = tostring(var.flags.goal_summary_enabled)
      SECURITY_DISPATCHER_ENABLED    = tostring(var.flags.security_dispatcher_enabled)
      SECURITY_SCHEDULE_ENABLED      = tostring(var.flags.security_schedule_enabled)
      SECURITY_SCHEDULE_HOURS        = tostring(var.flags.security_schedule_hours)
      SECURITY_KEEP_AGENT_WORKSPACES = "false"
      STORY_REVIEW_IN_POD            = "1"
    }
    git        = local.not_the_council
    management = local.not_the_council
    scheduler  = local.not_the_council
  }

  slack_secret_env = var.slack_enabled ? {
    SLACK_CLIENT_SECRET  = "slack-client-secret"
    SLACK_SIGNING_SECRET = "slack-signing-secret"
  } : {}

  # Gateway-OWNED (stage 1) secrets this service CONSUMES. Not created here —
  # the platform SA is granted accessor on them (they exist after stage 1).
  shared_secret_env = merge(
    {
      OPENCLAW_AUTH_TOKEN = "openclaw-gateway-token"
      # THE KERNEL TOOL SURFACE REFUSES TO START WITHOUT THIS.
      # `app/main.py` mounts `kernel/assistant/asgi.py`, and
      # `kernel/assistant/api.py` raises at import unless
      # ASSISTANT_API_TOKEN is set -- deliberately, because an unset
      # token once authorised every caller. Omitting it here did not
      # produce a clear error: the surface simply never answered, which
      # the preflight reports as E_KERNEL_UNREACHABLE.
      #
      # It is the SAME secret as OPENCLAW_AUTH_TOKEN, matching the
      # internal deployments, because both name the one pre-shared
      # bearer between the control plane and the gateway.
      ASSISTANT_API_TOKEN = "openclaw-gateway-token"
    },
    var.gemini_enabled ? { GEMINI_API_KEY = "gemini-api-key" } : {},
    var.anthropic_enabled ? { ANTHROPIC_API_KEY = "anthropic-api-key" } : {},
    var.openai_enabled ? { OPENAI_API_KEY = "openai-api-key" } : {},
  )

  # Platform-OWNED secrets (created here as empty shells).
  platform_secret_env = merge({
    FIREBASE_SERVICE_ACCOUNT_JSON = "firebase-service-account"
    DATABASE_URL                  = "database-url"
    APP_SECRET_KEY                = "app-secret-key"
    INTERNAL_API_KEY              = "internal-api-key"
    AGENT_TOKEN_SIGNING_KEY       = "agent-token-signing-key"
    CREDENTIAL_ENCRYPTION_KEY     = "credential-encryption-key"
    GITHUB_APP_PRIVATE_KEY_SECRET = "github-app-key"
    GITHUB_WEBHOOK_SECRET         = "github-webhook-secret"
  }, local.slack_secret_env)

  # ENV_VAR -> secret id wired into the Cloud Run service (platform + shared).
  # database-url is terraform's (modules/database); every other one is created
  # by prerequisites.sh and only referenced here.
  secret_env = merge(local.platform_secret_env, local.shared_secret_env)

  # Secrets the generic secrets module would create. Empty: the DSN secrets
  # live with the logins they describe, in modules/database.
  db_secrets = []
}
