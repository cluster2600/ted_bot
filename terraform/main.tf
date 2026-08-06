terraform {
  required_version = ">= 1.3"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0"
    }
  }
}

provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}

# --- Lookups -----------------------------------------------------------------
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

data "oci_core_images" "ol9" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Oracle Linux"
  operating_system_version = "9"
  shape                    = var.shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# --- Network: VCN + IGW + route + security list + subnet ---------------------
resource "oci_core_vcn" "vcn" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.0.0.0/16"]
  display_name   = "ted-bot-vcn"
  dns_label      = "tedbot"
}

resource "oci_core_internet_gateway" "igw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "ted-bot-igw"
}

resource "oci_core_route_table" "rt" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "ted-bot-rt"
  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

resource "oci_core_security_list" "sl" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "ted-bot-sl"

  # Outbound: open (needs TED, Telegram, Anthropic, PyPI, GitHub).
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  # Inbound: SSH only, from the whitelisted CIDR.
  ingress_security_rules {
    protocol = "6" # TCP
    source   = var.ssh_ingress_cidr
    tcp_options {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_subnet" "sn" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.vcn.id
  cidr_block                 = "10.0.1.0/24"
  display_name               = "ted-bot-subnet"
  dns_label                  = "app"
  route_table_id             = oci_core_route_table.rt.id
  security_list_ids          = [oci_core_security_list.sl.id]
  prohibit_public_ip_on_vnic = false
}

# --- Compute: the Always-Free micro instance ---------------------------------
resource "oci_core_instance" "ted_bot" {
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  display_name        = "ted-bot"
  shape               = var.shape

  # A1.Flex est une shape flexible -> OCPU/RAM explicites (quota ARM Always-Free).
  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_in_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.sn.id
    assign_public_ip = true
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ol9.images[0].id
    boot_volume_size_in_gbs = 50 # A1.Flex exige >= 50 Go
  }

  metadata = {
    ssh_authorized_keys = file(var.ssh_public_key_path)
    user_data = base64encode(templatefile("${path.module}/cloud-init.sh", {
      git_repo_slug      = var.git_repo_slug
      github_token       = var.github_token
      telegram_bot_token = var.telegram_bot_token
      telegram_chat_id   = var.telegram_chat_id
      anthropic_api_key  = var.anthropic_api_key
      ted_llm_model      = var.ted_llm_model
      lookback_days      = var.lookback_days
      heartbeat_url      = var.heartbeat_url
      oci_backup_bucket  = var.oci_backup_bucket
    }))
  }
}
