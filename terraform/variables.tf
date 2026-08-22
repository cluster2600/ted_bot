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
