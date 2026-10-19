# k8labs kernel campaign — operator runbook

Everything that does not need the lab PKCS#11/HSM key is built and verified.
This runbook covers the remaining, key-gated steps in order. Run it on the lab
host; every command assumes the repository root is the working directory unless
stated otherwise.

## Artifact inventory

| Artifact | Where | Identity |
|---|---|---|
| linux-next source | `kernel-campaign/src/linux` | `2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64`, tag `next-20261016` |
| kernel-ark packaging | `kernel-campaign/src/kernel-ark` | `858721d1e05149394e01ae4f5ce3202484c22850` |
| kernel SRPM | `kernel-campaign/src/linux/redhat/rpm/SRPMS/` | `kernel-7.3.0-0.0.next.20261016.64.k8.680e037.fc44.src.rpm` |
| runtime RPMs | `artifacts/kernel/7.3.0-0.0.next.20261016.64.k8.680e037.fc44/` (Git LFS) | `kernel-core`, `kernel-modules`, `kernel-modules-core`, `kernel-modules-extra` |
| EDK2 source | `kernel-campaign/src/edk2` | `d46aa46c8361194521391aa581593e556c707c6e`, tag `edk2-stable202508` |
| firmware | `kernel-campaign/out/firmware/CLOUDHV-k8labs-sb.fd` | `sha256 5a442b9f2aa5eee409d76ae8de3c040d52050ac6c4c5e4121b4ed1897088b9a2` |
| trust anchor | `firmware/k8labs-uefi-db.crt` | SPKI `sha256 81b6f09f…`, DER `sha256 288586d4…` |
| baked image | `packer/output-base/fedora-cloud-base.raw` | local verification only; re-bake for release |

The firmware image above trusts the lab certificate; it was verified to report
`SecureBoot=1` and to reject an unsigned image with `Access Denied`.

## Step 0 — environment checks

```sh
podman --version                 # >= 6
test -S /run/pcscd/pcscd.comm || systemctl start pcscd
pkcs11-tool --list-slots         # the lab token must be visible
test -e /dev/kvm
podman info --format '{{.Host.Security.Rootless}}'
df -h /home                       # kernel builds need ~40 GiB
```

## Step 1 — verify the lab key matches the enrolled certificate

The firmware trusts the certificate in `firmware/k8labs-uefi-db.crt`. The HSM
key must be its private half, or every boot will fail with `Access Denied`.

```sh
# Export the public key from the token and compare fingerprints.
pkcs11-tool --read-object --type pubkey --id <KEY-ID> --output /tmp/uki-pub.der
openssl pkey -pubin -inform DER -in /tmp/uki-pub.der -outform DER \
  | openssl dgst -sha256
openssl x509 -in firmware/k8labs-uefi-db.crt -pubkey -noout \
  | openssl pkey -pubin -outform DER | openssl dgst -sha256
```

The two digests must be equal and must match `81b6f09f…` from the manifest.
Stop if they differ.

## Step 1b — reproducible kernel build

rpm 6 does not honour `SOURCE_DATE_EPOCH` for `%{BUILDTIME}`, and the kABI
stablelists tarball is created with plain `tar`. Pin both before building or the
SRPM hash drifts between runs:

```sh
printf '%%_buildtime 1792368000\n' >> ~/.rpmmacros
export SOURCE_DATE_EPOCH=1792368000
export KBUILD_BUILD_TIMESTAMP="Mon Oct 19 00:00:00 UTC 2026"
export KBUILD_BUILD_USER=k8labs
export KBUILD_BUILD_HOST=k8labs-build
export TAR_OPTIONS="--mtime=@1792368000 --owner=0 --group=0 --numeric-owner --sort=name"
```

For the mock build, the same values belong in the mock configuration:

```python
config_opts['macros']['_buildtime'] = '1792368000'
config_opts['environment']['SOURCE_DATE_EPOCH'] = '1792368000'
config_opts['environment']['KBUILD_BUILD_TIMESTAMP'] = 'Mon Oct 19 00:00:00 UTC 2026'
config_opts['environment']['KBUILD_BUILD_USER'] = 'k8labs'
config_opts['environment']['KBUILD_BUILD_HOST'] = 'k8labs-build'
config_opts['environment']['TAR_OPTIONS'] = (
    '--mtime=@1792368000 --owner=0 --group=0 --numeric-owner --sort=name'
)
```

Module signing still uses a per-build generated key. For bit-identical
`kernel-modules*` packages, place a fixed lab module-signing key at
`certs/signing_key.pem` in the build tree before the build; the campaign runs
with `CONFIG_MODULE_SIG_FORCE` unset, so any key is acceptable.

With those two settings, two independent `make dist-srpm` runs produce the same
SRPM: `sha256 0fda80a724f22148a313e42970f2cacbd2f734dce1ee7a2f520c2df2916ede67`.

## Step 2 — sign and bake the image

The bake runs in the rootless bake container and fails closed if signing fails.

```sh
export K8LABS_SB_KEY='pkcs11:token=k8labs;object=uki-signing;type=private'
export K8LABS_SB_CERT="$PWD/firmware/k8labs-uefi-db.crt"

make base-deps
make base          # or: make bake-run with a var file that sets:
                   #   firmware_path        = "../build/CLOUDHV-k8labs-sb.fd"
                   #   kernel_version       = "7.3.0-0.0.next.20261016.64.k8.680e037.fc44"
                   #   kernel_repo_url      = "file:///var/lib/k8labs-kernel-repo"
                   #   kernel_rpm_source_dir= "../build/kernel-rpms"
                   #   uki_signing_key      = "<PKCS#11 URI or key path>"
                   #   uki_signing_cert     = "<path to k8labs-uefi-db.crt>"
                   #   uki_signing_tool     = "systemd-sbsign"
```

The bake fails unless `sbverify` accepts the UKI against the lab certificate.
Do not bypass that check.

Copy the baked image to the staging name the host agent expects:

```sh
mkdir -p build
qemu-img convert -O qcow2 packer/output-base/fedora-cloud-base.raw \
  build/k8labs-base.qcow2
```

## Step 3 — disposable-VM validation

```sh
python3 scripts/vm_validation.py            # static checks
python3 scripts/vm_validation.py --run-vm   # boot the image in a disposable VM
python3 scripts/vm_validation.py --json     # machine-readable result
```

The gate must report the pinned NVR, `CONFIG_SCHED_CLASS_EXT=y`,
`CONFIG_DEBUG_INFO_BTF=y`, `/sys/kernel/btf/vmlinux` present, all six schedulers
resolvable, and a clean attach/detach cycle. Do not proceed on a partial pass.

## Step 4 — canary the measurement worker

Only the two research workers may change. The control plane `k8labs-zxwv4-0`
must never be touched.

```sh
kubectl --kubeconfig build/kubeconfig get nodes -o wide
# 1. Stage the new image and the rollback image on the host.
#    host_agent.py advertises /host-state/images/k8labs-custom-kernel.qcow2
#    and keeps the previous image for rollback.
# 2. Cordon the measurement worker and drain the scheduler pods only.
kubectl --kubeconfig build/kubeconfig cordon <worker-1>
# 3. Let the CAPI image map replace that worker; watch it join.
kubectl --kubeconfig build/kubeconfig get nodes -w
# 4. Verify the new kernel and Secure Boot state on the node.
kubectl --kubeconfig build/kubeconfig debug node/<worker-1> -- \
  chroot /host uname -r
kubectl --kubeconfig build/kubeconfig debug node/<worker-1> -- \
  chroot /host mokutil --sb-state
```

Proceed only when `uname -r` reports
`7.3.0-0.0.next.20261016.64.k8.680e037.fc44.x86_64` and Secure Boot is enabled.

Run the scheduler attach/progress/detach checks on the canary before touching
the second worker. If any check fails, restore the previous image and uncordon.

## Step 5 — second worker

Repeat step 4 for the other research worker, keeping one worker online at all
times. Never cordon both.

## Step 6 — campaign

```sh
cd research/linux-sched
# EEVDF pilot: freeze one per-workload quota that reaches >=10% throttled
# periods with a healthy workload, then run the staged confirmatory matrix.
```

All five workloads, including mixed, are confirmatory. Report global Holm-
corrected results at the 90% power target; throttling margins are ±10%, and
scheduler × quota interactions use ±5%.

## Rollback

1. Restore the previous QCOW2 (`/host-state/images/k8labs-base.qcow2`) in the
   CAPI image map and let the worker replace itself.
2. If the kernel is suspect, revert the image map to the 7.3.0 rawhide image and
   the stock `CLOUDHV.fd`; the old firmware and image must be retained until the
   campaign closes.
3. Never roll back by editing the control-plane machine.

## Evidence

For every step write `.agent/tasks/<TASK_ID>/evidence.md` and `evidence.json`
with PASS/FAIL per acceptance criterion and the raw command output. A step is
not complete until every criterion is PASS.
