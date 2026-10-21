###############################################################################
# The execution plane's identities, its artifact store and Workload Identity.
###############################################################################

# ── identities ───────────────────────────────────────────────────────────
#
# FOUR, and the direction of each grant is why there are four rather
# than one. `exec` holds NOTHING -- that is what makes reaching the
# metadata server uninteresting. `verify` READS one committed tree;
# `capture` WRITES the directory it captured. Read and write are
# different capabilities and this is where they stay apart: a
# criterion that could write to the store is one that can put things
# where the control panel and the `artifact` criterion will read them.
resource "google_service_account" "exec" {
  account_id   = "agentos-exec"
  display_name = "AgentOS execution plane"
  project      = var.project_id
}

resource "google_service_account" "cond" {
  account_id   = "agentos-cond"
  display_name = "AgentOS conductor"
  project      = var.project_id
}

resource "google_service_account" "verify" {
  account_id   = "agentos-verify"
  display_name = "AgentOS verifier — reads committed trees, nothing else"
  project      = var.project_id
}

resource "google_service_account" "capture" {
  account_id   = "agentos-capture"
  display_name = "AgentOS environment capture"
  project      = var.project_id
}

# ── two custom roles that withhold `list` ────────────────────────────────
#
# `roles/storage.objectViewer` INCLUDES `storage.objects.list`, and
# granting it would make the sentence this design rests on false: with
# list, object names are enumerable and the content hash stops being
# the capability -- a criterion could walk the bucket and read every
# other story's tree, held-out checks included. Role ids must be
# alphanumeric; hyphens are rejected.
resource "google_project_iam_custom_role" "artifact_read" {
  role_id     = "agentosArtifactRead"
  title       = "AgentOS artifact read"
  description = "Get one object by name. No list: the hash is the capability."
  permissions = ["storage.objects.get"]
  project     = var.project_id
}

resource "google_project_iam_custom_role" "artifact_write" {
  role_id     = "agentosArtifactWrite"
  title       = "AgentOS artifact write"
  description = "Get and create one object by name. No list."
  permissions = ["storage.objects.get", "storage.objects.create"]
  project     = var.project_id
}

# ── the artifact store ───────────────────────────────────────────────────
resource "google_storage_bucket" "artifacts" {
  name                        = var.artifact_bucket
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = var.force_destroy
}

resource "google_storage_bucket_iam_member" "cond_objectuser" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.cond.email}"
}

# The control panel stores what pods emit (POST /internal/artifact) and
# serves it back, so it reads and writes this bucket too.
resource "google_storage_bucket_iam_member" "runtime_objectuser" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${var.runtime_sa_email}"
}

# A NEW CUSTOM ROLE IS NOT USABLE THE MOMENT IT EXISTS. Granting it in the
# same apply fails "Role ... does not exist in the resource's hierarchy"
# until IAM catches up, so the grants below wait this out once.
resource "time_sleep" "custom_roles_settle" {
  create_duration = "60s"
  triggers = {
    read  = google_project_iam_custom_role.artifact_read.id
    write = google_project_iam_custom_role.artifact_write.id
  }
}

resource "google_storage_bucket_iam_member" "verify_read" {
  bucket = google_storage_bucket.artifacts.name
  role   = time_sleep.custom_roles_settle.triggers.read
  member = "serviceAccount:${google_service_account.verify.email}"
}

resource "google_storage_bucket_iam_member" "capture_write" {
  bucket = google_storage_bucket.artifacts.name
  role   = time_sleep.custom_roles_settle.triggers.write
  member = "serviceAccount:${google_service_account.capture.email}"
}

# The conductor reaches Cloud SQL through the sidecar proxy.
resource "google_project_iam_member" "cond_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.cond.email}"
}

# ── Workload Identity ────────────────────────────────────────────────────
#
# THE BINDING, NOT JUST THE ACCOUNT. A hand-run build once created the
# capture GSA, granted it the bucket, wrote the KSA annotation -- and
# never bound the two. Everything described correctly and the pod could
# not get a token. An account that exists unbound is the quiet half.
locals {
  wi = {
    "agentos-execution" = google_service_account.exec.email
    "agentos-conductor" = google_service_account.cond.email
    "agentos-verifier"  = google_service_account.verify.email
    "agentos-capture"   = google_service_account.capture.email
    # The RBAC split gave the verifier Deployment a Kubernetes identity
    # of its own so a compromised verifier cannot delete execution pods.
    # It points at the CONDUCTOR's Google account on purpose: only the
    # Kubernetes half split, so no GCP capability moved and the binding
    # stays byte-identical rather than copied and drifting.
    "agentos-verifier-control" = google_service_account.cond.email
  }
}

resource "google_service_account_iam_member" "wi" {
  for_each = local.wi

  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.wi_pool}[${var.namespace}/${each.key}]"

  # THE POOL IS THE CLUSTER'S. `<project>.svc.id.goog` comes into being when
  # a cluster with Workload Identity does; the member string above only
  # NAMES it, so without this terraform binds in parallel with the cluster
  # create and every binding fails "Identity Pool does not exist".
  depends_on = [google_container_cluster.agentos]
}

