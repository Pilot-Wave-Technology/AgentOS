###############################################################################
# The control plane's database: the Cloud SQL instance, the `control_panel`
# database, the two logins the control plane uses, and the three DSN secrets
# that carry those logins to whoever connects.
#
# TERRAFORM OWNS ALL OF IT, together with the execution plane's own login
# (modules/execution_plane), so one `terraform apply` sets up the database and
# the cluster with every account and every grant on the GCP side. What it does
# NOT do is the in-database GRANTS -- those need a connection, which terraform
# neither has nor should want. They are migration 0003's, applied by the
# build's migrate step.
#
# This used to be prerequisites.sh, behind a RUNNABLE poll, because the
# instance create is slow and an interrupted API poll could fail an apply.
# The create timeout below covers the slow part. If an apply IS interrupted
# while the instance is still building, the instance keeps building: wait for
# it, then `./adopt-existing.sh` pulls it into state and the next apply
# carries on. The same script adopts a database made by an older
# prerequisites.sh.
###############################################################################

variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }
variable "tier" { type = string }
variable "database_version" { type = string }
variable "disk_gb" { type = number }
variable "deletion_protection" { type = bool }
variable "runtime_sa_email" { type = string }

variable "db_name" {
  type    = string
  default = "control_panel"
}

resource "google_sql_database_instance" "main" {
  name                = "${var.name_prefix}-db"
  project             = var.project_id
  region              = var.region
  database_version    = var.database_version
  deletion_protection = var.deletion_protection

  settings {
    edition           = "ENTERPRISE"
    tier              = var.tier
    availability_type = "ZONAL"
    disk_size         = var.disk_gb
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    # Public IP with NO authorised networks: nothing reaches it by address.
    # Cloud Run connects through the built-in Cloud SQL connector (a unix
    # socket) and execution pods through the cloud-sql-proxy sidecar; both
    # authenticate with IAM, not with a network rule.
    ip_configuration { ipv4_enabled = true }
  }

  timeouts {
    create = "60m"
    update = "60m"
  }

  lifecycle {
    # autoresize grows the disk; the size written here is a floor, and
    # terraform must not try to shrink it back (which Cloud SQL refuses).
    ignore_changes = [settings[0].disk_size]
  }
}

resource "google_sql_database" "main" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
  project  = var.project_id
}

output "connection_name" { value = google_sql_database_instance.main.connection_name }
output "instance_name" { value = google_sql_database_instance.main.name }
output "db_name" { value = google_sql_database.main.name }
