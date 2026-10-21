###############################################################################
# The execution pod's database access.
#
# A pod does NOT connect as the application. It connects as
# `agentos_leg`, which holds far less -- that narrowing is the whole
# reason the pod has a login of its own.
#
# THE ROLE IS MIGRATION 0004's, NOT A `google_sql_user`. A Cloud SQL user
# made through the API is a member of cloudsqlsuperuser and can create
# roles and databases -- more than the application holds. 0004 creates a
# plain LOGIN role with exactly the grants pods use. Terraform owns the
# PASSWORD: it generates it here, and the migrate job (which runs as the
# runtime account, granted below) sets it on the role every build.
###############################################################################

variable "leg_role" {
  type    = string
  default = "agentos_leg"
}

variable "runtime_sa_email" {
  type        = string
  description = "Runs the migrate job, which sets the role's password from the secret."
}

resource "random_password" "leg" {
  length  = 32
  special = false # it travels inside a DSN
}

resource "google_secret_manager_secret" "leg_password" {
  secret_id = "execution-db-password"
  project   = var.project_id
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "leg_password" {
  secret      = google_secret_manager_secret.leg_password.id
  secret_data = random_password.leg.result
}

resource "google_secret_manager_secret_iam_member" "runtime_leg_password" {
  secret_id = google_secret_manager_secret.leg_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.runtime_sa_email}"
  project   = var.project_id
}

# ── the sqlproxy sidecar's key ───────────────────────────────────────────
#
# A STATIC KEY, WHICH IS NOT THE USUAL ADVICE, and the reason is the
# two-container split. The sidecar runs beside the container the model
# drives; giving it Workload Identity would put a cloud credential in
# a pod the sandbox shares a network namespace with, which is the one
# thing that design exists to prevent. The key belongs to an account
# whose only permission is connecting to this one Cloud SQL instance.
#
# Terraform owns it so that it is created once, rotated deliberately,
# and removed with the plane -- rather than minted by whichever run of
# a script first noticed it was missing.
resource "google_service_account_key" "sqlproxy" {
  service_account_id = google_service_account.exec.name
}

resource "google_secret_manager_secret" "sqlproxy_key" {
  secret_id = "execution-sqlproxy-key"
  project   = var.project_id
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "sqlproxy_key" {
  secret      = google_secret_manager_secret.sqlproxy_key.id
  secret_data = base64decode(google_service_account_key.sqlproxy.private_key)
}

# The exec account may reach Cloud SQL, and nothing else.
resource "google_project_iam_member" "exec_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.exec.email}"
}

# The conductor reads both secrets when it builds the pod's manifest.
resource "google_secret_manager_secret_iam_member" "cond_leg_password" {
  secret_id = google_secret_manager_secret.leg_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cond.email}"
  project   = var.project_id
}

resource "google_secret_manager_secret_iam_member" "cond_sqlproxy_key" {
  secret_id = google_secret_manager_secret.sqlproxy_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cond.email}"
  project   = var.project_id
}

# ── what the conductor reads at deploy time ──────────────────────────────
#
# PER SECRET, NOT PROJECT-WIDE: a project-wide grant would hand the
# conductor every secret in the project, including the GitHub App key
# and the Firebase admin credential it has no business reading. The
# list mirrors what deploy_execution_plane.sh actually reads; a name
# missing here shows up as a conductor that cannot start, so the list
# is checked by preflight rather than trusted.
#
# The DSN secrets are terraform's (modules/database); the rest are made
# by prerequisites.sh, which runs before any terraform.
variable "conductor_secret_ids" {
  type        = list(string)
  description = "Secret ids the conductor reads. Granted accessor one by one."
}

resource "google_secret_manager_secret_iam_member" "cond_reads" {
  for_each  = toset(var.conductor_secret_ids)
  secret_id = "projects/${var.project_id}/secrets/${each.value}"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cond.email}"
  project   = var.project_id
}

