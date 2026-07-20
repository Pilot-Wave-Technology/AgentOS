variable "project_id" { type = string }
variable "region" { type = string }
variable "create_network" { type = bool }
variable "network_name" { type = string }
variable "connector_cidr" { type = string }

resource "google_compute_network" "vpc" {
  count                   = var.create_network ? 1 : 0
  name                    = var.network_name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  count         = var.create_network ? 1 : 0
  name          = "${var.network_name}-subnet"
  region        = var.region
  network       = google_compute_network.vpc[0].id
  ip_cidr_range = "10.20.0.0/20"
}

data "google_compute_network" "existing" {
  count = var.create_network ? 0 : 1
  name  = var.network_name
}

locals {
  network_name_resolved = var.create_network ? google_compute_network.vpc[0].name : data.google_compute_network.existing[0].name
  network_id            = var.create_network ? google_compute_network.vpc[0].id : data.google_compute_network.existing[0].id
}

# Serverless VPC Access connector — lets Cloud Run reach the gateway VM's
# internal IP (and any other private-range service).
resource "google_vpc_access_connector" "connector" {
  name          = "${var.network_name}-conn"
  region        = var.region
  ip_cidr_range = var.connector_cidr
  network       = local.network_name_resolved
  min_instances = 2
  max_instances = 3
}

output "network_id" { value = local.network_id }
output "network_name" { value = local.network_name_resolved }
output "connector_id" { value = google_vpc_access_connector.connector.id }
