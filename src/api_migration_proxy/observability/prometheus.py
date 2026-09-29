"""Prometheus text exposition for process metric snapshots."""

from __future__ import annotations

from .metrics import MetricSnapshot


def _labels(values: tuple[tuple[str, str], ...]) -> str:
    if not values:
        return ""
    fields = []
    for key, value in values:
        escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        fields.append(f'{key}="{escaped}"')
    return "{" + ",".join(fields) + "}"


def prometheus_text(snapshot: MetricSnapshot) -> str:
    lines: list[str] = []
    declared: set[str] = set()

    def declare(name: str, kind: str) -> None:
        if name not in declared:
            lines.append(f"# TYPE {name} {kind}")
            declared.add(name)

    for source, kind in ((snapshot.counters, "counter"), (snapshot.gauges, "gauge")):
        for (name, labels), value in sorted(source.items()):
            declare(name, kind)
            lines.append(f"{name}{_labels(labels)} {value}")
    for (name, labels), histogram in sorted(snapshot.histograms.items()):
        declare(name, "histogram")
        for bound, value in zip(histogram.bounds, histogram.buckets, strict=True):
            bucket_labels = (*labels, ("le", str(bound)))
            lines.append(f"{name}_bucket{_labels(bucket_labels)} {value}")
        lines.append(f"{name}_bucket{_labels((*labels, ('le', '+Inf')))} {histogram.count}")
        lines.append(f"{name}_count{_labels(labels)} {histogram.count}")
        lines.append(f"{name}_sum{_labels(labels)} {histogram.total}")
    return "\n".join(lines) + ("\n" if lines else "")
