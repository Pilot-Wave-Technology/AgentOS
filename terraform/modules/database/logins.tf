###############################################################################
# The control plane's two logins and the DSN secrets that carry them.
#
#   app_user      DML. What the services run as.        -> database-url
#   migrate_user  DDL. What the migrate step runs as.   -> database-url-migrate
#   app_user, again, in SIDECAR form                    -> database-url-conductor
#
# THE CONDUCTOR'S DSN IS THE SAME LOGIN AT A DIFFERENT HOST. Cloud Run reaches
# Cloud SQL over a unix socket (`?host=/cloudsql/...`); the conductor reaches
# it through a cloud-sql-proxy sidecar on 127.0.0.1:5432. Handing the
# conductor the socket form fails with a connection error that names a path,
# not a configuration. Both are written from ONE random_password, so they
# cannot describe different passwords for the same user.
#
# The execution pods' login (`agentos_leg`) is NOT here: migration 0004 creates
# it, and its password secret lives with the execution plane.
###############################################################################

locals {
  logins = {
    app     = "app_user"
    migrate = "migrate_user"
  }
  socket_host = "/cloudsql/${google_sql_database_instance.main.connection_name}"

  dsn = {
    "database-url"           = "postgresql://${local.logins.app}:${random_password.login["app"].result}@/${var.db_name}?host=${local.socket_host}"
    "database-url-migrate"   = "postgresql://${local.logins.migrate}:${random_password.login["migrate"].result}@/${var.db_name}?host=${local.socket_host}"
    "database-url-conductor" = "postgresql://${local.logins.app}:${random_password.login["app"].result}@127.0.0.1:5432/${var.db_name}"
  }
  dsn_secret_ids = ["database-url", "database-url-migrate", "database-url-conductor"]
}

resource "random_password" "login" {
  for_each = local.logins
  length   = 32
  special  = false # it travels inside a DSN
}

resource "google_sql_user" "login" {
  for_each = local.logins
  name     = each.value
  instance = google_sql_database_instance.main.name
  project  = var.project_id
  password = random_password.login[each.key].result
}

resource "google_secret_manager_secret" "dsn" {
  for_each  = toset(local.dsn_secret_ids)
  secret_id = each.value
  project   = var.project_id
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "dsn" {
  # Keys are the static id list, not the sensitive map, so they are known
  # at plan time.
  for_each    = toset(local.dsn_secret_ids)
  secret      = google_secret_manager_secret.dsn[each.key].id
  secret_data = local.dsn[each.key]

  # The login must exist before anything can read a DSN that names it.
  depends_on = [google_sql_user.login]
}

# The services read all three: database-url at runtime, database-url-migrate
# in the migrate job (which runs as this account), database-url-conductor only
# where the conductor runs in-process.
resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = toset(local.dsn_secret_ids)
  secret_id = google_secret_manager_secret.dsn[each.key].id
  project   = var.project_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.runtime_sa_email}"
}

output "dsn_secret_ids" { value = local.dsn_secret_ids }

# Depend on this to wait for every DSN to hold a value (not just exist).
output "dsn_versions" {
  value = [for v in google_secret_manager_secret_version.dsn : v.name]
}
