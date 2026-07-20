data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Derived resource names ---------------------------------------------------
  service_name    = var.name_prefix
  task_runner_job = "${var.name_prefix}-task-runner"

  # Public/web + management service names differ by profile. In "single" one
  # service (name_prefix) serves everything. In "split" the public + agent-
  # callback surface is <prefix>-web and the management routers live in
  # <prefix>-management; app.main's proxy_routes forwards /settings/* to the
  # latter, so these URLs must point at the right service or split breaks.
  web_service_name        = var.service_profile == "single" ? var.name_prefix : "${var.name_prefix}-web"
  management_service_name = var.service_profile == "single" ? var.name_prefix : "${var.name_prefix}-management"
  # Service account created by prerequisites.sh (not by terraform). Email is
  # deterministic from name_prefix + project.
  runtime_sa_email = "${var.name_prefix}-runtime@${var.project_id}.iam.gserviceaccount.com"

  workspace_bucket_prefix = coalesce(var.workspace_bucket_prefix, "${var.name_prefix}-agent-ws")
  resource_bucket         = coalesce(var.resource_bucket, "${var.name_prefix}-agent-resources-${var.project_id}")
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
    OPENCLAW_WS_URL             = var.openclaw_ws_url
    GCS_WORKSPACE_BUCKET_PREFIX = local.workspace_bucket_prefix
    GCS_RESOURCE_BUCKET         = local.resource_bucket
    AGENT_ARCHIVE_BUCKET        = local.archive_bucket
    BOOTSTRAP_ADMIN_EMAIL       = var.bootstrap_admin_email
    GITHUB_ORG                  = var.github_org
    GITHUB_APP_ID               = var.github_app_id
    GITHUB_APP_INSTALLATION_ID  = var.github_app_installation_id
    CONTROL_PANEL_URL           = local.control_panel_url
    MANAGEMENT_SERVICE_URL      = local.management_service_url
    MAIN_SERVICE_URL            = local.control_panel_url
    TASK_RUNNER_JOB_NAME        = var.enable_task_runner ? local.task_runner_job : ""
    AVAILABLE_MODELS            = jsonencode(var.available_models)
    # The Platform Research Council reads platform config from the AgentOS fork
    # (repo_url), NOT from the goal-repo org.
    PLATFORM_CLONE_URL = var.repo_url
    PLATFORM_REPO_NAME = trimsuffix(element(split("/", var.repo_url), length(split("/", var.repo_url)) - 1), ".git")
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
    COUNCIL_DISPATCHER_ENABLED           = tostring(var.flags.council_dispatcher_enabled)
    COUNCIL_SCHEDULE_ENABLED             = tostring(var.flags.council_schedule_enabled)
    SECURITY_DISPATCHER_ENABLED          = tostring(var.flags.security_dispatcher_enabled)
    SECURITY_UI_ENABLED                  = tostring(var.flags.security_ui_enabled)
    SECURITY_SCHEDULE_ENABLED            = tostring(var.flags.security_schedule_enabled)
    SECURITY_SCHEDULE_HOURS              = tostring(var.flags.security_schedule_hours)
    PLATFORM_COUNCIL_ENABLED             = tostring(var.flags.platform_council_enabled)
    PLATFORM_RESEARCH_DISPATCHER_ENABLED = tostring(var.flags.platform_research_dispatcher_enabled)
    PLATFORM_AGENTS_RECREATE_ON_BUILD    = tostring(var.flags.platform_agents_recreate_on_build)
    GOAL_SUMMARY_ENABLED                 = tostring(var.flags.goal_summary_enabled)
    GITGRAPH_ENABLED                     = tostring(var.flags.gitgraph_enabled)
    TASK_DAG_CEILING                     = tostring(var.flags.task_dag_ceiling)
    TASK_DAG_CEILING_ENFORCE             = tostring(var.flags.task_dag_ceiling_enforce)
    LEDGER_ACTOR_AUDIT                   = tostring(var.flags.ledger_actor_audit)
  }

  service_env = merge(local.base_env, local.slack_env, local.oauth_env, local.flag_env)

  slack_secret_env = var.slack_enabled ? {
    SLACK_CLIENT_SECRET  = "slack-client-secret"
    SLACK_SIGNING_SECRET = "slack-signing-secret"
  } : {}

  # Gateway-OWNED (stage 1) secrets this service CONSUMES. Not created here —
  # the platform SA is granted accessor on them (they exist after stage 1).
  shared_secret_env = merge(
    { OPENCLAW_AUTH_TOKEN = "openclaw-gateway-token" },
    var.gemini_enabled ? { GEMINI_API_KEY = "gemini-api-key" } : {},
    var.anthropic_enabled ? { CLAUDE_API_KEY = "claude-api-key" } : {},
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
  # ALL of these secrets are created by prerequisites.sh; terraform only references them.
  secret_env = merge(local.platform_secret_env, local.shared_secret_env)

  # The only secrets terraform creates here (DB DSNs — password is generated).
  # DB DSN secrets (database-url / database-url-migrate) are created by
  # prerequisites.sh together with the Cloud SQL instance + users, so terraform
  # creates no secrets here. (The DATABASE_URL env var still maps to the
  # prereq-created database-url secret via platform_secret_env above.)
  db_secrets = []
}
