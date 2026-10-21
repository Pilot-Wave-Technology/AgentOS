###############################################################################
# AgentOS gateway — STAGE 1. Creates the shared VPC + archive bucket, and the
# gateway VM. Service accounts and secrets are created by prerequisites.sh
# (run before terraform); this stack only references them.
###############################################################################

locals {
  required_apis  = ["compute.googleapis.com", "storage.googleapis.com"]
  archive_bucket = coalesce(var.archive_bucket, "${var.name_prefix}-agent-data-${var.project_id}")

  # SA + secrets are created by prerequisites.sh; email is deterministic.
  gateway_sa_email = "${var.name_prefix}-gateway@${var.project_id}.iam.gserviceaccount.com"

  # Fixed secret ids (prerequisites.sh created them, blank or real).
  sec_gateway_token = "openclaw-gateway-token"
  sec_anthropic     = "anthropic-api-key"
  sec_gemini        = "gemini-api-key"
  sec_openai        = "openai-api-key"
  sec_cp_url        = "control-panel-url"
  sec_repo_token    = "gateway-repo-token"

  gateway_port_range = var.processes_per_vm == 1 ? "18789" : "18789-${18789 + var.processes_per_vm - 1}"
}

resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ─── Shared VPC (platform attaches its connector here) ──────────────────────
resource "google_compute_network" "vpc" {
  name                    = var.network_name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "${var.network_name}-subnet"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.subnet_cidr
}

# ─── Shared archive bucket (+ gateway SA object access) ─────────────────────
resource "google_storage_bucket" "archive" {
  name                        = local.archive_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = var.force_destroy_buckets
  depends_on                  = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "archive_rw" {
  bucket = google_storage_bucket.archive.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.gateway_sa_email}"
}

# ─── Startup script ─────────────────────────────────────────────────────────
locals {
  startup_script = templatefile("${path.module}/startup.sh.tpl", {
    project_id                   = var.project_id
    repo_url                     = var.repo_url
    repo_branch                  = var.repo_branch
    gateway_token_secret         = local.sec_gateway_token
    anthropic_key_secret         = local.sec_anthropic
    gemini_key_secret            = local.sec_gemini
    openai_key_secret            = local.sec_openai
    control_panel_url_secret     = local.sec_cp_url
    repo_token_secret            = local.sec_repo_token
    archive_bucket               = local.archive_bucket
    processes_per_vm             = var.processes_per_vm
    compaction_model             = var.compaction_model
    enable_chrome_remote_desktop = var.enable_chrome_remote_desktop
  })
}

# ─── The VM (runs as the prereq-created gateway SA) ─────────────────────────
# THE GATEWAY'S ADDRESS IS RESERVED, not ephemeral. The control plane
# connects to ws://<this>:18789, and an ephemeral IP changes whenever the VM
# is recreated -- which left a working control plane pointed at an address
# nothing listened on, and the dashboard reporting the gateway offline.
# Reserved, it survives recreation; and the platform stack looks it up by
# this NAME, so nobody copies an IP between the two stacks.
resource "google_compute_address" "gateway" {
  name         = "${var.name_prefix}-gateway-ip"
  region       = var.region
  subnetwork   = google_compute_subnetwork.subnet.id
  address_type = "INTERNAL"
  address      = var.gateway_internal_ip == "" ? null : var.gateway_internal_ip
}

resource "google_compute_instance" "gateway" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = var.boot_disk_size_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    network    = google_compute_network.vpc.id
    subnetwork = google_compute_subnetwork.subnet.id
    network_ip = google_compute_address.gateway.address
    dynamic "access_config" {
      for_each = var.ssh_iap_only ? [] : [1]
      content {}
    }
  }

  service_account {
    email  = local.gateway_sa_email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
    startup-script = local.startup_script
  }

  labels                    = { component = "openclaw-gateway" }
  tags                      = ["${var.name}-gw", "iap-ssh"]
  allow_stopping_for_update = true
}

# ─── Firewalls ──────────────────────────────────────────────────────────────
resource "google_compute_firewall" "gateway" {
  count   = length(var.allowed_source_ranges_gateway) > 0 ? 1 : 0
  name    = "${var.name}-allow-gateway"
  network = google_compute_network.vpc.name
  allow {
    protocol = "tcp"
    ports    = [local.gateway_port_range]
  }
  source_ranges = var.allowed_source_ranges_gateway
  target_tags   = ["${var.name}-gw"]
  description   = "Control plane -> gateway ports ${local.gateway_port_range}."
}

resource "google_compute_firewall" "iap_ssh" {
  name    = "${var.name}-allow-iap-ssh"
  network = google_compute_network.vpc.name
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = ["35.235.240.0/20"] # Google IAP range
  target_tags   = ["iap-ssh"]
  description   = "SSH via IAP tunnel."
}
