output "public_ip" {
  value       = oci_core_instance.ted_bot.public_ip
  description = "Public IP of the ted-bot instance."
}

output "ssh" {
  value       = "ssh opc@${oci_core_instance.ted_bot.public_ip}"
  description = "SSH in (from a whitelisted CIDR)."
}

output "bootstrap_log_hint" {
  value       = "First boot runs cloud-init: tail -f /var/log/ted_bot_bootstrap.log"
  description = "Where the provisioning log lives on the box."
}
