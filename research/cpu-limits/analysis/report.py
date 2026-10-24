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


def compose_report(
    result: Mapping[str, object],
    *,
    summary_markdown: str,
    gates_markdown: str = "",
    environment: Mapping[str, object] | None = None,
    sample_size: Mapping[str, object] | None = None,
    figures: Sequence[str] = (),
) -> str:
    """Return a report that never treats absent cells as measured evidence."""

    cells = _cells(result)
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
    lines = [
        "# CPU-limits results",
        "",
        "This report describes complete confirmatory rows only.",
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
    lines.extend(("", "## Summary", "", summary_markdown.strip()))
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
    lines.extend(("", "## Limitations", "", "- Results apply to the recorded worker, workload images, measurement windows, and complete confirmatory rows.", "- Incomplete cells and missing contrasts remain incomplete; this report does not fill them.", "- Interpret focused cgroup-mode, base-slice, and sched_ext results only when their figures and gate evidence are present.", ""))
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
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(compose_report(result, summary_markdown=summary_markdown, gates_markdown=gates_markdown, environment=environment, sample_size=sample_size, figures=figures), encoding="utf-8")


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
    args = parser.parse_args(argv)
    result = _load_object(args.summary_json)
    assert result is not None
    figures = sorted(path.name for path in args.figures_dir.glob("*.svg")) if args.figures_dir else ()
    write_report(result, args.output, summary_markdown=args.summary_md.read_text(encoding="utf-8"), gates_markdown=args.gates.read_text(encoding="utf-8") if args.gates else "", environment=_load_object(args.environment), sample_size=_load_object(args.sample_size), figures=figures)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
