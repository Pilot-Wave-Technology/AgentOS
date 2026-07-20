###############################################################################
# AgentOS control plane — root module. Wires the sub-modules together.
# One `terraform apply` provisions: APIs, network + connector, Cloud SQL,
# buckets, Secret Manager shells, runtime IAM, and the Cloud Run service(s).
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

# Reads the Cloud SQL instance created by prerequisites.sh (see the module).
module "database" {
  source      = "./modules/database"
  project_id  = var.project_id
  name_prefix = var.name_prefix
}

module "storage" {
  source          = "./modules/storage"
  project_id      = var.project_id
  region          = var.region
  resource_bucket = local.resource_bucket
  force_destroy   = var.force_destroy_buckets
  depends_on      = [google_project_service.apis]
}

# NOTE: service accounts, IAM roles, and ALL secrets — including the DB DSN
# secrets (database-url / database-url-migrate) and their runtime-SA accessor —
# are created by prerequisites.sh (run before terraform). local.db_secrets is now
# empty, so this module is a no-op kept only so a future terraform-owned secret
# has a home. Nothing here creates or grants secrets.
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
  service_profile  = var.service_profile
  image            = var.image
  runtime_sa_email = local.runtime_sa_email
  cloudsql_conn    = module.database.connection_name
  vpc_connector_id = module.network.connector_id

  env_vars   = local.service_env
  secret_env = local.secret_env

  cpu           = var.service_cpu
  memory        = var.service_memory
  min_instances = var.service_min_instances
  max_instances = var.service_max_instances

  enable_task_runner = var.enable_task_runner
  task_runner_job    = local.task_runner_job
  task_runner_image  = var.task_runner_image

  depends_on = [module.database, module.secrets, module.network]
}
