"""Contracts for the kernel image pipeline: stock + comparison RPMs, signed UKIs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKER = ROOT / "packer"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_packer_uploads_both_kernel_repositories() -> None:
    base = _read(PACKER / "base.pkr.hcl")
    assert "source      = var.kernel_rpm_source_dir" in base
    assert 'destination = "/var/lib/k8labs-kernel-repo-stock"' in base
    assert "source      = var.custom_kernel_rpm_source_dir" in base
    assert 'destination = "/var/lib/k8labs-kernel-repo-custom"' in base


def test_packer_installs_stock_and_custom_kernels() -> None:
    variables = _read(PACKER / "variables.pkr.hcl")
    assert 'default     = "7.3.0-65.fc46"' in variables
    assert 'default     = "7.3.0-k8labs"' in variables
    assert "file:///var/lib/k8labs-kernel-repo-stock" in variables
    assert "file:///var/lib/k8labs-kernel-repo-custom" in variables
    assert 'default     = "stock"' in variables


def test_provisioner_builds_repository_metadata_for_both_repos() -> None:
    script = _read(PACKER / "scripts" / "01-configure-system.sh")
    assert "createrepo_c" in script
    assert "prepare_kernel_repo" in script
    assert 'case "${repo_url}" in' in script


def test_provisioner_installs_and_verifies_both_kernels() -> None:
    script = _read(PACKER / "scripts" / "01-configure-system.sh")
    assert 'dnf_install_from_repo "${KERNEL_REPO_URL:-}"' in script
    assert 'dnf_install_from_repo "${CUSTOM_KERNEL_REPO_URL:-}" kernel-k8labs' in script
    assert "STOCK_KERNEL_RELEASE=" in script
    assert "CUSTOM_KERNEL_RELEASE=" in script
    assert "vmlinuz-${release}" in script
    assert "stock=${STOCK_KERNEL_RELEASE}" in script
    assert "custom=${CUSTOM_KERNEL_RELEASE}" in script


def test_provisioner_sets_the_stock_kernel_default() -> None:
    script = _read(PACKER / "scripts" / "01-configure-system.sh")
    assert 'stock) DEFAULT_KERNEL_RELEASE="${STOCK_KERNEL_RELEASE}"' in script
    assert 'grubby --set-default "/boot/vmlinuz-${DEFAULT_KERNEL_RELEASE}"' in script
    assert "default=${DEFAULT_KERNEL}" in script


def test_uki_step_fails_closed_without_a_signing_key() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert "K8LABS_SB_KEY" in script
    assert 'fail "K8LABS_SB_KEY is required' in script
    assert 'fail "signing certificate not found' in script
    assert 'fail "signing key not found' in script


def test_uki_step_installs_systemd_boot_fallback() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert 'BOOT_DIR="${ESP_MOUNT}/EFI/BOOT"' in script
    assert 'UKI_DIR="${ESP_MOUNT}/EFI/Linux"' in script
    assert 'SYSTEMD_BOOT_SRC="/usr/lib/systemd/boot/efi/systemd-bootx64.efi"' in script
    assert '--output "${BOOT_DIR}/BOOTX64.EFI"' in script
    assert "loader.conf" in script


def test_uki_step_builds_both_kernels() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert 'build_uki "${STOCK_KERNEL_RELEASE}" "k8labs-stock"' in script
    assert 'build_uki "${CUSTOM_KERNEL_RELEASE}" "k8labs-custom"' in script
    assert "DEFAULT_UKI" in script


def test_uki_step_verifies_every_signature_before_shipping() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    # build_uki verifies each of the two kernels, and the boot manager is
    # verified separately before the disk is captured.
    assert script.count("sbverify --cert") >= 2
    assert script.count("|| fail") >= 2


def test_uki_step_is_wired_into_the_build() -> None:
    base = _read(PACKER / "base.pkr.hcl")
    assert "scripts/04-uki.sh" in base
    assert 'destination = "/root/uki-material"' in base


def test_uki_tooling_is_installed_in_the_image() -> None:
    script = _read(PACKER / "scripts" / "01-configure-system.sh")
    assert "systemd-ukify" in script
    assert "sbsigntools" in script


def test_uki_step_mounts_the_efi_system_partition() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert 'if ! mountpoint -q "${ESP_MOUNT}"' in script
    assert 'mount "${ESP_DEV}" "${ESP_MOUNT}"' in script
    # Regression: picking the first vfat device selected the cloud-init seed
    # disk and the UKI vanished with the bake VM.
    assert "findmnt -n -o SOURCE /" in script
    assert "lsblk -no PKNAME" in script


def test_uki_step_flushes_before_the_disk_is_captured() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert script.count("sync") >= 1
    assert script.index("sbverify --cert") < script.rindex("sync")


def test_uki_uses_the_installed_release_not_the_rpm_nvr() -> None:
    # The installed kernel is named <NVR>.x86_64, so dracut and ukify must use
    # the release derived from the file, not the RPM NVR.
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert 'kernel_image="/boot/vmlinuz-${release}"' in script
    assert '--kver "${release}"' in script
    assert '--uname "${release}"' in script


def test_uki_reuses_the_installed_kernel_cmdline() -> None:
    # The image roots on a btrfs subvolume; a hardcoded root= device panics.
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert "BLS_ENTRY" in script
    assert 'loader/entries/*-"${release}".conf' in script
    assert "sed -n 's/^options" in script


def test_uki_supports_pkcs11_uris() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert "pkcs11:*" in script
    assert "--signing-engine pkcs11" in script
    assert "PKCS11_MODULE_PATH" in script
    assert "uki-material/tokens" in script
    variables = _read(PACKER / "variables.pkr.hcl")
    assert "uki_pkcs11_module" in variables
    assert "/usr/lib64/pkcs11/libsofthsm2.so" in variables


def test_uki_step_uploads_material_directory_not_a_bare_key() -> None:
    base = _read(PACKER / "base.pkr.hcl")
    assert "var.uki_material_dir" in base
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert "/root/uki-material/cert.pem" in script
