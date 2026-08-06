# --- OCI API auth (see terraform/README.md for how to generate these) --------
variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" { type = string }
variable "region" { type = string }           # e.g. eu-zurich-1
variable "compartment_ocid" { type = string } # where to build

# --- Access ------------------------------------------------------------------
variable "ssh_public_key_path" {
  type        = string
  description = "Public key placed on the box for SSH (opc user)."
}

variable "ssh_ingress_cidr" {
  type        = string
  default     = "0.0.0.0/0"
  description = "CIDR allowed to reach port 22. Lock this to your IP (e.g. 1.2.3.4/32)."
}

# --- Shape / image -----------------------------------------------------------
variable "shape" {
  type    = string
  default = "VM.Standard.E2.1.Micro" # Always-Free AMD micro (1 Go) — A1 gardé pour oueb
}

# --- App bootstrap -----------------------------------------------------------
variable "git_repo_slug" {
  type    = string
  default = "cluster2600/ted_bot"
}

variable "github_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Fine-grained PAT (contents:read) to clone a PRIVATE repo. Leave empty if public."
}

variable "telegram_bot_token" {
  type      = string
  sensitive = true
}

variable "telegram_chat_id" {
  type      = string
  sensitive = true
}

variable "nvidia_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional — clé NVIDIA (Nemotron) pour la couche adapt-in-the-loop. Récupérée depuis OCI Vault."
}

variable "ted_llm_model" {
  type    = string
  default = "claude-haiku-4-5"
}

variable "lookback_days" {
  type    = string
  default = "3" # >1 covers weekend publication gaps; dedup makes overlap free
}

variable "heartbeat_url" {
  type        = string
  default     = ""
  description = "Optional dead-man's-switch URL (e.g. healthchecks.io) pinged after each run."
}

variable "oci_backup_bucket" {
  type        = string
  default     = ""
  description = "Optional OCI Object Storage bucket for weekly ted.db backups (needs oci CLI on the box)."
}
