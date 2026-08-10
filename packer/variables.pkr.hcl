# Base OS: Fedora 44 (kernel 7.1)

variable "kernel_version" {
  description = "Kernel version to pin during image baking"
  type        = string
  default     = "7.1"
}

# ---------------------------------------------------------------------------
# Cloud-Hypervisor builder settings
# ---------------------------------------------------------------------------

variable "ch_binary_path" {
  description = "Path to cloud-hypervisor binary (empty = use PATH)"
  type        = string
  default     = ""
}

variable "firmware_path" {
  description = "Path to CLOUDHV.fd UEFI firmware binary"
  type        = string
}

variable "tap_device" {
  description = "TAP device name for Packer SSH connectivity"
  type        = string
  default     = "packer-tap"
}

variable "guest_ip" {
  description = "Static IP assigned to the guest VM (used for SSH provisioning)"
  type        = string
  default     = "192.168.124.10"
}

variable "guest_mac" {
  description = "MAC address for the guest"
  type        = string
  default     = "de:ad:be:ef:00:01"
}

variable "guest_mask" {
  description = "Netmask for the guest (e.g., 255.255.255.0)"
  type        = string
  default     = "255.255.255.0"
}

# ---------------------------------------------------------------------------
# Fedora Cloud Base image
# ---------------------------------------------------------------------------

variable "cloud_image_url" {
  description = "Download URL for Fedora Cloud Base qcow2"
  type        = string
}

variable "cloud_image_checksum" {
  description = "SHA256 checksum for the Fedora Cloud Base image"
  type        = string
}

variable "cloud_image_path" {
  description = "Local path to the Fedora Cloud Base qcow2 (downloaded)"
  type        = string
}

# ---------------------------------------------------------------------------
# Cloud-init disk (Packer SSH key injection)
# ---------------------------------------------------------------------------

variable "cloudinit_disk_path" {
  description = "Path to the pre-generated cloud-init CIDATA FAT disk"
  type        = string
}

variable "ssh_username" {
  description = "SSH user for Packer provisioning (root for builds; key injected via cloud-init)"
  type        = string
  default     = "root"
}

variable "ssh_private_key_file" {
  description = "Path to the SSH private key for Packer provisioning"
  type        = string
}

variable "ssh_timeout" {
  description = "Timeout for SSH connection during provisioning"
  type        = string
  default     = "15m"
}

# ---------------------------------------------------------------------------
# VM hardware
# ---------------------------------------------------------------------------

variable "vm_cpu_cores" {
  description = "Number of CPU cores for the build VM"
  type        = number
  default     = 2
}

variable "vm_memory" {
  description = "Memory in MiB for the build VM"
  type        = number
  default     = 2048
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

variable "output_directory" {
  description = "Output directory for the baked image"
  type        = string
  default     = "../build/base-image"
}

variable "output_image_name" {
  description = "Output image filename"
  type        = string
  default     = "k8labs-base.qcow2"
}

variable "manifest_output" {
  description = "Output path for the manifest file (created by post-processor)"
  type        = string
  default     = "../build"
}

# ---------------------------------------------------------------------------
# Extension baking inputs
# ---------------------------------------------------------------------------

variable "extensions_release_dir" {
  description = "Directory containing the built sysext/confext .raw images (relative to the packer template)"
  type        = string
  default     = "../extensions/release"
}

variable "packer_scripts_dir" {
  description = "Directory containing the Packer provisioning scripts (relative to the packer template)"
  type        = string
  default     = "scripts"
}
