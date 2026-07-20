variable "project_id"  { type = string }
variable "name_prefix" { type = string }

# The Cloud SQL instance, the control_panel database, the app_user/migrate_user
# roles, and the two DB DSN secrets (database-url / database-url-migrate) are all
# created by prerequisites.sh BEFORE terraform runs. The instance create is slow
# (several minutes) and, on some org policies, the API poll that terraform does
# during apply can be interrupted — so that wait lives in a plain script with an
# explicit RUNNABLE loop instead. This module only READS the existing instance to
# wire its connection name into the Cloud Run service + task-runner job.
#
# NOTE: the instance MUST already exist (run prerequisites.sh first) or this data
# source fails at plan time — that ordering is intentional.
data "google_sql_database_instance" "main" {
  name    = "${var.name_prefix}-db"
  project = var.project_id
}

output "connection_name" { value = data.google_sql_database_instance.main.connection_name }
output "instance_name"   { value = data.google_sql_database_instance.main.name }
