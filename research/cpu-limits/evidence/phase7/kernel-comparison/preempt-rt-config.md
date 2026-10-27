# PREEMPT_RT and sched_ext: configuration and build check

Claim under test: "PREEMPT_RT cannot be combined with sched_ext."

Source: `next-20261016`, commit `2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64`.

## What the kernel sources say

`kernel/Kconfig.preempt` puts no dependency between the two options:

```
config SCHED_CLASS_EXT
	bool "Extensible Scheduling Class"
	depends on BPF_SYSCALL && BPF_JIT && DEBUG_INFO_BTF
	select GENERIC_ALLOCATOR
	select STACKTRACE if STACKTRACE_SUPPORT

config PREEMPT_RT
	bool "Fully Preemptible Kernel (Real-Time)"
	depends on EXPERT && ARCH_SUPPORTS_RT && !COMPILE_TEST
	select PREEMPTION
```

`Documentation/scheduler/sched-ext.rst` does not mention PREEMPT_RT at all, and
`kernel/sched/ext/` contains no `CONFIG_PREEMPT_RT` guard.

## Configuration

Starting from the campaign configuration (sched_ext, BTF, cgroup v2), set
`CONFIG_PREEMPT_RT=y` and `CONFIG_EXPERT=y`, then run `olddefconfig` **in the
build container** (which has pahole):

```
CONFIG_PREEMPT_RT=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_PREEMPT_DYNAMIC=y
CONFIG_PREEMPT_LAZY=y
CONFIG_FAIR_GROUP_SCHED=y
CONFIG_DEBUG_FS=y
```

Running the same `olddefconfig` on the lab host instead **drops**
`SCHED_CLASS_EXT`, because the host has no `pahole`, so `DEBUG_INFO_BTF`
becomes unmet (`depends on PAHOLE_VERSION >= 122`) and sched_ext follows. That
toolchain accident is the only way this combination "fails to configure", and
it is not a kernel restriction.

## Build

```
make -C build/linux-custom-src O=build/kernel-custom-next -j JOBS bzImage modules
```

Result: `arch/x86/boot/bzImage` 16790528 bytes, 2445 modules with BTF, sched_ext
objects linked in, no errors. The configuration above is what the build used;
`CONFIG_LOCALVERSION` was set to `-rt-k8labs` to keep the artifact
distinguishable.

One interactive prompt appeared during the build (the ML-DSA module-signing key
choice). It read EOF and continued with the default; it does not affect the
sched_ext or preemption options.

## What is not established

The kernel was **not booted**. Node images boot a signed UKI produced by the
bake pipeline (`packer/`, `systemd-sbsign`, PKCS#11), and this environment has
no signing token, no host image staging directory and no CAPI machine objects.
A build-verified combination is therefore not a runtime-verified one: sched_ext
on PREEMPT_RT remains unsupported until someone boots it and runs the attach
and load tests.
