# ---------------------------------------------------------------------------
# outputs.tf — Values exposed to the caller (make configure) and to the
# confext rendering and push steps.
# ---------------------------------------------------------------------------

output "pki_output_dir" {
  description = "Absolute PKI/kubeconfig output directory, anchored to this module's directory via path.module (repo-root build/runtime/pki with the default)."
  value       = local.pki_dir
}

output "confexts_output_dir" {
  description = "Absolute role-split confext output directory, anchored to this module's directory via path.module (repo-root build/runtime/confexts with the default)."
  value       = abspath("${path.module}/${var.confexts_output_dir}")
}

output "cp_ip" {
  description = "Control-plane IP input, re-exposed for downstream phases."
  value       = var.cp_ip
}

output "k8s_endpoint" {
  description = "Kubernetes API endpoint embedded into kubeconfigs."
  value       = local.k8s_endpoint
}

output "node_ips" {
  description = "Node name -> IP map."
  value       = var.node_ips
}

output "node_roles" {
  description = "Node name -> role map (control-plane | worker)."
  value       = var.node_roles
}

output "node_names" {
  description = "Ordered list of node names."
  value       = keys(var.node_ips)
}

output "ssh_user" {
  description = "SSH user for the phase-B push step."
  value       = var.ssh_user
}

output "ssh_private_key" {
  description = "Path of the SSH private key for the phase-B push step (empty when ssh agent is used)."
  value       = var.ssh_private_key
  sensitive   = true
}
