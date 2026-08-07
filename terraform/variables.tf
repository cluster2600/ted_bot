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
  type        = string
  default     = "nvidia/nemotron-3-ultra-550b-a55b"
  description = "Modèle servi par integrate.api.nvidia.com (format vendeur/modèle). Doit rester aligné sur LLM_MODEL dans ted_scanner.py."

  validation {
    # Un id NVIDIA est toujours "vendeur/modèle" — un nom d'un autre fournisseur
    # (ex. l'ancien "claude-haiku-4-5") passait silencieusement et faisait échouer
    # 100 % des appels LLM en production.
    condition     = can(regex("^[a-z0-9_.-]+/[a-z0-9_.-]+$", var.ted_llm_model))
    error_message = "ted_llm_model doit être un id NVIDIA de la forme vendeur/modèle, ex. nvidia/nemotron-3-ultra-550b-a55b."
  }
}

variable "name_prefix" {
  type        = string
  default     = "ted-bot"
  description = "Préfixe des display_name. À changer pour faire cohabiter deux stacks (migration, rebuild)."

  validation {
    # Sert aussi de dns_label VCN (ponctuation retirée) : doit tenir en 15 car.
    # alphanumériques et commencer par une lettre.
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.name_prefix)) && length(replace(var.name_prefix, "-", "")) <= 15
    error_message = "name_prefix : minuscules/chiffres/tirets, doit commencer par une lettre, et <= 15 caractères une fois les tirets retirés."
  }
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
