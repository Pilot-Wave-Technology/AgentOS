###############################################################################
# AgentOS gateway (OpenClaw VM) — STAGE 1 of 2. Deploy this FIRST.
#
# This stack creates VPC + subnet + archive bucket + firewalls + VM. It does NOT
# create the service account or secrets — it references the SA + secrets that the
# platform branch's prerequisites.sh created (run that FIRST).
# After `terraform apply`, feed its outputs (network_name, archive_bucket,
# openclaw_ws_url) into the platform stack's terraform.tfvars.
#
# Copy terraform.tfvars.example -> terraform.tfvars and fill it in.
###############################################################################

variable "project_id" {
  type        = string
  description = "GCP project ID (you have owner)."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "name_prefix" {
  type        = string
  description = "Prefix for created resources (VPC, bucket, SA). Must match the platform stack's name_prefix."
  default     = "agentos"
}

variable "gateway_internal_ip" {
  type        = string
  description = "Pin the gateway's internal IP. Empty (the default) lets GCP pick one from the subnet and RESERVES it, so it survives the VM being recreated. Set it only to adopt an address a VM already has."
  default     = ""
}

variable "name" {
  type        = string
  description = "Gateway VM name."
  default     = "agentos-gateway"
}

# ─── Shared network (created here; platform reuses it) ──────────────────────
variable "network_name" {
  type        = string
  description = "VPC to create. The platform stack attaches its Serverless connector to this same VPC."
  default     = "agentos-vpc"
}

variable "subnet_cidr" {
  type        = string
  description = "Primary subnet CIDR for the VPC."
  default     = "10.20.0.0/20"
}

variable "allowed_source_ranges_gateway" {
  type        = list(string)
  description = "CIDRs allowed to reach the gateway port. Default = subnet + the platform connector's /28."
  default     = ["10.20.0.0/20", "10.8.0.0/28"]
}

variable "ssh_iap_only" {
  type        = bool
  description = "If true, no external IP (SSH only via IAP). Then you must add Cloud NAT for outbound (repo clone + LLM APIs)."
  default     = false
}

# ─── Shared archive bucket (created here; platform references by name) ───────
variable "archive_bucket" {
  type        = string
  description = "GCS bucket for archived agent memory/transcripts. Empty = <name_prefix>-agent-data-<project>."
  default     = ""
}

# (LLM/gateway secrets are created by prerequisites.sh, not here.)

# ─── VM sizing / source ─────────────────────────────────────────────────────
variable "machine_type" {
  type    = string
  default = "e2-standard-4"
}
variable "boot_disk_size_gb" {
  type    = number
  default = 100
}
variable "boot_image" {
  type    = string
  default = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}
variable "compaction_model" {
  type        = string
  description = "Model that writes compaction summaries (agents.defaults.compaction.model). A base provider/model id, so it resolves from the first boot."
  default     = "google/gemini-3.8-flash"
}

variable "processes_per_vm" {
  type    = number
  default = 1
  validation {
    condition     = var.processes_per_vm >= 1 && var.processes_per_vm <= 16
    error_message = "processes_per_vm must be 1..16."
  }
}
variable "repo_url" {
  type        = string
  description = "Your AgentOS FORK URL (the VM clones it on boot). Same value as the platform stack's repo_url. Private fork => set GATEWAY_REPO_TOKEN in secrets.env. See FORKING.md."
  default     = "https://github.com/YOUR_ORG/AgentOS.git"
}
variable "repo_branch" {
  type    = string
  default = "agent_os_openclaw"
}
variable "enable_chrome_remote_desktop" {
  type    = bool
  default = false
}

# ─── Repeatable-testing toggle ──────────────────────────────────────────────
variable "force_destroy_buckets" {
  type        = bool
  description = "Allow terraform destroy to delete the archive bucket even if non-empty (set true for test projects)."
  default     = false
}
