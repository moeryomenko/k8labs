#!/usr/bin/env python3
"""Validate the linux-sched OTel campaign's artefacts offline.

These check the properties that are cheap to get wrong and expensive to discover
during a campaign:

* the profiling DaemonSet is least-privilege — the plan forbids
  ``privileged: true``, and the component's documented capability set is enough;
* every image is pinned by digest, because a moved tag would change what produced
  the profiles;
* the configuration carries the whole label set the gate queries on, and the
  label names match the ones the session runner records;
* the gate exists and verifies the things the plan's acceptance names.

No cluster and no network: this is the check that a manifest edit did not quietly
drop the guarantees.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINUX_SCHED_DIR = HERE.parent
MK = LINUX_SCHED_DIR / "linux-sched.mk"

SESSION_LABELS = ("workload", "scheduler", "node", "kernel", "image", "run_id")
REQUIRED_CAPABILITIES = (
    "BPF",
    "PERFMON",
    "SYS_PTRACE",
    "CHECKPOINT_RESTORE",
    "SYS_RESOURCE",
    "DAC_READ_SEARCH",
    "SYSLOG",
)


class Problems(list):
    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.append(message)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _without_comments(text: str) -> str:
    """Drop whole-line comments, so prose about a forbidden setting is not one.

    The DaemonSet documents *why* it is not privileged, which contains the very
    phrase the check looks for.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def validate(problems: Problems, root: Path = HERE, mk_path: Path = MK) -> None:
    """Check the artefacts under `root`, wiring against `mk_path`."""

    daemonset = _read(root / "k8s" / "alloy-daemonset.yaml")
    template = _read(root / "k8s" / "alloy-config.alloy.tmpl")
    compose = _read(root / "host" / "compose.yaml")
    profiles = _read(root / "profiles.sh")
    gate = _read(root / "check.sh")
    mk = _read(mk_path)

    # The profiler must not run privileged, and prose saying so is not a breach.
    active = _without_comments(daemonset)
    problems.check(
        "privileged: true" not in active and "privileged:true" not in active,
        "the profiling DaemonSet must not use privileged: true",
    )
    for capability in REQUIRED_CAPABILITIES:
        problems.check(
            f"- {capability}" in daemonset,
            f"the profiling DaemonSet is missing capability {capability}",
        )

    # Least privilege needs the host PID namespace and the tracing filesystem.
    problems.check("hostPID: true" in daemonset, "the DaemonSet needs hostPID: true")
    problems.check(
        "mountPath: /sys/kernel/tracing" in daemonset,
        "the DaemonSet must mount /sys/kernel/tracing",
    )

    # Only the research workers, and only pinned images.
    # The DaemonSet must not carry the placeholder ConfigMap. Applying both
    # objects together overwrote every rendered session configuration with the
    # placeholder, which declares no targets, so every session profiled nothing
    # while Alloy still reported itself healthy.
    problems.check(
        "kind: ConfigMap" not in daemonset,
        "alloy-daemonset.yaml must not contain a ConfigMap: applying it with the "
        "rendered session configuration reverts the labels to the placeholder",
    )
    problems.check(
        (root / "k8s" / "alloy-configmap.yaml").is_file(),
        "the placeholder Alloy configuration must live in its own file",
    )
    placeholder = root / "k8s" / "alloy-configmap.yaml"
    if placeholder.is_file():
        problems.check(
            "kind: ConfigMap" in _read(placeholder),
            "alloy-configmap.yaml must define the placeholder ConfigMap",
        )
    problems.check(
        "kubernetes.io/hostname: __TARGET_NODE__" in daemonset,
        "the DaemonSet must be pinned to a rendered target node, not to every "
        "research worker: other nodes would profile under the session's labels",
    )
    problems.check(
        (root / "host" / "provision-volume.sh").exists(),
        "the 10 GiB volume provisioning script must exist",
    )
    problems.check(
        "PROFILE_SIZE_BYTES" in _read(root / "host" / "provision-volume.sh"),
        "the volume script must verify the 10 GiB size, not just create a directory",
    )
    for name, text in (("DaemonSet", daemonset), ("compose", compose)):
        problems.check(
            "@sha256:" in text,
            f"{name}: images must be pinned by digest",
        )
    problems.check(
        "grafana/alloy:v1.20.1@sha256:5133c159d34c7acf37a1b6d91682abcb6b28b2a29bae152ff5cf4753296f956c"
        in daemonset,
        "the DaemonSet must pin Alloy to the digest the campaign validated",
    )
    problems.check(
        "grafana/pyroscope:2.4.0@sha256:999dba0da6a9300af264ddba12ac3b78e637ea5bb419852866ba7f2dabe74eaa"
        in compose,
        "the compose file must pin Pyroscope to the digest the campaign validated",
    )

    # The store keeps the plan's retention, and its data is a host-owned mount.
    problems.check(
        "-retention-period=336h" in compose,
        "Pyroscope must run with the plan's 336h retention",
    )
    problems.check(
        "${PROFILE_VOLUME" in compose,
        "Pyroscope's data must be a host-owned bind mount, not a node filesystem",
    )

    # Every session label is in the configuration, and the runner records them.
    for label in SESSION_LABELS:
        problems.check(
            f'target_label  = "{label}"' in template,
            f"the Alloy template is missing the {label} session label",
        )
        problems.check(
            f'"{label}"' in profiles,
            f"the session runner does not record the {label} label",
        )
    problems.check(
        'replacement   = "linux-sched-profile"' in template,
        "the Alloy template must pin service_name to linux-sched-profile",
    )
    problems.check(
        "__PYROSCOPE_URL__" in template,
        "the Alloy template must take the Pyroscope URL from the runner",
    )

    # The pipeline must not be the OTLP one Alloy cannot carry.
    problems.check(
        "otelcol.receiver.otlp" not in template,
        "the campaign must not route profiles through Alloy's OTLP receiver: it "
        "rejects the profiles signal (verified on v1.20.1 and v1.21.0-rc.0)",
    )

    # The gate verifies what the plan's acceptance names.
    problems.check("-/healthy" in gate, "the gate must check Alloy health")
    problems.check(
        "All Alloy components are healthy." in gate,
        "the gate must require the documented health string",
    )
    problems.check(
        "pyroscope_ebpf_pprofs_total" in gate,
        "the gate must require collected profiles to have advanced",
    )
    problems.check(
        "pyroscope_ebpf_profiling_sessions_failing_total" in gate,
        "the gate must require no failing profiling sessions",
    )
    problems.check("df -B1" in gate, "the gate must verify the volume with df -B1")
    problems.check(
        "-retention-period=" in gate, "the gate must verify Pyroscope's retention"
    )
    problems.check(
        "STOP_BYTES" in gate, "the gate must enforce the 9 GiB stop threshold"
    )
    problems.check("profilecli" in gate, "the gate must query profiles with profilecli")

    # The plan's acceptance names the command the gate is run through.
    problems.check(
        "linux-sched-otel-check:" in mk,
        "the Makefile must expose linux-sched-otel-check",
    )
    problems.check(
        "check.sh" in mk,
        "linux-sched-otel-check must run the gate script",
    )


def main() -> int:
    problems = Problems()
    validate(problems)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("OTel campaign artefacts OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
