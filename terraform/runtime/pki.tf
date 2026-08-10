# ---------------------------------------------------------------------------
# pki.tf — Cluster PKI generation with the tls provider.
#
# Mirrors ansible/roles/certs/tasks/main.yml:28-433 output exactly (spec
# requirements): one self-signed CA, admin/apiserver/controller-manager/scheduler/
# service-account client certs, a front-proxy CA + client cert, one etcd
# server/client cert (the certs role generates a single etcd pair used for
# both directions), and per-node kubelet certs under kubelets/.
#
# Naming parity with the role:
#   - ca.pem / ca-key.pem                       (self-signed CA, CN=Kubernetes)
#   - admin.pem / admin-key.pem                 (CN=admin, O=system:masters)
#   - kubernetes.pem / kubernetes-key.pem       (apiserver, CN=kubernetes)
#   - kube-controller-manager.pem / -key.pem    (CN=system:kube-controller-manager)
#   - kube-scheduler.pem / -key.pem             (CN=system:kube-scheduler)
#   - service-account.pem / -key.pem            (CN=service-accounts)
#   - front-proxy-ca.pem / -key.pem             (self-signed, CN=front-proxy-ca)
#   - front-proxy-client.pem / -key.pem         (CN=front-proxy-client)
#   - etcd.pem / etcd-key.pem                   (CN=etcd)
#   - kubelets/<node>.pem / kubelets/<node>-key.pem (CN=system:node:<node>)
#
# All keys RSA 2048; all certs validity_period_hours = 87600 (3650 days).
# ---------------------------------------------------------------------------

locals {
  # Render target is anchored to this module's directory via
  # abspath("${path.module}/..."), NOT to the tofu apply CWD. local_file
  # resolves relative filenames against the process working directory, which
  # `tofu -chdir=terraform/runtime apply` pins to terraform/runtime/ — so a
  # bare "../../build/runtime/pki" would land at <repo>/terraform/build/...
  # instead of the repo-root build/runtime/pki the verify gate and
  # `make destroy-full` expect (the spec renders into build/runtime/).
  # path.module is absolute, so the default pki_output_dir "../../build/runtime/pki"
  # resolves to <repo>/build/runtime/pki regardless of where tofu is invoked.
  pki_dir = abspath("${path.module}/${var.pki_output_dir}")
}

# --- 1. Certificate Authority (self-signed) --------------------------------
resource "tls_private_key" "ca" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem = tls_private_key.ca.private_key_pem

  subject {
    common_name  = "Kubernetes"
    organization = "Kubernetes"
  }

  validity_period_hours = var.cert_validity_hours
  is_ca_certificate     = true

  allowed_uses = [
    "cert_signing",
    "crl_signing",
  ]
}

# --- 2. Admin client certificate -------------------------------------------
resource "tls_private_key" "admin" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "admin" {
  private_key_pem = tls_private_key.admin.private_key_pem

  subject {
    common_name  = "admin"
    organization = "system:masters"
  }
}

resource "tls_locally_signed_cert" "admin" {
  cert_request_pem      = tls_cert_request.admin.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "client_auth",
  ]
}

# --- 3. Kube-apiserver certificate (SANs: cp_ip, svc IP, loopback, DNS) -----
resource "tls_private_key" "apiserver" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "apiserver" {
  private_key_pem = tls_private_key.apiserver.private_key_pem

  subject {
    common_name  = "kubernetes"
    organization = "Kubernetes"
  }

  ip_addresses = [
    var.cp_ip,
    var.kubernetes_svc_ip,
    "127.0.0.1",
  ]

  dns_names = [
    "kubernetes",
    "kubernetes.default",
    "kubernetes.default.svc",
    "kubernetes.default.svc.${var.cluster_domain}",
  ]
}

resource "tls_locally_signed_cert" "apiserver" {
  cert_request_pem      = tls_cert_request.apiserver.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
    "client_auth",
  ]
}

# --- 4. Kube-controller-manager certificate --------------------------------
resource "tls_private_key" "kube_controller_manager" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "kube_controller_manager" {
  private_key_pem = tls_private_key.kube_controller_manager.private_key_pem

  subject {
    common_name  = "system:kube-controller-manager"
    organization = "system:kube-controller-manager"
  }
}

resource "tls_locally_signed_cert" "kube_controller_manager" {
  cert_request_pem      = tls_cert_request.kube_controller_manager.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "client_auth",
  ]
}

# --- 5. Kube-scheduler certificate -----------------------------------------
resource "tls_private_key" "kube_scheduler" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "kube_scheduler" {
  private_key_pem = tls_private_key.kube_scheduler.private_key_pem

  subject {
    common_name  = "system:kube-scheduler"
    organization = "system:kube-scheduler"
  }
}

resource "tls_locally_signed_cert" "kube_scheduler" {
  cert_request_pem      = tls_cert_request.kube_scheduler.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "client_auth",
  ]
}

# --- 6. Service account key pair -------------------------------------------
resource "tls_private_key" "service_account" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "service_account" {
  private_key_pem = tls_private_key.service_account.private_key_pem

  subject {
    common_name  = "service-accounts"
    organization = "Kubernetes"
  }
}

resource "tls_locally_signed_cert" "service_account" {
  cert_request_pem      = tls_cert_request.service_account.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "digital_signature",
    "client_auth",
  ]
}

# --- 7a. Front-proxy CA (self-signed) --------------------------------------
resource "tls_private_key" "front_proxy_ca" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "front_proxy_ca" {
  private_key_pem = tls_private_key.front_proxy_ca.private_key_pem

  subject {
    common_name = "front-proxy-ca"
  }

  validity_period_hours = var.cert_validity_hours
  is_ca_certificate     = true

  allowed_uses = [
    "cert_signing",
    "crl_signing",
  ]
}

# --- 7b. Front-proxy client certificate ------------------------------------
resource "tls_private_key" "front_proxy_client" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "front_proxy_client" {
  private_key_pem = tls_private_key.front_proxy_client.private_key_pem

  subject {
    common_name = "front-proxy-client"
  }
}

resource "tls_locally_signed_cert" "front_proxy_client" {
  cert_request_pem      = tls_cert_request.front_proxy_client.cert_request_pem
  ca_private_key_pem    = tls_private_key.front_proxy_ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.front_proxy_ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "client_auth",
  ]
}

# --- 8. Etcd server/client certificate (single pair, both usages) ----------
resource "tls_private_key" "etcd" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "etcd" {
  private_key_pem = tls_private_key.etcd.private_key_pem

  subject {
    common_name  = "etcd"
    organization = "Kubernetes"
  }

  ip_addresses = [
    var.cp_ip,
    "127.0.0.1",
  ]

  dns_names = [
    "localhost",
  ]
}

resource "tls_locally_signed_cert" "etcd" {
  cert_request_pem      = tls_cert_request.etcd.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
    "client_auth",
  ]
}

# --- 9. Per-node kubelet certificates --------------------------------------
resource "tls_private_key" "kubelet" {
  for_each  = var.node_ips
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "kubelet" {
  for_each = var.node_ips

  private_key_pem = tls_private_key.kubelet[each.key].private_key_pem

  subject {
    common_name  = "system:node:${each.key}"
    organization = "system:nodes"
  }
}

resource "tls_locally_signed_cert" "kubelet" {
  for_each = var.node_ips

  cert_request_pem      = tls_cert_request.kubelet[each.key].cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
    "client_auth",
  ]
}

# --- Certificate/key file output -------------------------------------------
# Keys are written 0600, certs 0644 (parity with the role's openssl_privatekey
# and x509_certificate modes).

resource "local_file" "ca_cert" {
  filename        = "${local.pki_dir}/ca.pem"
  content         = tls_self_signed_cert.ca.cert_pem
  file_permission = "0644"
}

resource "local_file" "ca_key" {
  filename        = "${local.pki_dir}/ca-key.pem"
  content         = tls_private_key.ca.private_key_pem
  file_permission = "0600"
}

resource "local_file" "admin_cert" {
  filename        = "${local.pki_dir}/admin.pem"
  content         = tls_locally_signed_cert.admin.cert_pem
  file_permission = "0644"
}

resource "local_file" "admin_key" {
  filename        = "${local.pki_dir}/admin-key.pem"
  content         = tls_private_key.admin.private_key_pem
  file_permission = "0600"
}

resource "local_file" "apiserver_cert" {
  filename        = "${local.pki_dir}/kubernetes.pem"
  content         = tls_locally_signed_cert.apiserver.cert_pem
  file_permission = "0644"
}

resource "local_file" "apiserver_key" {
  filename        = "${local.pki_dir}/kubernetes-key.pem"
  content         = tls_private_key.apiserver.private_key_pem
  file_permission = "0600"
}

resource "local_file" "kube_controller_manager_cert" {
  filename        = "${local.pki_dir}/kube-controller-manager.pem"
  content         = tls_locally_signed_cert.kube_controller_manager.cert_pem
  file_permission = "0644"
}

resource "local_file" "kube_controller_manager_key" {
  filename        = "${local.pki_dir}/kube-controller-manager-key.pem"
  content         = tls_private_key.kube_controller_manager.private_key_pem
  file_permission = "0600"
}

resource "local_file" "kube_scheduler_cert" {
  filename        = "${local.pki_dir}/kube-scheduler.pem"
  content         = tls_locally_signed_cert.kube_scheduler.cert_pem
  file_permission = "0644"
}

resource "local_file" "kube_scheduler_key" {
  filename        = "${local.pki_dir}/kube-scheduler-key.pem"
  content         = tls_private_key.kube_scheduler.private_key_pem
  file_permission = "0600"
}

resource "local_file" "service_account_cert" {
  filename        = "${local.pki_dir}/service-account.pem"
  content         = tls_locally_signed_cert.service_account.cert_pem
  file_permission = "0644"
}

resource "local_file" "service_account_key" {
  filename        = "${local.pki_dir}/service-account-key.pem"
  content         = tls_private_key.service_account.private_key_pem
  file_permission = "0600"
}

resource "local_file" "front_proxy_ca_cert" {
  filename        = "${local.pki_dir}/front-proxy-ca.pem"
  content         = tls_self_signed_cert.front_proxy_ca.cert_pem
  file_permission = "0644"
}

resource "local_file" "front_proxy_ca_key" {
  filename        = "${local.pki_dir}/front-proxy-ca-key.pem"
  content         = tls_private_key.front_proxy_ca.private_key_pem
  file_permission = "0600"
}

resource "local_file" "front_proxy_client_cert" {
  filename        = "${local.pki_dir}/front-proxy-client.pem"
  content         = tls_locally_signed_cert.front_proxy_client.cert_pem
  file_permission = "0644"
}

resource "local_file" "front_proxy_client_key" {
  filename        = "${local.pki_dir}/front-proxy-client-key.pem"
  content         = tls_private_key.front_proxy_client.private_key_pem
  file_permission = "0600"
}

resource "local_file" "etcd_cert" {
  filename        = "${local.pki_dir}/etcd.pem"
  content         = tls_locally_signed_cert.etcd.cert_pem
  file_permission = "0644"
}

resource "local_file" "etcd_key" {
  filename        = "${local.pki_dir}/etcd-key.pem"
  content         = tls_private_key.etcd.private_key_pem
  file_permission = "0600"
}

resource "local_file" "kubelet_cert" {
  for_each = var.node_ips

  filename        = "${local.pki_dir}/kubelets/${each.key}.pem"
  content         = tls_locally_signed_cert.kubelet[each.key].cert_pem
  file_permission = "0644"
}

resource "local_file" "kubelet_key" {
  for_each = var.node_ips

  filename        = "${local.pki_dir}/kubelets/${each.key}-key.pem"
  content         = tls_private_key.kubelet[each.key].private_key_pem
  file_permission = "0600"
}
