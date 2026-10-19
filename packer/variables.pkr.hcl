# Base OS: Fedora 44 with a kernel from a pinned repository. Fedora 44's own
# kernel (7.2.x) carries an older sched_ext ABI than the sched_ext tree the scx
# releases track, so the kernel comes from rawhide and is pinned by full NVR.

variable "kernel_version" {
  description = "Kernel version (full NVR) to pin during image baking"
  type        = string
  default     = "7.3.0-0.0.next.20261016.64.k8.680e037.fc44"
}

variable "kernel_repo_url" {
  description = "rpm-md repository to install the kernel from (empty = base OS repos). The campaign uses the locked local repository uploaded by base.pkr.hcl."
  type        = string
  default     = "file:///var/lib/k8labs-kernel-repo"
}

variable "kernel_rpm_source_dir" {
  description = "Host directory holding the four locked kernel runtime RPMs"
  type        = string
  default     = "../artifacts/kernel/7.3.0-0.0.next.20261016.64.k8.680e037.fc44"
}

# ---------------------------------------------------------------------------
# Scheduler-research fixtures
# ---------------------------------------------------------------------------
# Baked onto the node, not shipped in an image: the lab has no PVC and no SSH to
# the VMs, and the workload Pods and the observer DaemonSet mount these paths
# read-only. The load generators execute inside the observer container, so the
# observer also mounts the tools directory; node and container are both
# Fedora 44, so a node-built binary runs in the container.

variable "fixture_dir" {
  description = "On-node directory holding the baked workload fixtures"
  type        = string
  default     = "/var/lib/linux-sched/fixtures"
}

variable "fixture_bin_dir" {
  description = "On-node directory holding the baked research tools (wrk2)"
  type        = string
  default     = "/var/lib/linux-sched/bin"
}

variable "fixture_clip_url" {
  description = "URL of the licensed FFmpeg input clip archive (Big Buck Bunny, CC-BY 3.0)"
  type        = string
  default     = "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4.zip"
}

variable "fixture_clip_zip_sha256" {
  description = "SHA256 of the downloaded clip archive"
  type        = string
  default     = "109e3ede8790bd633f374ca311d9cc61dce8d7f98f5b0797ca98199c9fbceedf"
}

variable "fixture_clip_mp4_sha256" {
  description = "SHA256 of the extracted clip, which is what the workload reads"
  type        = string
  default     = "f78f39603e6774907f2faafabf26a667f4a6fc31769ec304a8a8f7c62d280508"
}

variable "wrk2_repo_url" {
  description = "Git repository for wrk2 (fixed-rate HTTP load generator)"
  type        = string
  default     = "https://github.com/giltene/wrk2"
}

variable "wrk2_commit" {
  description = "wrk2 commit to build; wrk2 is not packaged in Fedora"
  type        = string
  default     = "44a94c17d8e6a0bac8559b53da76848e430cb7a7"
}

variable "pgbench_scale" {
  description = "pgbench scale factor for the baked PostgreSQL snapshot"
  type        = number
  default     = 10
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

# ---------------------------------------------------------------------------
# Signed Unified Kernel Image
# ---------------------------------------------------------------------------
# The firmware trusts the lab certificate in its UEFI db and refuses unsigned
# images, so the bake must sign the UKI. The key is a PKCS#11 URI (the lab HSM)
# or a path; no private key material belongs in this repository.

variable "uki_cmdline" {
  description = "Kernel command line embedded in the UKI"
  type        = string
  default     = "root=/dev/vda2 rw console=ttyS0"
}

variable "uki_signing_key" {
  description = "PKCS#11 URI or path of the lab Secure Boot signing key"
  type        = string
  default     = ""
}

variable "uki_signing_cert" {
  description = "Path to the public certificate enrolled in the firmware db"
  type        = string
  default     = "../firmware/k8labs-uefi-db.crt"
}

variable "uki_signing_tool" {
  description = "ukify signing tool: sbsign, systemd-sbsign or pesign"
  type        = string
  default     = "systemd-sbsign"
}
