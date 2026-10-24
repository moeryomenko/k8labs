"""Write deterministic, dependency-free SVG figures for CPU-limits results."""

from __future__ import annotations

import argparse
import html
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

WIDTH = 960
HEIGHT = 540
MARGIN = 70


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return float(value)
    return None


def _cells(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = result.get("cells", [])
    if not isinstance(raw, list):
        return []
    cells = [item for item in raw if isinstance(item, Mapping)]
    return sorted(
        cells,
        key=lambda item: (
            str(item.get("workload", "")),
            str(item.get("policy", "")),
            str(item.get("condition", "")),
            str(item.get("block_id", "")),
            str(item.get("cell_id", "")),
        ),
    )


def _svg(title: str, content: Sequence[str]) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540" role="img">',
        f"<title>{_text(title)}</title>",
        '<rect width="960" height="540" fill="white"/>',
        f'<text x="40" y="40" font-family="sans-serif" font-size="22">{_text(title)}</text>',
    ]
    parts.extend(content)
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def _empty_figure(title: str) -> bytes:
    return _svg(title, ['<text x="40" y="90" font-family="sans-serif" font-size="18">NOT MEASURED: no complete confirmatory cells.</text>'])


def _scale(value: float, low: float, high: float, start: float, size: float) -> float:
    if high <= low:
        return start + size / 2
    return start + (value - low) * size / (high - low)


def _performance_heatmap(cells: Sequence[Mapping[str, object]]) -> bytes:
    if not cells:
        return _empty_figure("Policy x workload performance heatmap")
    workloads = sorted({str(cell.get("workload", "unknown")) for cell in cells})
    policies = sorted({str(cell.get("policy", "unknown")) for cell in cells})
    values: dict[tuple[str, str], list[float]] = {}
    for cell in cells:
        value = _number(cell.get("throughput"))
        if value is not None:
            values.setdefault((str(cell.get("policy")), str(cell.get("workload"))), []).append(value)
    if not values:
        return _empty_figure("Policy x workload performance heatmap")
    means = {key: sum(items) / len(items) for key, items in values.items()}
    maximum = max(means.values())
    cell_width = 700 / max(1, len(workloads))
    cell_height = 320 / max(1, len(policies))
    content = ['<text x="70" y="75" font-family="sans-serif" font-size="13">Mean throughput by complete cell</text>']
    for index, workload in enumerate(workloads):
        x = 200 + index * cell_width
        content.append(f'<text x="{x + cell_width / 2:.2f}" y="105" text-anchor="middle" font-family="sans-serif" font-size="12">{_text(workload)}</text>')
    for row, policy in enumerate(policies):
        y = 125 + row * cell_height
        content.append(f'<text x="190" y="{y + cell_height / 2:.2f}" text-anchor="end" font-family="sans-serif" font-size="12">{_text(policy)}</text>')
        for column, workload in enumerate(workloads):
            value = means.get((policy, workload))
            intensity = 0 if value is None else round(220 * value / maximum)
            color = f"rgb({255 - intensity},{255 - intensity // 3},255)"
            x = 200 + column * cell_width
            label = "missing" if value is None else f"{value:.3f}"
            content.extend((f'<rect x="{x:.2f}" y="{y:.2f}" width="{cell_width - 2:.2f}" height="{cell_height - 2:.2f}" fill="{color}" stroke="#555"/>', f'<text x="{x + cell_width / 2:.2f}" y="{y + cell_height / 2:.2f}" text-anchor="middle" font-family="monospace" font-size="12">{label}</text>'))
    return _svg("Policy x workload performance heatmap", content)


def _scatter(cells: Sequence[Mapping[str, object]]) -> bytes:
    points = [(cell, _number(cell.get("cpu_usage_seconds")), _number(cell.get("cgroup_parameters", {}).get("cpu.max.quota") if isinstance(cell.get("cgroup_parameters"), Mapping) else None)) for cell in cells]
    points = [(cell, usage, quota) for cell, usage, quota in points if usage is not None and quota is not None]
    if not points:
        return _empty_figure("CPU usage versus quota")
    usages, quotas = [point[1] for point in points], [point[2] for point in points]
    content = ['<line x1="70" y1="470" x2="900" y2="470" stroke="black"/>', '<line x1="70" y1="80" x2="70" y2="470" stroke="black"/>', '<text x="450" y="515" text-anchor="middle" font-family="sans-serif">CPU quota (microseconds)</text>', '<text x="20" y="275" transform="rotate(-90 20 275)" text-anchor="middle" font-family="sans-serif">CPU usage (seconds)</text>']
    for cell, usage, quota in points:
        ratio = max(0.0, min(1.0, _number(cell.get("throttled_period_ratio")) or 0.0))
        x = _scale(quota, min(quotas), max(quotas), 90, 790)
        y = 470 - _scale(usage, min(usages), max(usages), 0, 360)
        radius = 5 + 16 * ratio
        content.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" fill="rgb({round(255 * ratio)},80,{round(255 * (1 - ratio))})"><title>{_text(cell.get("cell_id", "cell"))}: throttled ratio {ratio:.6f}</title></circle>')
    return _svg("CPU usage versus quota", content)


def _throttling_bars(cells: Sequence[Mapping[str, object]]) -> bytes:
    values = [(cell, _number(cell.get("throttled_period_ratio")), _number(cell.get("throttled_time_seconds"))) for cell in cells]
    values = [(cell, ratio, time) for cell, ratio, time in values if ratio is not None or time is not None]
    if not values:
        return _empty_figure("Throttled periods and throttled time")
    maximum = max(max(ratio or 0.0, time or 0.0) for _, ratio, time in values) or 1.0
    width = min(700 / len(values), 48)
    content = ['<text x="70" y="75" font-family="sans-serif" font-size="13">Blue: throttled-period ratio. Orange: throttled time (seconds).</text>', '<line x1="70" y1="470" x2="900" y2="470" stroke="black"/>']
    for index, (cell, ratio, time) in enumerate(values):
        x = 80 + index * (width + 8)
        for offset, value, color in ((0, ratio, "#357edd"), (width / 2, time, "#f28e2b")):
            height = 360 * (value or 0.0) / maximum
            content.append(f'<rect x="{x + offset:.2f}" y="{470 - height:.2f}" width="{width / 2 - 2:.2f}" height="{height:.2f}" fill="{color}"><title>{_text(cell.get("cell_id", "cell"))}</title></rect>')
    return _svg("Throttled periods and throttled time", content)


def _latency_cdf(cells: Sequence[Mapping[str, object]]) -> bytes:
    samples: list[tuple[str, float]] = []
    for cell in cells:
        if cell.get("workload") not in {"io-bound", "hybrid"}:
            continue
        values = cell.get("latency_samples_ms")
        if isinstance(values, list):
            samples.extend((str(cell.get("workload")), value) for item in values if (value := _number(item)) is not None)
        elif (value := _number(cell.get("latency_p99_ms"))) is not None:
            samples.append((str(cell.get("workload")), value))
    if not samples:
        return _empty_figure("I/O-bound and hybrid latency CDF")
    maximum = max(value for _, value in samples)
    content = ['<line x1="70" y1="470" x2="900" y2="470" stroke="black"/>', '<line x1="70" y1="80" x2="70" y2="470" stroke="black"/>']
    colors = {"io-bound": "#357edd", "hybrid": "#e15759"}
    for workload in sorted({kind for kind, _ in samples}):
        values = sorted(value for kind, value in samples if kind == workload)
        points = []
        for index, value in enumerate(values, start=1):
            x = 70 + 830 * value / maximum
            y = 470 - 390 * index / len(values)
            points.append(f"{x:.2f},{y:.2f}")
        content.append(f'<polyline fill="none" stroke="{colors.get(workload, "#444")}" stroke-width="3" points="{" ".join(points)}"/><text x="{120 + 150 * len(content):.2f}" y="90" font-family="sans-serif">{_text(workload)}</text>')
    return _svg("I/O-bound and hybrid latency CDF", content)


def _trace_rows(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = result.get("trace_rows", result.get("traces", []))
    return sorted((item for item in raw if isinstance(item, Mapping)) if isinstance(raw, list) else [], key=lambda item: (str(item.get("cell_id", "")), _number(item.get("ts_ns")) or 0.0, str(item.get("event", ""))))


def _runqueue_histogram(traces: Sequence[Mapping[str, object]]) -> bytes:
    values = [_number(item.get("latency_ns")) for item in traces]
    usable = [value for value in values if value is not None and value >= 0.0]
    if not usable:
        return _empty_figure("Run-queue latency histogram")
    maximum = max(usable) or 1.0
    buckets = [0] * 10
    for value in usable:
        buckets[min(9, int(value / maximum * 10))] += 1
    highest = max(buckets) or 1
    content = ['<line x1="70" y1="470" x2="900" y2="470" stroke="black"/>']
    for index, count in enumerate(buckets):
        height = 360 * count / highest
        x = 80 + index * 80
        content.append(f'<rect x="{x}" y="{470 - height:.2f}" width="65" height="{height:.2f}" fill="#59a14f"/><text x="{x + 32}" y="490" text-anchor="middle" font-size="10">{maximum * index / 10:.0f}</text>')
    return _svg("Run-queue latency histogram", content)


def _timeline(traces: Sequence[Mapping[str, object]]) -> bytes:
    selected = sorted({str(item.get("cell_id", "")) for item in traces})[:2]
    rows = [item for item in traces if str(item.get("cell_id", "")) in selected]
    timestamps = [_number(item.get("ts_ns")) for item in rows]
    usable = [value for value in timestamps if value is not None]
    if not rows or not usable:
        return _empty_figure("On-CPU and off-CPU timeline with throttle markers")
    low, high = min(usable), max(usable)
    content = ['<line x1="100" y1="460" x2="900" y2="460" stroke="black"/>']
    for row_index, cell_id in enumerate(selected):
        y = 150 + row_index * 160
        content.append(f'<text x="90" y="{y}" text-anchor="end" font-family="sans-serif" font-size="12">{_text(cell_id)}</text>')
        for event in (item for item in rows if str(item.get("cell_id")) == cell_id):
            timestamp = _number(event.get("ts_ns"))
            if timestamp is None:
                continue
            x = _scale(timestamp, low, high, 110, 770)
            label = str(event.get("event", ""))
            color = "#d62728" if "throttle" in label.lower() else "#357edd"
            content.append(f'<line x1="{x:.2f}" y1="{y - 35}" x2="{x:.2f}" y2="{y + 35}" stroke="{color}" stroke-width="3"><title>{_text(label)}</title></line>')
    return _svg("On-CPU and off-CPU timeline with throttle markers", content)


def _parameter_map(cells: Sequence[Mapping[str, object]]) -> bytes:
    entries = []
    for cell in cells:
        parameters = cell.get("cgroup_parameters")
        if isinstance(parameters, Mapping):
            entries.extend((str(cell.get("cell_id", "cell")), str(key), str(value)) for key, value in sorted(parameters.items()))
    if not entries:
        return _empty_figure("Cgroup parameter map")
    content = ['<text x="70" y="80" font-family="sans-serif" font-size="13">Measured cgroup settings at the selected enforcement level</text>']
    for index, (cell_id, key, value) in enumerate(entries[:18]):
        y = 115 + index * 21
        content.append(f'<text x="70" y="{y}" font-family="monospace" font-size="12">{_text(cell_id)}: {_text(key)} = {_text(value)}</text>')
    return _svg("Cgroup parameter map", content)


def _optional_plot(title: str, rows: object) -> bytes:
    if not isinstance(rows, list) or not rows:
        return _empty_figure(title)
    values = [_number(item.get("value")) for item in rows if isinstance(item, Mapping)]
    usable = [value for value in values if value is not None]
    if not usable:
        return _empty_figure(title)
    maximum = max(usable) or 1.0
    content = ['<line x1="70" y1="470" x2="900" y2="470" stroke="black"/>']
    for index, item in enumerate(item for item in rows if isinstance(item, Mapping)):
        value = _number(item.get("value"))
        if value is None:
            continue
        height = 360 * value / maximum
        x = 80 + index * 70
        label = item.get("label", item.get("mode", item.get("scheduler", index)))
        content.append(f'<rect x="{x}" y="{470 - height:.2f}" width="50" height="{height:.2f}" fill="#af7aa1"/><text x="{x + 25}" y="490" text-anchor="middle" font-size="10">{_text(label)}</text>')
    return _svg(title, content)


def render_figures(result: Mapping[str, object], output_dir: Path) -> list[str]:
    """Render all core figures and optional focused-experiment figures."""

    output_dir.mkdir(parents=True, exist_ok=True)
    cells = _cells(result)
    traces = _trace_rows(result)
    figures = {
        "performance-heatmap.svg": _performance_heatmap(cells),
        "cpu-quota-scatter.svg": _scatter(cells),
        "throttling-bars.svg": _throttling_bars(cells),
        "latency-cdf.svg": _latency_cdf(cells),
        "runqueue-histogram.svg": _runqueue_histogram(traces),
        "timeline.svg": _timeline(traces),
        "cgroup-parameter-map.svg": _parameter_map(cells),
    }
    if result.get("phase") == "pilot" and not traces:
        message = "PILOT DESCRIPTIVE: no trace rows were supplied with the CSV inputs."
        figures["runqueue-histogram.svg"] = _svg("Run-queue latency histogram", [f'<text x="40" y="90" font-family="sans-serif" font-size="18">{message}</text>'])
        figures["timeline.svg"] = _svg("On-CPU and off-CPU timeline with throttle markers", [f'<text x="40" y="90" font-family="sans-serif" font-size="18">{message}</text>'])
    optional = (("cgroup_mode", "cgroup-mode.svg", "cgroup_mode effect"), ("base_slice", "base-slice.svg", "base_slice_ns effect"), ("sched_ext", "sched-ext-quota.svg", "sched_ext quota enforcement"))
    for key, filename, title in optional:
        if result.get(key):
            figures[filename] = _optional_plot(title, result[key])
    for filename, payload in sorted(figures.items()):
        (output_dir / filename).write_bytes(payload)
    return sorted(figures)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = json.loads(args.summary.read_text(encoding="utf-8"))
    if not isinstance(result, Mapping):
        raise TypeError("summary JSON must be an object")
    print(f"wrote {len(render_figures(result, args.output_dir))} figures")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
