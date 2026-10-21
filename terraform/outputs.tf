output "openclaw_ws_url" {
  description = "The gateway's URL. The PLATFORM stack derives this itself from the reserved address (agentos-gateway-ip) -- nothing to copy."
  value       = "ws://${google_compute_address.gateway.address}:18789"
}

output "network_name" {
  description = "Set this as network_name in the PLATFORM terraform.tfvars (it reuses this VPC)."
  value       = google_compute_network.vpc.name
}

output "archive_bucket" {
  description = "Set this as archive_bucket in the PLATFORM terraform.tfvars."
  value       = google_storage_bucket.archive.name
}

output "vm_name" { value = google_compute_instance.gateway.name }
output "internal_ip" { value = google_compute_instance.gateway.network_interface[0].network_ip }
output "service_account" { value = local.gateway_sa_email }

output "next_steps" {
  value = <<-EOT
    Gateway (stage 1) is up. Next:
    1. Make sure the LLM/gateway secrets have real values (set in prerequisites.sh
       via secrets.env, or add now):
         gemini-api-key, anthropic-api-key, openclaw-gateway-token
       (control-panel-url is filled in stage 2.)
    2. Deploy the PLATFORM stack (stage 2), SAME project + region. In its
       terraform.tfvars set:
         network_name    = "${google_compute_network.vpc.name}"
         archive_bucket  = "${google_storage_bucket.archive.name}"
         (openclaw_ws_url is derived by the platform stack -- nothing to copy)
  EOT
}
