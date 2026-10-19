# Module-signing key

The kernel build signs modules with a fixed key pair so two builds of the same
source and configuration produce identical module packages and an identical
embedded certificate. Without it, the kernel generates a new key per build and
every module package differs.

The pair is **not** committed. The lab rule is absolute: no private key material
in Git, LFS, firmware files, logs or manifests. Keep it outside the repository,
at `kernel-campaign/keys/k8labs-module-signing.{pem,x509}`, and record its
fingerprint in `kernel_build_lock.yaml`.

This key is not the Secure Boot key. The release UKI is signed by the lab
PKCS#11/HSM key, and `CONFIG_MODULE_SIG_FORCE` is deliberately unset, so any
correctly signed module loads.

## Create it once

```sh
openssl req -new -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout k8labs-module-signing.pem \
  -out k8labs-module-signing.x509 \
  -subj "/CN=k8labs module signing/"

# The kernel's extract-cert reads a PEM that carries key and certificate.
cat k8labs-module-signing.pem k8labs-module-signing.x509 > combined.pem
mv combined.pem k8labs-module-signing.pem
chmod 600 k8labs-module-signing.pem
```

## Use it in a build

```sh
cp kernel-campaign/keys/k8labs-module-signing.pem \
   kernel-campaign/src/linux/certs/k8labs-module-signing.pem
cp kernel-campaign/keys/k8labs-module-signing.x509 \
   kernel-campaign/src/linux/certs/k8labs-module-signing.x509
```

`CONFIG_MODULE_SIG_KEY` in the config fragment points at the in-tree PEM. Because
the key is part of the source package, rotating it changes the SRPM hash; it does
not change the config hash or the NVR.
