variable "project_id" { type = string }
variable "region" { type = string }
variable "service_name" { type = string }
variable "service_profile" { type = string }
variable "image" { type = string }
variable "runtime_sa_email" { type = string }
variable "cloudsql_conn" { type = string }
variable "vpc_connector_id" { type = string }
variable "env_vars" { type = map(string) }
variable "secret_env" { type = map(string) }
variable "cpu" { type = string }
variable "memory" { type = string }
variable "min_instances" { type = number }
variable "max_instances" { type = number }
variable "enable_task_runner" { type = bool }
variable "task_runner_job" { type = string }
variable "task_runner_image" { type = string }

locals {
  # In "single" one service runs the combined app.single entrypoint. In "split"
  # the same image runs the four dedicated entrypoints as separate services.
  services = var.service_profile == "single" ? {
    (var.service_name) = { entrypoint = "app.single:app", public = true, min = var.min_instances, max = var.max_instances }
    } : {
    "${var.service_name}-web"        = { entrypoint = "app.main:app", public = true, min = var.min_instances, max = var.max_instances }
    "${var.service_name}-council"    = { entrypoint = "app.council_worker:app", public = false, min = 1, max = 1 }
    "${var.service_name}-git"        = { entrypoint = "app.git_engine_worker:app", public = false, min = 1, max = 1 }
    "${var.service_name}-management" = { entrypoint = "app.management_worker:app", public = false, min = 1, max = 2 }
  }

  # The first apply on a clean project runs the Google "hello" placeholder image
  # (no uvicorn). Pairing it with the app command/args makes the container fail
  # to start, so we omit command/args for the placeholder and let Cloud Build set
  # the real command/args when it deploys the real image (see cloudbuild.yaml).
  placeholder_image = "us-docker.pkg.dev/cloudrun/container/hello"
  use_placeholder   = var.image == local.placeholder_image
}

resource "google_cloud_run_v2_service" "svc" {
  for_each = local.services
  name     = each.key
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  # Cloud Run services are stateless; let Terraform replace/update them freely
  # (the default true blocks the placeholder->real replace + repeatable teardown).
  deletion_protection = false

  template {
    service_account = var.runtime_sa_email
    timeout         = "3600s"

    scaling {
      min_instance_count = each.value.min
      max_instance_count = each.value.max
    }

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [var.cloudsql_conn]
      }
    }

    containers {
      image   = var.image
      command = local.use_placeholder ? null : ["uvicorn"]
      args    = local.use_placeholder ? null : [each.value.entrypoint, "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      # Plain (non-secret) env.
      dynamic "env" {
        for_each = var.env_vars
        content {
          name  = env.key
          value = env.value
        }
      }

      # Secret-backed env (Secret Manager, :latest).
      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  # Cloud Build deploys new images on push; don't let terraform revert them.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
      # Cloud Build sets the real image + command/args on deploy; don't revert.
      template[0].containers[0].command,
      template[0].containers[0].args,
    ]
  }
}

# Public services allow unauthenticated ingress (auth is enforced in-app via Firebase).
resource "google_cloud_run_v2_service_iam_member" "public" {
  for_each = { for k, v in local.services : k => v if v.public }
  name     = google_cloud_run_v2_service.svc[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ─── Task-runner job (async document rendering) ────────────────────────────
resource "google_cloud_run_v2_job" "task_runner" {
  count    = var.enable_task_runner ? 1 : 0
  name     = var.task_runner_job
  location = var.region

  # false so `terraform destroy` / reset.sh can tear the job down for repeatable
  # testing. Without this the provider defaults to true and destroy aborts with
  # "cannot destroy job without setting deletion_protection=false".
  deletion_protection = false

  template {
    template {
      service_account = var.runtime_sa_email
      max_retries     = 1
      timeout         = "600s"

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [var.cloudsql_conn]
        }
      }

      containers {
        image = var.task_runner_image
        resources {
          limits = {
            cpu    = "2"
            memory = "2Gi"
          }
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
        env {
          name  = "GCS_RESOURCE_BUCKET"
          value = lookup(var.env_vars, "GCS_RESOURCE_BUCKET", "")
        }
        dynamic "env" {
          for_each = { for k, v in var.secret_env : k => v if contains(["DATABASE_URL", "GEMINI_API_KEY"], k) }
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].template[0].containers[0].image,
    ]
  }
}

output "service_names" { value = keys(local.services) }
