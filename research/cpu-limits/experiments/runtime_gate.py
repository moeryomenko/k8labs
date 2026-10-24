"""Fail-closed runtime checks for the CPU limits experiment."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

CGROUP_CONTROLLERS = Path("/sys/fs/cgroup/cgroup.controllers")
KERNEL_RELEASE = Path("/proc/sys/kernel/osrelease")
CGROUP_MODE = Path("/sys/kernel/debug/sched/cgroup_mode")
BASE_SLICE_NS = Path("/sys/kernel/debug/sched/base_slice_ns")
CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
CPU_WEIGHT = Path("/sys/fs/cgroup/cpu.weight")
FIXTURE = Path("/fixtures/input.mp4")
REQUIRED_COMMANDS = ("bpftrace", "perf", "wrk2")
WRITABLE_DEBUG_PATHS = (CGROUP_MODE, BASE_SLICE_NS)
BASE_CHECKS = (
    "cgroup_v2",
    "cpu_controller",
    "kernel_release",
    "cgroup_mode_readable",
    "base_slice_ns_readable",
    "command_bpftrace",
    "command_perf",
    "command_wrk2",
    "fixture_input_mp4",
    "cpu_max_readable",
    "cpu_weight_readable",
)


@dataclass(frozen=True)
class Check:
    """One required runtime capability check."""

    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-safe check record."""

        return asdict(self)


@dataclass(frozen=True)
class GateResult:
    """The complete result of one runtime gate run."""

    status: str
    focused: bool
    root: str
    checks: tuple[Check, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe gate record."""

        return {
            "status": self.status,
            "focused": self.focused,
            "root": self.root,
            "checks": [check.as_dict() for check in self.checks],
        }


def _rooted(root: Path, path: Path) -> Path:
    return root / path.relative_to("/")


def _check_readable(root: Path, name: str, path: Path) -> Check:
    target = _rooted(root, path)
    try:
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode) or not mode & 0o444:
            raise OSError("not a readable regular file")
        target.read_text(encoding="utf-8")
    except OSError as error:
        return Check(name, "FAIL", f"{path}: {error}")
    return Check(name, "PASS", str(path))


def _check_writable(root: Path, name: str, path: Path) -> Check:
    target = _rooted(root, path)
    try:
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode) or not mode & 0o222:
            raise OSError("not a writable regular file")
    except OSError as error:
        return Check(name, "FAIL", f"{path}: {error}")
    return Check(name, "PASS", str(path))


def _check_cgroup_v2(root: Path) -> Check:
    return _check_readable(root, "cgroup_v2", CGROUP_CONTROLLERS)


def _check_cpu_controller(root: Path) -> Check:
    try:
        controllers = _rooted(root, CGROUP_CONTROLLERS).read_text(encoding="utf-8")
    except OSError as error:
        return Check("cpu_controller", "FAIL", f"{CGROUP_CONTROLLERS}: {error}")
    if "cpu" not in controllers.split():
        return Check("cpu_controller", "FAIL", "cpu controller is not enabled")
    return Check("cpu_controller", "PASS", "cpu controller is enabled")


def _check_kernel_release(root: Path) -> Check:
    check = _check_readable(root, "kernel_release", KERNEL_RELEASE)
    if check.status == "FAIL":
        return check
    release = _rooted(root, KERNEL_RELEASE).read_text(encoding="utf-8").strip()
    if not release:
        return Check("kernel_release", "FAIL", "kernel release is empty")
    return Check("kernel_release", "PASS", release)


def _check_command(root: Path, command: str) -> Check:
    for directory in (Path("/usr/bin"), Path("/usr/local/bin"), Path("/bin")):
        path = directory / command
        target = _rooted(root, path)
        try:
            mode = target.stat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and mode & 0o111:
            return Check(f"command_{command}", "PASS", str(path))
    return Check(
        f"command_{command}",
        "FAIL",
        f"{command} is missing or is not executable",
    )


def _run_check(check: Callable[[Path], Check], root: Path) -> Check:
    try:
        result = check(root)
    except (
        ArithmeticError,
        AttributeError,
        LookupError,
        OSError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:  # pragma: no cover - defensive fail-closed guard
        return Check("internal_gate_error", "FAIL", str(error))
    if result.status not in {"PASS", "FAIL"}:
        return Check(result.name, "FAIL", "check returned an invalid status")
    return result


def run_gate(root: Path = Path("/"), *, focused: bool = False) -> GateResult:
    """Run every required check and return a complete fail-closed result."""

    checks: list[Check] = [
        _run_check(_check_cgroup_v2, root),
        _run_check(_check_cpu_controller, root),
        _run_check(_check_kernel_release, root),
        _run_check(
            lambda checked_root: _check_readable(
                checked_root, "cgroup_mode_readable", CGROUP_MODE
            ),
            root,
        ),
        _run_check(
            lambda checked_root: _check_readable(
                checked_root, "base_slice_ns_readable", BASE_SLICE_NS
            ),
            root,
        ),
    ]
    checks.extend(
        _run_check(
            lambda checked_root, command=command: _check_command(checked_root, command),
            root,
        )
        for command in REQUIRED_COMMANDS
    )
    checks.extend(
        (
            _run_check(
                lambda checked_root: _check_readable(
                    checked_root, "fixture_input_mp4", FIXTURE
                ),
                root,
            ),
            _run_check(
                lambda checked_root: _check_readable(
                    checked_root, "cpu_max_readable", CPU_MAX
                ),
                root,
            ),
            _run_check(
                lambda checked_root: _check_readable(
                    checked_root, "cpu_weight_readable", CPU_WEIGHT
                ),
                root,
            ),
        )
    )
    if focused:
        checks.extend(
            _run_check(
                lambda checked_root, path=path: _check_writable(
                    checked_root, f"{path.name}_writable", path
                ),
                root,
            )
            for path in WRITABLE_DEBUG_PATHS
        )
    status = "PASS" if all(check.status == "PASS" for check in checks) else "FAIL"
    return GateResult(status, focused, str(root), tuple(checks))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse runtime gate command-line options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path("/"), help="host root to inspect"
    )
    parser.add_argument(
        "--focused",
        action="store_true",
        help="require writable scheduler debug controls",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("runtime-gate.json"),
        help="output JSON path",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gate, print each result, and write JSON."""

    args = parse_args(argv)
    result = run_gate(args.root, focused=args.focused)
    for check in result.checks:
        print(f"{check.status} {check.name}: {check.detail}")
    try:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"FAIL json_output: {error}", file=sys.stderr)
        return 1
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
