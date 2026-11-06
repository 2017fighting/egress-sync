"""Prometheus text exposition, hand-rolled.

Why not `prometheus_client`: it is a third-party dependency for ~120 lines of
formatting, and the metric set here is small and fixed. The registry below is
also the list of metrics the alerting rules can rely on — a name that is not in
it raises instead of quietly emitting something nobody scrapes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    name: str
    type: str
    help: str


SPECS: tuple[Spec, ...] = (
    Spec("egress_sync_build_info", "gauge", "Always 1; the version label carries the build."),
    Spec("egress_sync_derived_entries", "gauge", "Entries derived in the most recent round, by kind."),
    Spec("egress_sync_entries", "gauge", "Entries in the last successfully published set, by kind."),
    Spec("egress_sync_invalid_entries", "gauge", "Unparseable tokens the sources produced in the last round, by kind."),
    Spec(
        "egress_sync_last_refresh_success_timestamp_seconds",
        "gauge",
        "Unix time of the last successful provider refresh; 0 if never.",
    ),
    Spec("egress_sync_last_round_result", "gauge", "One-hot membership of the last round's classification."),
    Spec("egress_sync_last_round_timestamp_seconds", "gauge", "Unix time the last round finished."),
    Spec(
        "egress_sync_last_success_timestamp_seconds",
        "gauge",
        "Unix time of the last fully published round; 0 if never.",
    ),
    Spec("egress_sync_publish_failures_total", "counter", "Publish calls that did not return 2xx, by kind."),
    Spec("egress_sync_publish_rejected_total", "counter", "Rounds the gate refused to publish, by kind and reason."),
    Spec("egress_sync_refresh_failures_total", "counter", "Provider refreshes that failed."),
    Spec("egress_sync_rounds_total", "counter", "Rounds completed, by result class."),
    Spec(
        "egress_sync_tracker_ip_literals",
        "gauge",
        "Announce hosts that are bare IP literals (no rule kind carries them yet).",
    ),
)

SPEC_BY_NAME = {spec.name: spec for spec in SPECS}

# The values `egress_sync_last_round_result` / `egress_sync_rounds_total` may
# carry. Anything else is a bug, so it is checked at render time.
RESULTS = ("published", "gate_rejected", "source_error", "publish_error")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(float(value))


class Metrics:
    def __init__(self, version: str = "") -> None:
        self._lock = threading.Lock()
        self._series: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._version = version
        self.set("egress_sync_build_info", 1, version=version or "unknown")
        for name in SPEC_BY_NAME:
            self._series.setdefault(name, {})
        for result in RESULTS:
            self.set("egress_sync_last_round_result", 0, result=result)

    # -- writers ---------------------------------------------------------
    def set(self, name: str, value: float, **labels: str) -> None:
        series = self._series.setdefault(self._check(name), {})
        with self._lock:
            series[_labels(labels)] = float(value)

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = _labels(labels)
        series = self._series.setdefault(self._check(name), {})
        with self._lock:
            series[key] = series.get(key, 0.0) + amount

    def set_one_hot(self, name: str, active: str, choices: tuple[str, ...], label: str) -> None:
        for choice in choices:
            self.set(name, 1 if choice == active else 0, **{label: choice})

    # -- readers ---------------------------------------------------------
    def get(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._series.get(name, {}).get(_labels(labels), 0.0)

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for spec in SPECS:
                series = self._series.get(spec.name) or {}
                lines.append(f"# HELP {spec.name} {spec.help}")
                lines.append(f"# TYPE {spec.name} {spec.type}")
                if not series:
                    # A metric with no series yet still needs a sample, or panels
                    # and alerts silently see nothing on a fresh process.
                    lines.append(f"{spec.name} 0")
                    continue
                for key, value in sorted(series.items()):
                    rendered = ",".join(f'{label}="{_escape(val)}"' for label, val in key)
                    prefix = f"{spec.name}{{{rendered}}}" if rendered else spec.name
                    lines.append(f"{prefix} {_format_value(value)}")
            return "\n".join(lines) + "\n"

    def _check(self, name: str) -> str:
        if name not in SPEC_BY_NAME:
            raise KeyError(f"undeclared metric: {name}")
        return name


def _labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))
