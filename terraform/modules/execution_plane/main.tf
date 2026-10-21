###############################################################################
# The execution plane's INFRASTRUCTURE.
#
# What belongs here and what does not, because the split is the point:
#
#   HERE      the cluster, the node pools, the four identities, the two
#             custom roles, the artifact bucket, the Workload Identity
#             bindings, the execution pods' login PASSWORD, the sqlproxy
#             key, and every secret grant the conductor needs.
#             Declarative, reviewable in `terraform plan`, removed by
#             `terraform destroy`. (The image registry is the ROOT's:
#             the control plane pushes to it with or without this plane.)
#
#   FILES     main.tf (cluster + pools), identities.tf,
#             database_access.tf, outputs.tf.
#
#   NOT HERE, THOUGH IT IS ALSO DECLARATIVE: the namespace, its quota
#             and the Kubernetes service accounts. They need cluster
#             CREDENTIALS, which do not exist until the cluster does,
#             so putting them in this apply means a provider
#             configured from a resource created in the same run --
#             which works until the day the cluster is replaced. They
#             stay with the pipeline, which already has credentials by
#             the time it runs.
#
#   NOT HERE  deploying the conductor, the verifier and the egress proxy
#             at a particular image digest, and rebuilding the Kubernetes
#             secrets from Secret Manager. Those change every build and
#             are the PIPELINE's, exactly as the control plane already
#             splits them (see the header of cloudbuild.yaml). The build
#             never creates the cluster: with none, it skips that step.
#
# This replaces the provisioning half of scripts/infra/gke_cluster.sh
# and scripts/infra/gke_standard_pools.sh. Those scripts remain as the
# reference for what this must reproduce, and for standing a plane up
# by hand while debugging.
###############################################################################

variable "project_id" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "namespace" { type = string }
variable "name_prefix" { type = string }
variable "artifact_bucket" { type = string }
variable "force_destroy" {
  type    = bool
  default = false
}

# Node counts are TOTALS, not per zone. See the pools below.
variable "small_min_nodes" {
  type        = number
  default     = 0
  description = "Warm floor for the small pool. 0 costs nothing and pays a scale-from-zero wait on the first story."
}

variable "disk_gb" { type = number }

locals {
  gsa = {
    exec    = "agentos-exec@${var.project_id}.iam.gserviceaccount.com"
    cond    = "agentos-cond@${var.project_id}.iam.gserviceaccount.com"
    verify  = "agentos-verify@${var.project_id}.iam.gserviceaccount.com"
    capture = "agentos-capture@${var.project_id}.iam.gserviceaccount.com"
  }
  wi_pool = "${var.project_id}.svc.id.goog"
}

# ── the cluster ──────────────────────────────────────────────────────────
#
# STANDARD, NOT AUTOPILOT, and the reason is not price.
#
# Autopilot is cheaper per pod and needs no pool sizing, and it was the
# original choice. What retired it: one story to a machine needs a
# nodeSelector on `agentos.io/node-class`, and Autopilot's Warden
# refuses any selector key outside its own allowlist -- at ADMISSION,
# so the manifest is never applied. The selector is emitted for every
# execution pod, so an Autopilot cluster deploys GREEN and then fails
# every story at dispatch. Autopilot also refuses any pod asking for
# more than 10 GiB of ephemeral storage, flat, which a brownfield
# checkout exceeds.
resource "google_container_cluster" "agentos" {
  name     = var.cluster_name
  location = var.region
  project  = var.project_id

  release_channel { channel = "REGULAR" }
  datapath_provider = "ADVANCED_DATAPATH" # Dataplane V2
  networking_mode   = "VPC_NATIVE"
  network           = var.network_id
  subnetwork        = google_compute_subnetwork.gke.id
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  workload_identity_config { workload_pool = local.wi_pool }

  # IMAGE STREAMING IS NOT ON BY DEFAULT ON STANDARD, and Autopilot had
  # it on. The sandbox image is ~900 MB of toolchain; without streaming
  # every cold node pulls all of it before the story starts, which is
  # the cost the image decision assumed away. Set on the cluster AND on
  # each pool, because a pool does not inherit it.
  node_pool_defaults {
    node_config_defaults {
      gcfs_config { enabled = true }
    }
  }

  # GKE will not create an empty cluster, so one node is born with it
  # and immediately removed: a pod whose nodeSelector is missing or
  # misspelled would otherwise land on the default pool and RUN, so
  # one-story-per-node would hold everywhere except where nobody looks.
  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = false
}

# ── the pools ────────────────────────────────────────────────────────────
#
# TOTAL, NOT PER ZONE. On a REGIONAL cluster the gcloud `--min-nodes`
# and `--max-nodes` flags are bounds PER ZONE, and this cluster spans
# three. The first hand-run build therefore made a platform pool with a
# floor of six e2-standard-4 rather than two. Nothing failed and
# nothing looked wrong; the bill was three times the number written
# down, which is the kind of error found a month later on an invoice.
# `total_min_node_count` says what it means.
locals {
  pools = {
    # `platform` FIRST, because nothing else works without it. The
    # conductor, the verifier and the egress proxy have to live
    # somewhere; a control-plane pod carrying no class selector
    # otherwise lands on a STORY pool, takes a node sized for one
    # execution, and evicts the warm placeholders on its way in.
    #
    # TWO NODES, NOT ONE: the conductor runs two replicas and they
    # should not share a machine.
    platform = { machine = "e2-standard-4", min = 2, max = 4 }

    # TWENTY EACH, because the system ceiling is twenty and nothing
    # tells a pool which size the next twenty runs will ask for.
    # Sizing `medium` at 10 and `large` at 4 means a burst of large
    # stories stops at four with sixteen slots held open and the
    # scheduler reporting nothing -- Pending on capacity, which is not
    # a state admission can see.
    #
    # The FLOORS keep it cheap: medium and large stay at zero and cost
    # nothing until something asks. A ceiling is permission to grow,
    # not a reservation.
    small  = { machine = "e2-standard-4", min = var.small_min_nodes, max = 20 }
    medium = { machine = "e2-standard-8", min = 0, max = 20 }
    large  = { machine = "e2-standard-16", min = 0, max = 20 }
  }
}

resource "google_container_node_pool" "pools" {
  for_each = local.pools

  name     = each.key
  cluster  = google_container_cluster.agentos.name
  location = var.region
  project  = var.project_id

  node_count = each.value.min

  autoscaling {
    total_min_node_count = each.value.min
    total_max_node_count = each.value.max
    location_policy      = "ANY"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = each.value.machine
    disk_size_gb = var.disk_gb
    disk_type    = "pd-balanced"

    # GKE_METADATA is the Workload Identity node mode. Without it pods
    # read the NODE's service account from the metadata server, which
    # is the escalation the two-uid boundary exists to make
    # uninteresting.
    workload_metadata_config { mode = "GKE_METADATA" }
    gcfs_config { enabled = true }

    labels = { "agentos.io/node-class" = each.key }

    # NO TAINT. A named scope labels and TAINTS each pool so several
    # environments can share a cluster without stealing each other's
    # machines. With one environment there is nobody to steal from,
    # and a taint nothing tolerates is a cluster that schedules
    # nothing. scripts/infra/gke_standard_pools.sh adds the taint when
    # AGENTOS_POOL_SCOPE is set; a single-environment install leaves
    # it blank.
  }

  lifecycle {
    # The autoscaler owns the live count after creation.
    ignore_changes = [node_count]
  }
}

