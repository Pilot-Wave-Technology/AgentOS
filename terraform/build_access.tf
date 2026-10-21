###############################################################################
# What the BUILD may read: each secret by name, never project-wide.
#
# Both builds run as <name_prefix>-build (made by prerequisites.sh, with its
# project roles). Its secret access lives here rather than there because
# half of these secrets are terraform's and do not exist when
# prerequisites.sh runs.
#
# Who reads what, at build time:
#   execution-plane.sh -> deploy_execution_plane.sh rebuilds the conductor's
#     Kubernetes secrets from these, every build;
#   preflight.sh reads each one to prove it exists and holds a value.
# A name missing here fails the execution-plane step on "permission
# denied" -- loudly, at the step that needs it.
###############################################################################

locals {
  build_sa_email = "${var.name_prefix}-build@${var.project_id}.iam.gserviceaccount.com"

  build_reads_secrets = [
    # the database, as the conductor and preflight see it
    "database-url",
    "database-url-migrate",
    "database-url-conductor",
    # the execution pods' own login and their Cloud SQL proxy key
    "execution-db-password",
    "execution-sqlproxy-key",
    # what the conductor is deployed with
    "openclaw-gateway-token",
    "gemini-api-key",
    "anthropic-api-key",
    "credential-encryption-key",
    "agent-token-signing-key",
    "control-panel-url",
  ]
}

resource "google_secret_manager_secret_iam_member" "build_reads" {
  for_each  = toset(local.build_reads_secrets)
  secret_id = "projects/${var.project_id}/secrets/${each.value}"
  project   = var.project_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.build_sa_email}"

  # Half of these are created in this apply; bind once they exist.
  depends_on = [module.database, module.execution_plane]
}

# Preflight DESCRIBES the artifact bucket and reads nothing in it. Not
# legacyBucketReader: that includes `storage.objects.list`, and on this
# bucket an object's name is its content hash -- the capability itself.
resource "google_project_iam_custom_role" "bucket_describe" {
  role_id     = "agentosBucketDescribe"
  title       = "AgentOS bucket describe"
  description = "Read a bucket's metadata. No object list, no object read."
  permissions = ["storage.buckets.get"]
  project     = var.project_id
}

# The ONE secret the build WRITES. sync-url stores the web service's URL
# in `control-panel-url` after each deploy, which the gateway's timer and
# the conductor read. Version-add on that secret only -- no other write.
resource "google_secret_manager_secret_iam_member" "build_writes_cp_url" {
  secret_id = "projects/${var.project_id}/secrets/control-panel-url"
  project   = var.project_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = "serviceAccount:${local.build_sa_email}"
}

# Same settle as the execution plane's custom roles (identities.tf).
resource "time_sleep" "bucket_describe_settle" {
  create_duration = "60s"
  triggers        = { role = google_project_iam_custom_role.bucket_describe.id }
}

resource "google_storage_bucket_iam_member" "build_sees_artifacts" {
  bucket = module.execution_plane.artifact_bucket
  role   = time_sleep.bucket_describe_settle.triggers.role
  member = "serviceAccount:${local.build_sa_email}"
}
