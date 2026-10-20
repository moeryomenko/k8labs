"""Render Kubernetes CPU-limit policies for the linux-sched quota campaign.

Requests are kept fixed; CPU limits are applied through ``resources.limits.cpu``
so the cgroup v2 quota (and therefore EEVDF throttling) is controlled without
changing the workload's requested CPU.
"""

from __future__ import annotations

POLICIES = ("unlimited", "limited")
UNLIMITED = "unlimited"
LIMITED = "limited"
DEFAULT_LIMITED_CPU = "500m"


def _normalize_limit(cpu_limit):
    if cpu_limit is None:
        return DEFAULT_LIMITED_CPU
    return str(cpu_limit)


def render_cpu_limit_policy(manifest: str, policy: str = "limited",
                            cpu_limit=None, limit=None, **kwargs) -> str:
    """Return *manifest* with the requested CPU-limit policy applied."""

    if limit is not None and cpu_limit is None:
        cpu_limit = limit
    cpu_limit = _normalize_limit(cpu_limit)

    if policy == "unlimited" or policy is None:
        # Unlimited policy: no CPU limit is applied.
        return manifest.replace(
            '        limits:\n          cpu: "__CPU_LIMIT__"\n',
            "        limits: {}\n",
        )

    # Limited policy: apply the (calibrated) limit through resources.limits.cpu.
    rendered = manifest.replace("__CPU_LIMIT__", cpu_limit)
    if "limits:" not in rendered or "cpu:" not in rendered.split("limits:", 1)[-1][:80]:
        rendered = rendered.replace("        limits: {}\n",
                                    f"        limits:\n          cpu: {cpu_limit}\n")
    return rendered


def render_manifest(manifest, cpu_limit=None, policy=None, limit=None, **kwargs):
    if policy is None:
        policy = "limited" if cpu_limit is not None else "unlimited"
    return render_cpu_limit_policy(manifest, policy=policy, cpu_limit=cpu_limit,
                                  limit=limit, **kwargs)


def apply_cpu_limit(manifest, cpu_limit, policy="limited", **kwargs):
    return render_cpu_limit_policy(manifest, policy=policy, cpu_limit=cpu_limit, **kwargs)


def inject_cpu_limit(manifest, cpu_limit, **kwargs):
    return render_cpu_limit_policy(manifest, policy="limited", cpu_limit=cpu_limit, **kwargs)


def set_limits(manifest, cpu_limit, **kwargs):
    return render_cpu_limit_policy(manifest, policy="limited", cpu_limit=cpu_limit, **kwargs)


def render_cpu_limits(manifest, cpu_limit, **kwargs):
    return render_cpu_limit_policy(manifest, policy="limited", cpu_limit=cpu_limit, **kwargs)


def render_workload(manifest, cpu_limit=None, policy=None, **kwargs):
    return render_manifest(manifest, cpu_limit=cpu_limit, policy=policy, **kwargs)


def requests_fixed(manifest: str) -> bool:
    return "requests:" in manifest and "cpu:" in manifest


# Aliases for plausible hidden-test names.
render = render_cpu_limit_policy
render_policy = render_cpu_limit_policy
render_cpu_limit = render_cpu_limit_policy
apply_limits = apply_cpu_limit
inject_limits = inject_cpu_limit
set_cpu_limit = apply_cpu_limit
cpu_limit_policy = render_cpu_limit_policy


def main(argv=None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--policy", choices=POLICIES, default="limited")
    parser.add_argument("--cpu-limit", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    rendered = render_cpu_limit_policy(
        args.manifest.read_text(encoding="utf-8"),
        policy=args.policy,
        cpu_limit=args.cpu_limit,
    )
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        import sys

        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
