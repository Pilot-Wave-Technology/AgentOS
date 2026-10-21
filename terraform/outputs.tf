output "service_url" {
  description = "Public URL of the control-panel Cloud Run service (default *.run.app)."
  value       = local.control_panel_url
}

output "service_name" {
  value = local.service_name
}

output "region" {
  value = var.region
}

output "project_id" {
  value = var.project_id
}

output "db_name" {
  value = module.database.db_name
}

output "network_name" {
  value = var.network_name
}

output "runtime_service_account" {
  description = "Runtime SA the service runs as (created by prerequisites.sh)."
  value       = local.runtime_sa_email
}

output "cloudsql_connection_name" {
  description = "Cloud SQL instance connection name (project:region:instance)."
  value       = module.database.connection_name
}

output "cloudsql_instance" {
  value = module.database.instance_name
}

output "vpc_connector_id" {
  value = module.network.connector_id
}

output "bucket_names" {
  value = module.storage.bucket_names
}

output "secret_names_created_here" {
  description = "The DSN secrets terraform writes. Every other secret is created by prerequisites.sh."
  value       = module.database.dsn_secret_ids
}

output "extension_bucket" {
  description = "Where ../publish-extension.sh uploads the VS Code extension."
  value       = module.storage.extension_bucket
}

output "image_registry" {
  value = google_artifact_registry_repository.images.id
}

output "execution_plane_cluster" {
  description = "The GKE cluster every executor runs on."
  value       = module.execution_plane.cluster_name
}

output "task_runner_job" {
  value = var.enable_task_runner ? local.task_runner_job : null
}

output "next_steps" {
  value = <<-EOT
    1. The DSN secrets are filled by this apply; every other secret was created
       by prerequisites.sh. Overwrite any you
       left blank with ../set-secret.sh (never raw `gcloud secrets versions add`).
       See deploy/06-fill-in-secrets.md.
    2. Build + push your image, then deploy it (Cloud Build) — see
       deploy/07-build-and-deploy.md. Terraform ignores subsequent image changes.
    3. The OpenClaw gateway VM (agent_os_openclaw) was deployed FIRST in Stage ①;
       Its URL is looked up here from the reserved address (agentos-gateway-ip).
    4. Visit ${local.control_panel_url} and log in as ${var.bootstrap_admin_email}.
  EOT
}
