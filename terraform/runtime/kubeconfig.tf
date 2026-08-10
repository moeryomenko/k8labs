# ---------------------------------------------------------------------------
# kubeconfig.tf — Kubeconfig rendering.
#
# Mirrors ansible/roles/certs/templates/kubeconfig.j2 exactly: cluster name
# k8s-labs, context default, current-context default, embedded CA + client
# cert/key base64, server https://<cp_ip>:6443. Written 0644 like the role's
# template task.
# ---------------------------------------------------------------------------

locals {
  k8s_endpoint = "https://${var.cp_ip}:6443"
  ca_base64    = base64encode(tls_self_signed_cert.ca.cert_pem)
}

resource "local_file" "admin_kubeconfig" {
  filename = "${local.pki_dir}/admin.kubeconfig"
  content = templatefile("${path.module}/templates/kubeconfig.tftpl", {
    server          = local.k8s_endpoint
    ca_base64       = local.ca_base64
    user            = "admin"
    client_cert_b64 = base64encode(tls_locally_signed_cert.admin.cert_pem)
    client_key_b64  = base64encode(tls_private_key.admin.private_key_pem)
  })
  file_permission = "0644"
}

resource "local_file" "controller_manager_kubeconfig" {
  filename = "${local.pki_dir}/controller-manager.kubeconfig"
  content = templatefile("${path.module}/templates/kubeconfig.tftpl", {
    server          = local.k8s_endpoint
    ca_base64       = local.ca_base64
    user            = "system:kube-controller-manager"
    client_cert_b64 = base64encode(tls_locally_signed_cert.kube_controller_manager.cert_pem)
    client_key_b64  = base64encode(tls_private_key.kube_controller_manager.private_key_pem)
  })
  file_permission = "0644"
}

resource "local_file" "scheduler_kubeconfig" {
  filename = "${local.pki_dir}/scheduler.kubeconfig"
  content = templatefile("${path.module}/templates/kubeconfig.tftpl", {
    server          = local.k8s_endpoint
    ca_base64       = local.ca_base64
    user            = "system:kube-scheduler"
    client_cert_b64 = base64encode(tls_locally_signed_cert.kube_scheduler.cert_pem)
    client_key_b64  = base64encode(tls_private_key.kube_scheduler.private_key_pem)
  })
  file_permission = "0644"
}

resource "local_file" "kubelet_kubeconfig" {
  for_each = var.node_ips

  filename = "${local.pki_dir}/kubelets/${each.key}.kubeconfig"
  content = templatefile("${path.module}/templates/kubeconfig.tftpl", {
    server          = local.k8s_endpoint
    ca_base64       = local.ca_base64
    user            = "system:node:${each.key}"
    client_cert_b64 = base64encode(tls_locally_signed_cert.kubelet[each.key].cert_pem)
    client_key_b64  = base64encode(tls_private_key.kubelet[each.key].private_key_pem)
  })
  file_permission = "0644"
}
