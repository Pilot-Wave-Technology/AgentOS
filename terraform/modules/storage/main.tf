variable "project_id" { type = string }
variable "region" { type = string }
variable "resource_bucket" { type = string }
variable "force_destroy" {
  type    = bool
  default = false
}

# Resource-uploads bucket. The ARCHIVE bucket is created by the GATEWAY stack
# (stage 1); per-goal workspace buckets (<prefix>-<goalid>) are handled at
# goal-provision time (runtime SA has project objectAdmin).
resource "google_storage_bucket" "resource" {
  name                        = var.resource_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = var.force_destroy
}

output "bucket_names" {
  value = [google_storage_bucket.resource.name]
}
