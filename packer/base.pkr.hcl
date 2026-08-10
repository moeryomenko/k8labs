# Base image definition for k8labs.
# Uses Fedora Cloud Base qcow2 as source with UEFI firmware (CLOUDHV.fd).
# Cloud-init FAT disk injects SSH key for Packer provisioning.
# Networking: TAP device on k8sbr0 bridge, DHCP reservation for SSH.
#
# Plugin: manually installed at ~/.packer.d/plugins/github.com/moeryomenko/cloud-hypervisor/

source "cloud-hypervisor" "base" {
  # CH binary path (falls back to PATH if empty)
  ch_binary_path = var.ch_binary_path

  # UEFI firmware boot
  firmware = var.firmware_path

  # Hardware
  vcpus  = var.vm_cpu_cores
  memory = var.vm_memory

  # Root disk: Fedora Cloud Base raw (writable)
  disk_images {
    path       = var.cloud_image_path
    readonly   = false
    image_type = "raw"
    id         = "rootfs"
  }

  # Cloud-init disk: FAT16 CIDATA (read-only, SSH key injection)
  disk_images {
    path       = var.cloudinit_disk_path
    readonly   = true
    image_type = "raw"
    id         = "cloud-init"
  }

  # Networking: TAP attached to k8sbr0 bridge.
  # No ip set — the bridge handles L3 routing.
  # Guest gets 192.168.124.10 via DHCP reservation (MAC->IP mapping in dnsmasq).
  network_interfaces {
    tap  = var.tap_device
    mac  = var.guest_mac
    ip   = var.guest_ip
    mask = var.guest_mask
  }

  # SSH communicator for provisioning
  communicator           = "ssh"
  ssh_host               = var.guest_ip
  ssh_username           = var.ssh_username
  ssh_private_key_file   = var.ssh_private_key_file
  ssh_agent_auth         = false
  ssh_clear_authorized_keys = false
  ssh_port               = 22
  ssh_timeout            = var.ssh_timeout

  serial  = "null"
  console = "null"
}

build {
  sources = ["source.cloud-hypervisor.base"]

  # Configure the system and clean up BEFORE the extension payloads are
  # uploaded. 01-configure-system.sh creates /var/lib/extensions and
  # /var/lib/confexts (the upload destinations below) and installs the
  # runtime prerequisites (conmon, parted, growpart).
  provisioner "shell" {
    environment_vars = [
      "KERNEL_VERSION=${var.kernel_version}",
    ]
    scripts = [
      "scripts/01-configure-system.sh",
      "scripts/02-cleanup.sh"
    ]
  }

  # Bake the 7 sysext images into /var/lib/extensions/.
  provisioner "file" {
    sources = [
      "${var.extensions_release_dir}/kubelet.raw",
      "${var.extensions_release_dir}/cri-o.raw",
      "${var.extensions_release_dir}/crun.raw",
      "${var.extensions_release_dir}/cni.raw",
      "${var.extensions_release_dir}/etcd.raw",
      "${var.extensions_release_dir}/kubernetes-cp.raw",
      "${var.extensions_release_dir}/perfetto.raw",
    ]
    destination = "/var/lib/extensions/"
  }

  # Bake the 3 static confext images into /var/lib/confexts/.
  provisioner "file" {
    sources = [
      "${var.extensions_release_dir}/confext-cri-o.raw",
      "${var.extensions_release_dir}/confext-kubernetes.raw",
      "${var.extensions_release_dir}/confext-containers.raw",
    ]
    destination = "/var/lib/confexts/"
  }

  # SELinux ebpf-fix policy module; loaded into the policy store by
  # 03-seal.sh, which runs after this upload.
  provisioner "file" {
    source      = "../extensions/selinux/ebpf-fix.pp"
    destination = "/root/ebpf-fix.pp"
  }

  # First-boot root filesystem resize helper.
  provisioner "file" {
    source      = "${var.packer_scripts_dir}/resize-rootfs.sh"
    destination = "/usr/local/sbin/resize-rootfs.sh"
  }

  # Seal: enable the sysext/confext services, load the baked SELinux module,
  # finalize the image. Runs after the uploads so it can consume them.
  provisioner "shell" {
    scripts = [
      "scripts/03-seal.sh"
    ]
  }

  post-processor "manifest" {
    output     = "${var.manifest_output}/manifest.json"
    strip_path = true
  }
}
