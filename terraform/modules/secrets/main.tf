variable "project_id" { type = string }
variable "secret_names" { type = list(string) }
variable "accessor_sa_email" { type = string }
variable "shared_accessor_secret_ids" {
  type        = list(string)
  description = "Secrets OWNED by another stack (the gateway) that this SA also needs to read. Accessor granted; not created here."
  default     = []
}
variable "prefilled" {
  type        = map(string)
  description = "name -> value for secrets terraform can populate (e.g. the generated DB DSNs). Others are left empty for you to fill."
  default     = {}
  sensitive   = true
}

resource "google_secret_manager_secret" "s" {
  for_each  = toset(var.secret_names)
  secret_id = each.value
  replication {
    auto {}
  }
}

# Populate only the secrets terraform owns (DB DSNs). All others stay empty
# shells you fill in the "Populate secrets" deploy step.
resource "google_secret_manager_secret_version" "v" {
  # Iterate the (non-sensitive) secret names; look up the sensitive value.
  for_each    = nonsensitive(toset(keys(var.prefilled)))
  secret      = google_secret_manager_secret.s[each.key].id
  secret_data = var.prefilled[each.key]
}

# Grant the runtime SA accessor on every secret this stack creates.
# for_each over the STATIC name list (not the resource map) so the keys are
# known at plan time on a first apply; index into .s for the computed id.
resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = toset(var.secret_names)
  secret_id = google_secret_manager_secret.s[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.accessor_sa_email}"
}

# Also grant accessor on gateway-owned (shared) secrets this SA must read.
resource "google_secret_manager_secret_iam_member" "shared_accessor" {
  for_each  = toset(var.shared_accessor_secret_ids)
  secret_id = "projects/${var.project_id}/secrets/${each.value}"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.accessor_sa_email}"
}

output "secret_ids" {
  value = { for k, s in google_secret_manager_secret.s : k => s.id }
}
