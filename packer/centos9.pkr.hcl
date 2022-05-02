variable "cpu" {
  type    = string
  default = "2"
}

variable "ram" {
  type    = string
  default = "2048"
}

variable "disk_size" {
  type    = string
  default = "20000"
}

variable "headless" {
  type    = string
  default = "true"
}

variable "iso_checksum_type" {
  type    = string
  default = "sha256"
}

variable "iso_checksum" {
  type    = string
  default = "df0901a8482237587101581a74e62742fa95ef493435d077a23d4bbe9508d63a"
}

variable "iso_url" {
  type    = string
  default = "http://mirror.stream.centos.org/9-stream/BaseOS/x86_64/iso/CentOS-Stream-9-20220425.0-x86_64-boot.iso"
}

variable "ssh_password" {
  type    = string
  default = "testtest"
}

variable "ssh_username" {
  type    = string
  default = "root"
}

source "qemu" "centos9" {
  accelerator      = "kvm"
  boot_command     = ["<tab><bs><bs><bs><bs><bs>inst.text inst.ks=http://{{ .HTTPIP }}:{{ .HTTPPort }}/kvm.ks<enter><wait>"]
  boot_wait        = "20s"
  disk_cache       = "none"
  disk_compression = true
  disk_discard     = "unmap"
  disk_interface   = "virtio"
  disk_size        = "${var.disk_size}"
  format           = "qcow2"
  headless         = "${var.headless}"
  http_directory   = "http"
  iso_url          = "${var.iso_url}"
  iso_checksum     = "${var.iso_checksum_type}:${var.iso_checksum}"
  net_device       = "virtio-net"
  output_directory = "artifacts/"
  qemu_binary      = "/sbin/qemu-system-x86_64"
  qemuargs         = [["-m", "${var.ram}M"], ["-smp", "${var.cpu}"], ["-cpu", "host"]]
  shutdown_command = "sudo /usr/sbin/shutdown -h now"
  ssh_password     = "${var.ssh_password}"
  ssh_username     = "${var.ssh_username}"
  ssh_wait_timeout = "30m"
}

build {
  sources = ["source.qemu.centos9"]

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    inline          = ["yum -y install epel-release", "yum repolist"]
  }
}
