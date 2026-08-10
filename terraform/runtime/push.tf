# ---------------------------------------------------------------------------
# push.tf — Phase-B push + ordered service activation.
#
# One null_resource per node (for_each over var.node_ips) invokes
# terraform/runtime/push-confext.sh via local-exec: hash-conditional scp of
# the node's role confext images (z-etcd / z-kubernetes-cp on control-plane,
# z-kubelet-<node> everywhere; rendered by confexts.tf) to /var/lib/confexts/,
# then `systemd-confext refresh` + `systemctl daemon-reload`, then
# `systemctl enable --now` in dependency order with health gates.
#
# Ordering: control-plane nodes push first (their apiserver must exist before
# workers register); workers depend on the control-plane resource.
#
# Idempotency: the deterministic trigger is the confext
# content fingerprint (confexts.tf) plus the node identity, so an apply with
# unchanged images does not re-run the local-exec at all; within a run the
# push script's own remote sha256 probe skips images that already match.
#
# The ssh identity comes from var.ssh_private_key (the same identity wait-ssh
# uses); pathexpand resolves the fixture's ~/.ssh/id_ed25519 because a tilde
# does not expand mid-argument after -i. Batch-mode options keep every remote
# command non-interactive.
# ---------------------------------------------------------------------------

locals {
  push_ssh_key        = var.ssh_private_key != "" ? pathexpand(var.ssh_private_key) : ""
  push_ssh_extra_opts = local.push_ssh_key != "" ? " -i ${local.push_ssh_key}" : ""
  push_ssh_opts       = "-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new${local.push_ssh_extra_opts}"
  push_scp_opts       = local.push_ssh_extra_opts

  # Per-node push pieces: ssh target and the node's .raw image paths
  # (role-split per confexts.tf node_confext_names).
  push_nodes = {
    for name in sort(keys(var.node_ips)) : name => {
      target = "${var.ssh_user}@${var.node_ips[name]}"
      images = [for image in local.node_confext_names[name] : "${local.confexts_dir}/${image}.raw"]
    }
  }
}

resource "null_resource" "push_confexts_control_plane" {
  for_each = {
    for name, ip in var.node_ips : name => ip if var.node_roles[name] == "control-plane"
  }

  triggers = {
    fingerprint = "${each.key}:${local.confext_fingerprint}"
    node_ip     = each.value
    node_role   = var.node_roles[each.key]
    node_images = join(",", local.node_confext_names[each.key])
  }

  depends_on = [null_resource.package_confexts]

  provisioner "local-exec" {
    command = "${path.module}/push-confext.sh ${local.push_nodes[each.key].target} ${join(" ", local.push_nodes[each.key].images)}"
    environment = {
      PUSH_SSH_OPTS = local.push_ssh_opts
      PUSH_SCP_OPTS = local.push_scp_opts
    }
  }
}

resource "null_resource" "push_confexts_worker" {
  for_each = {
    for name, ip in var.node_ips : name => ip if var.node_roles[name] == "worker"
  }

  triggers = {
    fingerprint = "${each.key}:${local.confext_fingerprint}"
    node_ip     = each.value
    node_role   = var.node_roles[each.key]
    node_images = join(",", local.node_confext_names[each.key])
  }

  depends_on = [null_resource.push_confexts_control_plane, null_resource.package_confexts]

  provisioner "local-exec" {
    command = "${path.module}/push-confext.sh ${local.push_nodes[each.key].target} ${join(" ", local.push_nodes[each.key].images)}"
    environment = {
      PUSH_SSH_OPTS = local.push_ssh_opts
      PUSH_SCP_OPTS = local.push_scp_opts
    }
  }
}
