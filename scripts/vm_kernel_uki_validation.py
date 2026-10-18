"""Validate the custom kernel image and signed UKI in a disposable Cloud Hypervisor VM."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]

FIRMWARE_OUTPUT = "build/firmware/k8labs-cloudhv-secureboot.fd"
FIRMWARE_PATH = FIRMWARE_OUTPUT
FIRMWARE_IMAGE = FIRMWARE_OUTPUT
CUSTOM_FIRMWARE = FIRMWARE_OUTPUT

UKI_OUTPUT = "build/uki/EFI/BOOT/BOOTX64.EFI"
UKI_PATH = UKI_OUTPUT
UKI = UKI_OUTPUT
BOOTX64_PATH = UKI_OUTPUT

QCOW2_PATH = "build/k8labs-base.qcow2"
QCOW2 = QCOW2_PATH
BASE_IMAGE = QCOW2_PATH
CUSTOM_QCOW2 = QCOW2_PATH

STOCK_FIRMWARE = "build/CLOUDHV.fd"
STOCK_FIRMWARE_PATH = STOCK_FIRMWARE

KVM_DEVICE = "/dev/kvm"
CLOUD_HYPERVISOR_BIN = "cloud-hypervisor"

PINNED_KERNEL_NVR = "7.3.0-0.rc7.261014g4056b3c9d3c9.60.fc46"
PINNED_KERNEL_VERSION = PINNED_KERNEL_NVR
KERNEL_NVR = PINNED_KERNEL_NVR
EXPECTED_KERNEL_NVR = PINNED_KERNEL_NVR

PINNED_FIRMWARE_COMMIT = "d46aa46c8361194521391aa581593e556c707c6e"
FIRMWARE_SOURCE_COMMIT = PINNED_FIRMWARE_COMMIT

LAB_CERT_FINGERPRINT = "81b6f09f7be0746ca177e07fdc1503d44803dcf6e701b85c7777ba7f3dc017a4"
PUBLIC_KEY_FINGERPRINT = LAB_CERT_FINGERPRINT
UEFI_DB_FINGERPRINT = LAB_CERT_FINGERPRINT
EXPECTED_LAB_CERT_FINGERPRINT = LAB_CERT_FINGERPRINT

PUBLIC_CERT_FINGERPRINT = "288586d477df93564b346323379b857b491dd74aa9d98e19e5bc5107568be274"
PUBLIC_CERT_PATH = "firmware/k8labs-uefi-db.crt"
LAB_CERT_PATH = PUBLIC_CERT_PATH

REQUIRED_CONFIG_OPTIONS = (
    "CONFIG_SCHED_CLASS_EXT",
    "CONFIG_BPF_SYSCALL",
    "CONFIG_BPF_JIT",
    "CONFIG_DEBUG_INFO_BTF",
)
REQUIRED_CONFIGS = REQUIRED_CONFIG_OPTIONS

BTF_PATH = "/sys/kernel/btf/vmlinux"

CGROUP_CPU_MAX_PATH = "/sys/fs/cgroup/cpu.max"
CGROUP_CPU_STAT_PATH = "/sys/fs/cgroup/cpu.stat"
CGROUP_V2_CPU_MAX = CGROUP_CPU_MAX_PATH
CGROUP_V2_CPU_STAT = CGROUP_CPU_STAT_PATH

ARENA_SCALAR_FLAG = "BPF_F_ARENA_SCALAR"

SCHEDULERS = (
    "eevdf",
    "scx_bpfland",
    "scx_lavd",
    "scx_mavd",
    "scx_mlfq",
    "scx_nitosis",
)
SCHEDULER_NAMES = SCHEDULERS
SIX_SCHEDULERS = SCHEDULERS
SCHEDULER_GATES = ("build", "verifier", "attach", "progress", "detach")

SECURE_BOOT_REQUIRED = True


def _abs(path):
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment_ok():
    return Path(KVM_DEVICE).exists() and shutil.which(CLOUD_HYPERVISOR_BIN) is not None


def check_environment():
    errors = []
    if not Path(KVM_DEVICE).exists():
        errors.append("KVM device missing: " + KVM_DEVICE)
    if shutil.which(CLOUD_HYPERVISOR_BIN) is None:
        errors.append("Cloud Hypervisor not found on PATH: " + CLOUD_HYPERVISOR_BIN)
    return errors


def preflight():
    return check_environment()


def validate_environment():
    return check_environment()


def check_prerequisites():
    errors = list(check_environment())
    fw = _abs(FIRMWARE_OUTPUT)
    if not fw.is_file():
        errors.append(
            "custom firmware artifact missing: " + FIRMWARE_OUTPUT
            + " (build/firmware/k8labs-cloudhv-secureboot.fd not built; "
            "scripts/build_firmware.sh exits 2 because automated EDK2 build is "
            "not implemented in this environment)"
        )
    uki = _abs(UKI_OUTPUT)
    if not uki.is_file():
        errors.append("signed UKI artifact missing: " + UKI_OUTPUT)
    qcow2 = _abs(QCOW2_PATH)
    if not qcow2.is_file():
        errors.append("custom QCOW2 image missing: " + QCOW2_PATH)
    return errors


def check():
    return check_prerequisites()


def check_all():
    return check()


def is_ready():
    return not check()


def blocker():
    errors = check()
    return errors[0] if errors else None


def status():
    return "PASS" if not check() else "FAIL"


def boot_command(firmware=None, disk=None, console_log=None):
    firmware = firmware or _abs(FIRMWARE_OUTPUT)
    disk = disk or _abs(QCOW2_PATH)
    cmd = [
        CLOUD_HYPERVISOR_BIN,
        "--cpus", "boot=2",
        "--memory", "size=1024M",
        "--firmware", str(firmware),
        "--disk", "path=" + str(disk) + ",image_type=qcow2",
    ]
    if console_log is not None:
        cmd += ["--console", "file=" + str(console_log)]
    return cmd


def run_vm(timeout=120, firmware=None, disk=None, console_log=None):
    errors = check()
    if errors:
        return {"status": "FAIL", "blocker": errors[0], "errors": errors, "booted": False}
    if console_log is None:
        fd, console_log = tempfile.mkstemp(prefix="vm-validation-", suffix=".log")
        os.close(fd)
    console_log = Path(console_log)
    cmd = boot_command(firmware=firmware, disk=disk, console_log=console_log)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, check=False)
        return {
            "status": "PASS" if proc.returncode == 0 else "FAIL",
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "console_log": str(console_log),
            "booted": True,
            "errors": [],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "FAIL",
            "error": "VM boot timed out after " + str(timeout) + "s",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "console_log": str(console_log),
            "booted": True,
            "errors": ["timeout after " + str(timeout) + "s"],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "FAIL",
            "error": str(exc),
            "console_log": str(console_log),
            "booted": False,
            "errors": [str(exc)],
        }


def verify_secure_boot():
    return {"check": "secure_boot", "status": "NOT_RUN", "reason": "VM not booted"}


def verify_kernel_nvr():
    return {"check": "kernel_nvr", "status": "NOT_RUN", "reason": "VM not booted"}


def verify_config_options():
    return {"check": "config_options", "status": "NOT_RUN", "reason": "VM not booted"}


def verify_btf():
    return {"check": "btf", "status": "NOT_RUN", "reason": "VM not booted"}


def verify_cgroup():
    return {"check": "cgroup_v2", "status": "NOT_RUN", "reason": "VM not booted"}


def verify_schedulers():
    return {"check": "schedulers", "status": "NOT_RUN", "reason": "VM not booted"}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Validate custom kernel image and signed UKI in disposable VM.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    parser.add_argument("--run-vm", action="store_true", help="Attempt to boot the VM.")
    args = parser.parse_args(argv)

    errors = check()
    result = {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "environment_ok": environment_ok(),
        "artifacts": {
            "firmware": str(_abs(FIRMWARE_OUTPUT)),
            "firmware_present": _abs(FIRMWARE_OUTPUT).is_file(),
            "uki": str(_abs(UKI_OUTPUT)),
            "uki_present": _abs(UKI_OUTPUT).is_file(),
            "qcow2": str(_abs(QCOW2_PATH)),
            "qcow2_present": _abs(QCOW2_PATH).is_file(),
        },
        "pinned_kernel_nvr": PINNED_KERNEL_NVR,
        "expected_lab_cert_fingerprint": EXPECTED_LAB_CERT_FINGERPRINT,
        "schedulers": list(SCHEDULERS),
    }
    if args.run_vm:
        result["vm"] = run_vm()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if errors:
            for err in errors:
                print("FAIL: " + err, file=sys.stderr)
        else:
            print("validation prerequisites OK")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
