output "cluster_name" { value = google_container_cluster.agentos.name }
output "cluster_endpoint" { value = google_container_cluster.agentos.endpoint }
output "artifact_bucket" { value = google_storage_bucket.artifacts.name }
output "service_accounts" { value = local.gsa }
output "leg_role" { value = var.leg_role }
