# k8labs comparison kernel package.
#
# The kernel is built out of tree from the local linux checkout and staged by
# scripts/build-custom-kernel.sh. This package only installs the already-built
# artifacts; it does not rebuild the kernel. It is deliberately named
# kernel-k8labs so it can coexist with Fedora's kernel-core/kernel-modules
# packages, and its release matches the kernel's LOCALVERSION (7.3.0-k8labs).
Name:           kernel-k8labs
Version:        7.3.0
Release:        k8labs
Summary:        k8labs comparison kernel built from the local linux tree
License:        GPL-2.0-only
URL:            https://github.com/moeryomenko/k8labs
BuildArch:      x86_64
AutoReqProv:    no
Requires:       dracut
Requires:       kmod

%description
Comparison kernel built from the k8labs local linux checkout with the
campaign configuration fragment (sched_ext, BTF, cgroup v2, EFI stub and
module signing). It installs beside the Fedora kernel so both can be selected
at boot.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}
cp -a %{staging}/. %{buildroot}/
rm -f %{buildroot}/lib/modules/%{kernelrelease}/build
rm -f %{buildroot}/lib/modules/%{kernelrelease}/source

%post
depmod -a %{kernelrelease} || true
if [ -x /usr/bin/kernel-install ]; then
    /usr/bin/kernel-install add %{kernelrelease} /boot/vmlinuz-%{kernelrelease} || true
fi

%postun
if [ -x /usr/bin/kernel-install ]; then
    /usr/bin/kernel-install remove %{kernelrelease} || true
fi

%files
/boot/vmlinuz-%{kernelrelease}
/boot/System.map-%{kernelrelease}
/boot/config-%{kernelrelease}
/lib/modules/%{kernelrelease}
