# THE CLUSTER LIVES ON THE DEPLOYMENT'S NETWORK, not on `default`.
#
# The control panel reaches a story's pod directly -- the workspace panel
# asks the pod's explorer for the file tree -- and Cloud Run can only reach
# a private IP through its VPC connector, which sits on THIS network. A
# cluster that names no network lands on `default`, where the connector has
# no route: the executor works, and the panel says "the pod holding this
# workspace did not answer ... timed out".
#
# Its own subnet, with NAMED pod and service ranges, so the firewall rule
# below can name the pod range instead of guessing what GKE picked.

variable "network_id" {
  type        = string
  description = "The network the VPC connector is on (module.network.network_id)."
}

variable "connector_cidr" {
  type        = string
  description = "The VPC connector's range: the source of Cloud Run's traffic to pods."
}

variable "gke_subnet_cidr" {
  type    = string
  default = "10.30.0.0/20"
}

variable "gke_pods_cidr" {
  type    = string
  default = "10.32.0.0/14"
}

variable "gke_services_cidr" {
  type    = string
  default = "10.36.0.0/20"
}

resource "google_compute_subnetwork" "gke" {
  name          = "${var.name_prefix}-gke"
  project       = var.project_id
  region        = var.region
  network       = var.network_id
  ip_cidr_range = var.gke_subnet_cidr

  # Pods pull images and reach Google APIs without public node IPs.
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.gke_pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.gke_services_cidr
  }
}

# Cloud Run -> pod. Only the connector's range, only to the pod range: the
# pods' own NetworkPolicy (CONNECTOR_CIDR in the conductor manifest) then
# narrows it to the one port the explorer answers on.
resource "google_compute_firewall" "connector_to_pods" {
  name               = "${var.name_prefix}-connector-to-pods"
  project            = var.project_id
  network            = var.network_id
  direction          = "INGRESS"
  source_ranges      = [var.connector_cidr]
  destination_ranges = [var.gke_pods_cidr]

  allow {
    protocol = "tcp"
  }
}
