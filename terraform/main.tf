###############################################################################
# AgentOS control plane — root module. Wires the sub-modules together.
# One `terraform apply` provisions: APIs, network + connector, Cloud SQL (the
# instance, the database, every login and its DSN secret), the image registry,
# buckets, the Cloud Run service(s), and the GKE cluster every executor runs
# on, with its identities and grants. Service accounts and the
# non-database secrets come from prerequisites.sh, which runs first.
###############################################################################

# ─── Enable required APIs ──────────────────────────────────────────────────
locals {
  required_apis = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "vpcaccess.googleapis.com",
    "compute.googleapis.com",
    "servicenetworking.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "bigquery.googleapis.com",
    "container.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each                   = toset(local.required_apis)
  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

# ─── Modules ───────────────────────────────────────────────────────────────
module "network" {
  source         = "./modules/network"
  project_id     = var.project_id
  region         = var.region
  create_network = var.create_network
  network_name   = var.network_name
  connector_cidr = var.connector_cidr
  depends_on     = [google_project_service.apis]
}

# The Cloud SQL instance, the control_panel database, app_user/migrate_user
# and the three DSN secrets. In-database grants are migration 0003's.
module "database" {
  source              = "./modules/database"
  project_id          = var.project_id
  region              = var.region
  name_prefix         = var.name_prefix
  tier                = var.db_tier
  database_version    = var.db_version
  disk_gb             = var.db_disk_gb
  deletion_protection = var.db_deletion_protection
  runtime_sa_email    = local.runtime_sa_email
  depends_on          = [google_project_service.apis]
}

# ─── The image registry ────────────────────────────────────────────────────
# Every image this installation runs is pushed here: the control plane, the
# task runner, and the sandbox base. The build pushes to it, so it must exist
# before the first build -- which terraform runs ahead of.
resource "google_artifact_registry_repository" "images" {
  repository_id = var.name_prefix
  location      = var.region
  project       = var.project_id
  format        = "DOCKER"
  description   = "AgentOS images: control plane, task runner, sandbox base"
  depends_on    = [google_project_service.apis]
}

module "storage" {
  source           = "./modules/storage"
  project_id       = var.project_id
  region           = var.region
  resource_bucket  = local.resource_bucket
  extension_bucket = local.extension_bucket
  force_destroy    = var.force_destroy_buckets
  depends_on       = [google_project_service.apis]
}

# NOTE: service accounts, IAM roles and every NON-database secret are created
# by prerequisites.sh (run before terraform); the DSN secrets are
# modules/database's. local.db_secrets is empty, so this module is a no-op kept
# only so a future terraform-owned secret has a home.
module "secrets" {
  source            = "./modules/secrets"
  project_id        = var.project_id
  secret_names      = local.db_secrets
  accessor_sa_email = local.runtime_sa_email
  depends_on        = [google_project_service.apis]
}

module "cloud_run" {
  source           = "./modules/cloud_run"
  project_id       = var.project_id
  region           = var.region
  service_name     = local.service_name
  image            = var.image
  runtime_sa_email = local.runtime_sa_email
  cloudsql_conn    = module.database.connection_name
  vpc_connector_id = module.network.connector_id

  env_vars        = local.service_env
  per_service_env = local.per_service_env
  secret_env      = local.secret_env

  cpu           = var.service_cpu
  memory        = var.service_memory
  min_instances = var.service_min_instances
  max_instances = var.service_max_instances

  enable_task_runner = var.enable_task_runner
  task_runner_job    = local.task_runner_job
  task_runner_image  = var.task_runner_image

  depends_on = [module.database, module.secrets, module.network]
}

# ── The execution plane ──────────────────────────────────────────────────
#
# REQUIRED. Every executor runs here: each leg of a story's work is a
# pod of its own on this cluster -- two containers, two users, a
# default-deny network and no cloud credential in the one the model
# drives. The control plane decides; this plane does the work.
#
# It is a GKE Standard cluster with four node pools, and it costs money
# whether or not a story is running (the platform pool floors at two
# nodes).
module "execution_plane" {
  source = "./modules/execution_plane"

  project_id      = var.project_id
  region          = var.region
  name_prefix     = var.name_prefix
  cluster_name    = var.name_prefix
  network_id      = module.network.network_id
  connector_cidr  = var.connector_cidr
  namespace       = var.name_prefix
  artifact_bucket = "${var.name_prefix}-artifacts-${var.project_id}"
  force_destroy   = var.force_destroy_buckets
  disk_gb         = var.execution_node_disk_gb
  small_min_nodes = var.execution_small_min_nodes

  # The migrate job runs as this account and sets the pods' login
  # password from the secret the module writes.
  runtime_sa_email = local.runtime_sa_email

  # What the conductor reads when it deploys. The DSNs come from
  # module.database; the rest exist before terraform (prerequisites.sh).
  conductor_secret_ids = concat(
    ["database-url", "database-url-conductor"],
    [
      "openclaw-gateway-token",
      "gemini-api-key",
      "anthropic-api-key",
      "credential-encryption-key",
      "agent-token-signing-key",
    ],
  )

  depends_on = [module.database, google_project_service.apis]
}
