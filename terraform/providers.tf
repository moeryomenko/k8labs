provider "cloudhypervisor" {
  # Managed mode: provider starts cloud-hypervisor as subprocess per VM.
  # No configuration needed — defaults to manage_ch_process = true.
}

provider "local" {
  # Used for generating cloud-init disks and tracking local state.
}
