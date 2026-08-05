# --- OCI API auth (see terraform/README.md for how to generate these) --------
variable "tenancy_ocid"     { type = string }
variable "user_ocid"        { type = string }
variable "fingerprint"      { type = string }
variable "private_key_path" { type = string }
variable "region"           { type = string }               # e.g. eu-zurich-1
variable "compartment_ocid" { type = string }               # where to build

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
  type        = string
  default     = "VM.Standard.E2.1.Micro"   # OCI Always-Free AMD micro
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

variable "anthropic_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional — enables the Haiku adapt-in-the-loop layer."
}

variable "ted_llm_model" {
  type    = string
  default = "claude-haiku-4-5"
}

variable "lookback_days" {
  type    = string
  default = "1"
}
