# ---------------------------------------------------------------------------
# confexts.tf — Role-split runtime confext rendering + packaging.
#
# Renders the z-etcd / z-kubernetes-cp / z-kubelet-<node> source trees under
# build/runtime/trees/ then packages each tree into
# build/runtime/confexts/<name>.raw via package-confext.sh (mksquashfs
# -noappend -all-root, mirroring extensions/build.sh). The tree file content
# references the PKI/kubeconfig/encryption resources, so the secret
# material exists in state once, not duplicated for the confext copies.
#
# Every tree carries etc/extension-release.d/extension-release.z-<name> with
# ID=fedora, VERSION_ID=44 so systemd-confext accepts the merge on
# the Fedora 44 host. KERNEL_VERSION is kept for parity with the static
# confexts.
#
# z-etcd (cp1 only)      etc/etcd/etcd.conf.yml — pre-delete static confext
#                        content (commit 91a035f, TLS-disabled single-node)
#                        with the real cp_ip substituted for {{CP_IP}}.
# z-kubernetes-cp (cp1)  cp.env (KUBE_ADVERTISE_ADDRESS /
#                        KUBE_ETCD_SERVERS=http://<cp_ip>:2379 matching the
#                        apiserver unit's no-TLS --etcd-servers semantics),
#                        CP certs under pki/, kubeconfigs, encryption-config.
# z-kubelet-<node>       kubelet.conf (the node's kubeconfig), ca.pem and the
#                        per-node kubelet cert/key from the kubelets/ output.
# ---------------------------------------------------------------------------

locals {
  confexts_dir = abspath("${path.module}/${var.confexts_output_dir}")
  # Rendered trees live next to the packaged images: build/runtime/trees.
  trees_dir = abspath("${local.confexts_dir}/../trees")

  control_plane_nodes = [for name, role in var.node_roles : name if role == "control-plane"]
  has_control_plane   = length(local.control_plane_nodes) > 0

  # Flat list of confext image names: cp-only roles first, then one kubelet
  # image per node.
  confext_image_names = concat(
    local.has_control_plane ? ["z-etcd", "z-kubernetes-cp"] : [],
    [for name in sort(keys(var.node_ips)) : "z-kubelet-${name}"],
  )

  # node -> confext image names that must reach that node (the push step consumes
  # the resolved .raw paths via the node_confexts output).
  node_confext_names = {
    for name in sort(keys(var.node_ips)) : name => concat(
      contains(local.control_plane_nodes, name) ? ["z-etcd", "z-kubernetes-cp"] : [],
      ["z-kubelet-${name}"],
    )
  }

  # Release metadata for every runtime confext (ID + VERSION_ID must
  # match the Fedora 44 host; KERNEL_VERSION kept for parity with statics).
  confext_metadata = "ID=fedora\nVERSION_ID=44\nKERNEL_VERSION=7.1\n"

  # Every rendered tree file: key is the path relative to the tree root,
  # value the exact content. Content references the PKI resources so the
  # PKI secrets exist in state once, not twice.
  confext_tree_files = merge(
    {
      "z-etcd/etc/etcd/etcd.conf.yml"                           = templatefile("${path.module}/templates/etcd.conf.yml.tftpl", { cp_ip = var.cp_ip })
      "z-etcd/etc/extension-release.d/extension-release.z-etcd" = "${local.confext_metadata}EXTENSION=z-etcd\n"
    },
    {
      "z-kubernetes-cp/etc/kubernetes/cp.env"                                     = templatefile("${path.module}/templates/cp.env.tftpl", { cp_ip = var.cp_ip })
      "z-kubernetes-cp/etc/kubernetes/pki/ca.pem"                                 = local_file.ca_cert.content
      "z-kubernetes-cp/etc/kubernetes/pki/ca-key.pem"                             = local_file.ca_key.content
      "z-kubernetes-cp/etc/kubernetes/pki/kubernetes.pem"                         = local_file.apiserver_cert.content
      "z-kubernetes-cp/etc/kubernetes/pki/kubernetes-key.pem"                     = local_file.apiserver_key.content
      "z-kubernetes-cp/etc/kubernetes/pki/front-proxy-ca.pem"                     = local_file.front_proxy_ca_cert.content
      "z-kubernetes-cp/etc/kubernetes/pki/front-proxy-client.pem"                 = local_file.front_proxy_client_cert.content
      "z-kubernetes-cp/etc/kubernetes/pki/front-proxy-client-key.pem"             = local_file.front_proxy_client_key.content
      "z-kubernetes-cp/etc/kubernetes/pki/service-account.pem"                    = local_file.service_account_cert.content
      "z-kubernetes-cp/etc/kubernetes/pki/service-account-key.pem"                = local_file.service_account_key.content
      "z-kubernetes-cp/etc/kubernetes/admin.kubeconfig"                           = local_file.admin_kubeconfig.content
      "z-kubernetes-cp/etc/kubernetes/controller-manager.kubeconfig"              = local_file.controller_manager_kubeconfig.content
      "z-kubernetes-cp/etc/kubernetes/scheduler.kubeconfig"                       = local_file.scheduler_kubeconfig.content
      "z-kubernetes-cp/etc/kubernetes/encryption-config.yaml"                     = local_file.encryption_config.content
      "z-kubernetes-cp/etc/extension-release.d/extension-release.z-kubernetes-cp" = "${local.confext_metadata}EXTENSION=z-kubernetes-cp\n"
    },
    merge([
      for name in sort(keys(var.node_ips)) : {
        "z-kubelet-${name}/etc/kubernetes/kubelet.conf"                                 = local_file.kubelet_kubeconfig[name].content
        "z-kubelet-${name}/etc/kubernetes/pki/ca.pem"                                   = local_file.ca_cert.content
        "z-kubelet-${name}/etc/kubernetes/pki/${name}.pem"                              = local_file.kubelet_cert[name].content
        "z-kubelet-${name}/etc/kubernetes/pki/${name}-key.pem"                          = local_file.kubelet_key[name].content
        "z-kubelet-${name}/etc/extension-release.d/extension-release.z-kubelet-${name}" = "${local.confext_metadata}EXTENSION=z-kubelet-${name}\n"
      }
    ]...),
  )

  # Fingerprint drives the packaging trigger so a content change (e.g. a new
  # cp_ip after DHCP drift) re-runs mksquashfs on the next apply.
  confext_fingerprint = sha256(join("\u0000", [
    for key in sort(keys(local.confext_tree_files)) : "${key}=${local.confext_tree_files[key]}"
  ]))
}

# --- Render the source trees under build/runtime/trees/ --------------------
resource "local_file" "confext_tree" {
  for_each = local.confext_tree_files

  filename        = "${local.trees_dir}/${each.key}"
  content         = each.value
  file_permission = endswith(each.key, "-key.pem") ? "0600" : "0644"
}

# --- Package each tree into build/runtime/confexts/<name>.raw --------------
resource "null_resource" "package_confexts" {
  triggers = {
    fingerprint = local.confext_fingerprint
    # The packaging script also contributes content to the images (the
    # enablement symlinks it creates per tree), so a change to it must re-run
    # mksquashfs on the next apply.
    package_script_sha = filesha256("${path.module}/package-confext.sh")
  }

  depends_on = [local_file.confext_tree]

  provisioner "local-exec" {
    command = "${path.module}/package-confext.sh ${local.trees_dir} ${local.confexts_dir}"
  }
}
