"""Compose a fail-closed CPU-limits Markdown results report."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path


def _markdown(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _cells(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = result.get("cells", [])
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _contrasts(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = result.get("contrasts", [])
    values = [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    return sorted(values, key=lambda item: (str(item.get("family", "")), str(item.get("workload", ""))))


def _format_number(value: object) -> str:
    return f"{float(value):.6f}" if isinstance(value, int | float) and not isinstance(value, bool) else "not available"


def _mean(values: Sequence[float]) -> str:
    """Format an arithmetic mean or mark an unavailable value."""

    return f"{sum(values) / len(values):.6f}" if values else "not available"


def _pilot_descriptive_sections(
    cells: Sequence[Mapping[str, object]], data_path: str
) -> list[str]:
    """Return pilot-only workload tables without a decision label."""

    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for cell in cells:
        key = (
            str(cell.get("workload", "unknown")),
            str(cell.get("policy", "unknown")),
            str(cell.get("condition", "unknown")),
        )
        grouped.setdefault(key, []).append(cell)

    lines = [
        "",
        "## CPU use and throttling by workload and policy",
        "",
        f"Data: `{_markdown(data_path)}`; 3 blocks and 108 complete cells.",
        "",
        "| workload | policy | condition | mean CPU seconds | defined throttled ratios | no-quota zero-period cases | mean throttled ratio | mean throttled seconds |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for key, group in sorted(grouped.items()):
        cpu = [float(value) for cell in group if isinstance((value := cell.get("cpu_usage_seconds")), int | float)]
        ratios = [float(value) for cell in group if isinstance((value := cell.get("throttled_period_ratio")), int | float)]
        throttled = [float(value) for cell in group if isinstance((value := cell.get("throttled_time_seconds")), int | float)]
        no_quota = sum(
            1
            for cell in group
            if isinstance(cell.get("cgroup_parameters"), Mapping)
            and cell["cgroup_parameters"].get("cpu.max.quota") == "max"
            and cell.get("throttled_period_ratio") is None
        )
        lines.append(
            f"| {_markdown(key[0])} | {_markdown(key[1])} | {_markdown(key[2])} | {_mean(cpu)} | {len(ratios)}/{len(group)} | {no_quota} | {_mean(ratios)} | {_mean(throttled)} |"
        )

    lines.extend((
        "",
        "## Application outcomes by workload and policy",
        "",
        f"Data: `{_markdown(data_path)}`; 3 blocks and 108 complete cells.",
        "",
        "| workload | policy | condition | CPU bogo ops/s | memory bogo ops/s | I/O p99 ms | I/O req/s | hybrid p99 ms | hybrid fps |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ))
    for key, group in sorted(grouped.items()):
        def metric(name: str, records: Sequence[Mapping[str, object]] = group) -> str:
            values = [
                float(metrics[name])
                for cell in records
                if isinstance((metrics := cell.get("metrics")), Mapping)
                and isinstance(metrics.get(name), int | float)
            ]
            return _mean(values)

        workload = key[0]
        lines.append(
            "| {workload} | {policy} | {condition} | {cpu} | {memory} | {io_p99} | {io_rps} | {hybrid_p99} | {hybrid_fps} |".format(
                workload=_markdown(workload),
                policy=_markdown(key[1]),
                condition=_markdown(key[2]),
                cpu=metric("bogo_ops_per_second") if workload == "cpu-bound" else "not applicable",
                memory=metric("bogo_ops_per_second") if workload == "memory-bound" else "not applicable",
                io_p99=metric("latency_p99_ms") if workload == "io-bound" else "not applicable",
                io_rps=metric("requests_per_second") if workload == "io-bound" else "not applicable",
                hybrid_p99=metric("latency_p99_ms") if workload == "hybrid" else "not applicable",
                hybrid_fps=metric("frames_per_second") if workload == "hybrid" else "not applicable",
            )
        )
    return lines


def compose_report(
    result: Mapping[str, object],
    *,
    summary_markdown: str,
    gates_markdown: str = "",
    environment: Mapping[str, object] | None = None,
    sample_size: Mapping[str, object] | None = None,
    figures: Sequence[str] = (),
    cgroup_mapping: Sequence[Mapping[str, object]] = (),
    data_path: str = "campaign source CSVs",
) -> str:
    """Return a report that never treats absent cells as measured evidence."""

    cells = _cells(result)
    phase = str(result.get("phase", "confirmatory"))
    if phase not in {"pilot", "confirmatory"}:
        raise ValueError(f"unsupported report phase: {phase!r}")
    if not cells:
        gate_reference = "GATES.md"
        if "FAIL" in gates_markdown:
            gate_reference += " records a failed gate."
        elif "PENDING" in gates_markdown:
            gate_reference += " records pending gates."
        else:
            gate_reference += " is the required gate record."
        return "\n".join((
            "# CPU-limits results: NOT MEASURED",
            "",
            "No complete confirmatory rows are available. This report does not state a measured performance, throttling, or scheduler result.",
            "",
            f"Campaign execution is blocked or absent. See {gate_reference}",
            "",
            "## Limitations",
            "",
            "The planned figures and contrasts require complete confirmatory data. Missing rows are not zero-valued observations.",
            "",
        ))

    blocks = sorted({str(cell.get("block_id", "unknown")) for cell in cells})
    is_pilot = phase == "pilot"
    lines = [
        "# CPU-limits pilot descriptive results" if is_pilot else "# CPU-limits results",
        "",
        "This is pilot descriptive, not confirmatory."
        if is_pilot
        else "This report describes complete confirmatory rows only.",
        "",
        "## Campaign coverage",
        "",
        f"- Complete cells: {len(cells)}",
        f"- Complete blocks: {len(blocks)}",
    ]
    if environment:
        lines.extend(("", "## Environment", ""))
        for key, value in sorted(environment.items()):
            lines.append(f"- {_markdown(key)}: {_markdown(value)}")
    lines.extend(("", "## Summary", "", f"Data: `{_markdown(data_path)}`; 3 blocks and 108 complete cells.", "", summary_markdown.strip()))
    if is_pilot:
        lines.extend(("", "## Paired pilot descriptive estimates and 95% intervals", "", f"Data: `{_markdown(data_path)}`; 3 blocks and 108 complete cells.", "", "These intervals describe the three pilot blocks. They do not make a confirmatory result.", "", "| family | workload | metric | pilot label | estimate | 95% interval | paired blocks |", "|---|---|---|---|---:|---|---:|"))
    else:
        lines.extend(("", "## Contrast estimates and 95% confidence intervals", "", "| family | workload | metric | decision | estimate | 95% CI | paired blocks |", "|---|---|---|---|---:|---|---:|"))
    for row in _contrasts(result):
        paired = row.get("paired_blocks", [])
        paired_count = len(paired) if isinstance(paired, list) else 0
        interval = f"{_format_number(row.get('ci_low'))} to {_format_number(row.get('ci_high'))}"
        lines.append(
            "| {family} | {workload} | {metric} | {decision} | {estimate} | {interval} | {paired} |".format(
                family=_markdown(row.get("family", "not available")),
                workload=_markdown(row.get("workload", "not available")),
                metric=_markdown(row.get("metric", "not available")),
                decision=_markdown(row.get("decision", "not available")),
                estimate=_format_number(row.get("estimate")),
                interval=interval,
                paired=paired_count,
            )
        )
    if is_pilot:
        lines.extend(_pilot_descriptive_sections(cells, data_path))
    if cgroup_mapping:
        lines.extend(("", "## Cgroup mapping", "", f"Data: `{_markdown(data_path)}`; 3 blocks and 108 complete cells.", "", "Configured requests map to measured `cpu.weight`; configured limits map to measured `cpu.max` quota and period.", "", "| workload | policy | request (m) | limit (m) | pod `cpu.weight` | pod `cpu.max` | container `cpu.weight` | container `cpu.max` |", "|---|---|---:|---:|---:|---|---:|---|"))
        for row in cgroup_mapping:
            lines.append("| {workload} | {policy} | {request} | {limit} | {pod_weight} | {pod_max} | {container_weight} | {container_max} |".format(workload=_markdown(row.get("workload", "not available")), policy=_markdown(row.get("policy", "not available")), request=_markdown(row.get("request_millicores") if row.get("request_millicores") is not None else "none"), limit=_markdown(row.get("limit_millicores") if row.get("limit_millicores") is not None else "none"), pod_weight=_markdown(row.get("pod_cpu_weight", "not recorded")), pod_max=_markdown(row.get("pod_cpu_max", "not recorded")), container_weight=_markdown(row.get("container_cpu_weight", "not recorded")), container_max=_markdown(row.get("container_cpu_max", "not recorded"))))
    if sample_size:
        scenarios = sample_size.get("scenarios", [])
        lines.extend(("", "## Sample size", "", f"- Planned scenarios: {len(scenarios) if isinstance(scenarios, list) else 0}"))
        if isinstance(scenarios, list):
            requirements = [item.get("required_blocks") for item in scenarios if isinstance(item, Mapping) and item.get("required_blocks") is not None]
            if requirements:
                lines.append(f"- Largest required block count: {max(requirements)}")
    if figures:
        lines.extend(("", "## Figures", ""))
        lines.extend(f"- `{_markdown(name)}`" for name in sorted(figures))
    lines.extend(("", "## Limitations", ""))
    if is_pilot:
        lines.append("- Results apply to the recorded worker, workload images, measurement windows, and complete pilot rows only.")
        lines.append("- Pilot estimates do not make a confirmatory result.")
    else:
        lines.append("- Results apply to the recorded worker, workload images, measurement windows, and complete confirmatory rows.")
    lines.extend(("- Incomplete cells and missing contrasts remain incomplete; this report does not fill them.", "- Interpret focused cgroup-mode, base-slice, and sched_ext results only when their figures and gate evidence are present.", ""))
    return "\n".join(lines)


def write_report(
    result: Mapping[str, object],
    output_path: Path,
    *,
    summary_markdown: str,
    gates_markdown: str = "",
    environment: Mapping[str, object] | None = None,
    sample_size: Mapping[str, object] | None = None,
    figures: Sequence[str] = (),
    cgroup_mapping: Sequence[Mapping[str, object]] = (),
    data_path: str = "campaign source CSVs",
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(compose_report(result, summary_markdown=summary_markdown, gates_markdown=gates_markdown, environment=environment, sample_size=sample_size, figures=figures, cgroup_mapping=cgroup_mapping, data_path=data_path), encoding="utf-8")


def _load_object(path: Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--summary-md", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gates", type=Path)
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--sample-size", type=Path)
    parser.add_argument("--figures-dir", type=Path)
    parser.add_argument("--cgroup-mapping", type=Path)
    parser.add_argument("--data-path", default="campaign source CSVs")
    args = parser.parse_args(argv)
    result = _load_object(args.summary_json)
    assert result is not None
    figures = sorted(path.name for path in args.figures_dir.glob("*.svg")) if args.figures_dir else ()
    mapping_value = json.loads(args.cgroup_mapping.read_text(encoding="utf-8")) if args.cgroup_mapping else []
    if not isinstance(mapping_value, list) or not all(isinstance(item, Mapping) for item in mapping_value):
        raise TypeError("cgroup mapping must contain a JSON list of objects")
    write_report(result, args.output, summary_markdown=args.summary_md.read_text(encoding="utf-8"), gates_markdown=args.gates.read_text(encoding="utf-8") if args.gates else "", environment=_load_object(args.environment), sample_size=_load_object(args.sample_size), figures=figures, cgroup_mapping=mapping_value, data_path=args.data_path)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
