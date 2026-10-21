###############################################################################
# AgentOS — control plane infrastructure inputs
#
# Every knob lives here. Copy terraform.tfvars.example -> terraform.tfvars and
# fill it in; you should never need to edit the .tf files. Variables are grouped:
#   1. Project / region / naming        (required)
#   2. External service inputs          (Firebase, GitHub App, Slack, OAuth)
#   3. LLM providers                    (Anthropic / Gemini / OpenAI)
#   4. Database                         (Cloud SQL)
#   5. Networking
#   6. Storage buckets
#   7. Gateway (OpenClaw VM) link
#   8. Cloud Run sizing + service profile
#   9. Feature flags                    (all service behaviour — defaults = the
#                                        current running configuration)
#
# Secret *values* never go here — prerequisites.sh creates + fills every Secret
# Manager secret; terraform only references them. Overwrite any you left blank
# later with ../set-secret.sh (see deploy/06-fill-in-secrets.md). Only non-secret
# config and the enable/disable flags live in tfvars.
###############################################################################

# ─── 1. Project / region / naming ──────────────────────────────────────────
variable "project_id" {
  type        = string
  description = "GCP project ID to deploy into (you must have owner)."
}

variable "region" {
  type        = string
  description = "GCP region for Cloud Run, Cloud SQL, connector, buckets."
  default     = "us-central1"
}

variable "name_prefix" {
  type        = string
  description = "Prefix for created resources (service, SA, buckets). Keep short/lowercase."
  default     = "agentos"
}

# ─── 2. External service inputs ────────────────────────────────────────────
variable "firebase_project_id" {
  type        = string
  description = "Firebase project ID for auth (usually same as project_id)."
  default     = ""
}

variable "firebase_web_api_key" {
  type        = string
  description = "Firebase Web API key (public, client-side). From Firebase console > Project settings."
  default     = ""
}

variable "bootstrap_admin_email" {
  type        = string
  description = "Email of the first user — auto-promoted to platform admin on first login."
}

variable "github_org" {
  type        = string
  description = "GitHub org/owner where AGENTS create + work on goal repos (via the GitHub App). This is NOT the AgentOS fork — that's `repo_url`. REQUIRED: no default, because an empty value builds API paths like `https://api.github.com/repos//name` that 404 at the moment an agent first tries to deliver work, hours into a run."
}

variable "repo_url" {
  type        = string
  description = "Your AgentOS FORK URL — the SAME value as the gateway stack's repo_url. The Platform Research Council reads platform config from its agent_os_platform branch. Fork AgentOS, then point this at your fork."
  default     = "https://github.com/YOUR_ORG/AgentOS.git"
}

variable "github_app_id" {
  type        = string
  description = "GitHub App ID (created per deploy/02-github-app.md). Empty = git features off."
  default     = ""
}

variable "github_app_installation_id" {
  type        = string
  description = "Installation ID of the GitHub App on your org."
  default     = ""
}

variable "slack_enabled" {
  type        = bool
  description = "Enable the optional Slack app integration."
  default     = false
}

variable "slack_client_id" {
  type        = string
  description = "Slack app client ID (only if slack_enabled)."
  default     = ""
}

variable "slack_command_suffix" {
  type        = string
  description = "Suffix for slash commands (/goal-<suffix>). Empty = plain /goal."
  default     = ""
}

variable "gcp_oauth_enabled" {
  type        = bool
  description = "Enable Connect-via-Google flow for per-goal service-account provisioning."
  default     = false
}

variable "gcp_oauth_client_id" {
  type        = string
  description = "OAuth 2.0 client ID (only if gcp_oauth_enabled)."
  default     = ""
}


# ─── 3. LLM providers ──────────────────────────────────────────────────────
# API keys are stored in Secret Manager (shells created by terraform; you add
# the values). Gemini + Anthropic are used by the control plane; all three are
# consumed by the OpenClaw gateway (see the agent_os_openclaw branch).
variable "anthropic_enabled" {
  type        = bool
  description = "Provision the Anthropic (Claude) API key secret + wire it in."
  default     = true
}

variable "gemini_enabled" {
  type        = bool
  description = "Provision the Gemini API key secret (used for summaries) + wire it in."
  default     = true
}

variable "openai_enabled" {
  type        = bool
  description = "Provision an OpenAI API key secret (optional provider)."
  default     = false
}

variable "available_models" {
  type        = list(string)
  description = "Models shown in the UI dropdowns. Keep in sync with the gateway's configured models."
  default = [
    # KEEP IN SYNC WITH `app/config.py::available_models`, which is the
    # documented source of truth. This default used to name
    # `google/gemini-2.5-flash` and `-pro`, so a terraform apply pushed
    # the UI dropdown back to models the catalogue had already retired
    # -- picking one gave an agent that looked configured and failed on
    # its first turn.
    "google/gemini-3.8-flash",
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-haiku-4-5",
    "claude-cli/opus",
    "claude-cli/sonnet",
  ]
}

# ─── 4. Database (Cloud SQL) ───────────────────────────────────────────────
# Terraform creates the instance, the control_panel database, app_user +
# migrate_user and their DSN secrets (modules/database).
variable "db_tier" {
  type        = string
  description = "Cloud SQL machine tier (ENTERPRISE edition)."
  default     = "db-custom-2-8192"
}

variable "db_version" {
  type        = string
  description = "Postgres major version. Changing it on a live instance is an upgrade, not an edit."
  default     = "POSTGRES_17"
}

variable "db_disk_gb" {
  type        = number
  description = "Starting disk size. Autoresize grows it; terraform never shrinks it."
  default     = 250
}

variable "db_deletion_protection" {
  type        = bool
  description = "Refuse `terraform destroy` on the instance. Set false only on a throwaway project you mean to tear down."
  default     = true
}

# ─── 5. Networking ─────────────────────────────────────────────────────────
variable "create_network" {
  type        = bool
  description = "Create a VPC. Default FALSE: the GATEWAY stack (stage 1) already created the shared VPC; this stack only adds the Serverless connector to it."
  default     = false
}

variable "network_name" {
  type        = string
  description = "VPC to attach to — MUST equal the gateway stack's network_name (its network_name output)."
  default     = "agentos-vpc"
}

variable "force_destroy_buckets" {
  type        = bool
  description = "Allow terraform destroy to delete non-empty buckets (set true for repeatable test projects)."
  default     = false
}

variable "connector_cidr" {
  type        = string
  description = "‬/28 CIDR for the Serverless VPC Access connector."
  default     = "10.8.0.0/28"
}

# ─── 6. Storage buckets ────────────────────────────────────────────────────
# Names must be globally unique. Empty => derived from name_prefix + project_id.
variable "workspace_bucket_prefix" {
  type        = string
  description = "Prefix for per-goal agent workspace buckets. Empty = <name_prefix>-agent-ws."
  default     = ""
}

variable "resource_bucket" {
  type        = string
  description = "Bucket for uploaded goal resources. Empty = <name_prefix>-agent-resources-<project>."
  default     = ""
}

variable "archive_bucket" {
  type        = string
  description = "Bucket for archived agent memory/transcripts on deletion. Empty = <name_prefix>-agent-data-<project>."
  default     = ""
}

# ─── 7. Gateway (OpenClaw VM) link ─────────────────────────────────────────
# The gateway is provisioned by the agent_os_openclaw branch. After it is up,
# put its internal WebSocket URL here so the control plane can reach it.
variable "openclaw_ws_url" {
  type        = string
  description = "Override only. Empty (the default) derives ws://<ip>:18789 from the gateway stack's reserved address, <name_prefix>-gateway-ip -- so each deployment uses its own gateway and nothing is copied by hand."
  default     = ""
}

# ─── 8. Cloud Run sizing ────────────────────────────────────────────────────
variable "service_cpu" {
  type        = string
  description = "CPU for the main service."
  default     = "2"
}

variable "service_memory" {
  type        = string
  description = "Memory for the main service."
  default     = "4Gi"
}

variable "service_min_instances" {
  type        = number
  description = "Min instances (>=1 keeps the co-located background loops warm)."
  default     = 1
}

variable "service_max_instances" {
  type        = number
  description = "Max instances. Single-run loops are DB-lock guarded, so >1 is safe."
  default     = 3
}

variable "enable_task_runner" {
  type        = bool
  description = "Deploy the task-runner Cloud Run Job (async document rendering)."
  default     = true
}

variable "image" {
  type        = string
  description = "Container image for the service. Default = Google hello image so the FIRST apply succeeds on a clean project; Cloud Build then deploys the real image (terraform ignores subsequent image changes)."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "task_runner_image" {
  type        = string
  description = "Container image for the task-runner job. Same default rationale as `image`."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

# ─── 9. Feature flags ──────────────────────────────────────────────────────
# Defaults reflect the CURRENT running configuration. Each is passed straight
# through to the service as an env var. Flip in tfvars and re-apply (or re-deploy).
variable "flags" {
  description = "All service behaviour flags. See terraform.tfvars.example for a grouped, commented copy."
  type = object({
    # security / auth
    agent_scoped_tokens_enabled = optional(bool, true)
    agent_authz_enforce         = optional(bool, true)
    custom_tools_broker_enforce = optional(bool, true)
    agent_reject_shared_token   = optional(bool, false)
    # chat
    chat_queue_enabled = optional(bool, true)
    chat_gateway_id    = optional(string, "gw-1")
    # memory v2
    memory_v2_bridge_enabled      = optional(bool, true)
    memory_v2_flush_enabled       = optional(bool, true)
    memory_v2_snapshot_enabled    = optional(bool, true)
    memory_v2_user_memory_enabled = optional(bool, false)
    # goal review council
    council_dispatcher_enabled = optional(bool, true)
    council_schedule_enabled   = optional(bool, true)
    # security council
    security_dispatcher_enabled = optional(bool, false)
    security_ui_enabled         = optional(bool, false)
    security_schedule_enabled   = optional(bool, false)
    security_schedule_hours     = optional(number, 12)
    # platform research council
    platform_council_enabled             = optional(bool, false)
    platform_research_dispatcher_enabled = optional(bool, false)
    platform_agents_recreate_on_build    = optional(bool, true)
    # goal progress summaries
    goal_summary_enabled = optional(bool, true)
    # git dependency-graph engine
    gitgraph_enabled = optional(bool, true)
    # claude-code DAG concurrency
    task_dag_ceiling         = optional(number, 4)
    task_dag_ceiling_enforce = optional(bool, false)
    # misc
    ledger_actor_audit = optional(bool, true)
    log_level          = optional(string, "INFO")
  })
  default = {}
}

# ─── 10. Execution plane ────────────────────────────────────────────────────
variable "execution_node_disk_gb" {
  type        = number
  description = "Boot disk per execution node. The sandbox image is ~900MB and a brownfield checkout plus its toolchain is the rest."
  default     = 100
}

variable "execution_small_min_nodes" {
  type        = number
  description = "Warm nodes held in the `small` pool. 0 costs nothing and makes the first story of the day wait for a node; 1 keeps one warm."
  default     = 0
}
