# ---------------------------------------------------------------------------
# variables.tf — Input contract for the terraform/runtime root module.
#
# Names and shapes match the QA fixture terraform/runtime/test.tfvars
# (the input contract) plus the role-parity inputs that
# ansible/roles/certs used as defaults (kubernetes_svc_ip, cluster_domain,
# cert_validity_hours).
# ---------------------------------------------------------------------------

variable "cp_ip" {
  description = "Control-plane node IP. Embedded into apiserver cert SANs, kubeconfig server URLs, etcd.conf.yml and cp.env."
  type        = string
}

variable "node_ips" {
  description = "Map of node name to IP for every cluster node (cp1, w1, w2). The control-plane node's IP must equal cp_ip."
  type        = map(string)
}

variable "node_roles" {
  description = "Map of node name to role (control-plane | worker). Drives role-split runtime confext rendering."
  type        = map(string)
}

variable "ssh_user" {
  description = "SSH user for the phase-B push step. Same identity as wait-ssh uses."
  type        = string
  default     = "root"
}

variable "ssh_private_key" {
  description = "Path to the SSH private key used by the phase-B push step. May be empty when the ssh agent is used."
  type        = string
  default     = ""
}

variable "pki_output_dir" {
  description = "Directory where PKI artifacts, kubeconfigs and encryption-config.yaml are rendered. A relative path is anchored to this module's directory (terraform/runtime/) via path.module, not to the tofu CWD; the default ../../build/runtime/pki therefore lands at the repo-root build/runtime/pki."
  type        = string
  default     = "../../build/runtime/pki"
}

variable "confexts_output_dir" {
  description = "Directory where role-split runtime confext images are rendered. A relative path is anchored to this module's directory (terraform/runtime/) via path.module; the default ../../build/runtime/confexts lands at the repo-root build/runtime/confexts."
  type        = string
  default     = "../../build/runtime/confexts"
}

variable "kubernetes_svc_ip" {
  description = "Kubernetes API Service IP embedded into the apiserver cert SANs. Parity with ansible/inventory/group_vars/all.yml kubernetes_svc_ip."
  type        = string
  default     = "10.96.0.1"
}

variable "cluster_domain" {
  description = "Cluster DNS domain appended to the apiserver DNS SAN. Parity with ansible/roles/certs/defaults/main.yml cluster_domain."
  type        = string
  default     = "cluster.local"
}

variable "cert_validity_hours" {
  description = "Certificate validity in hours. 87600 hours = 3650 days, parity with ansible cert_validity_days=3650."
  type        = number
  default     = 87600
}
