"""Metrics exposition: the shapes the scrape config and the alert rules rely on."""

from __future__ import annotations

import pytest
from egress_sync.metrics import RESULTS, Metrics


def test_render_is_prometheus_text_format(metrics: Metrics) -> None:
    metrics.set("egress_sync_entries", 62, kind="tracker")
    metrics.inc("egress_sync_rounds_total", result="published")

    text = metrics.render()

    assert "# TYPE egress_sync_entries gauge" in text
    assert 'egress_sync_entries{kind="tracker"} 62' in text
    assert 'egress_sync_rounds_total{result="published"} 1' in text
    assert text.endswith("\n")


def test_every_metric_has_a_type_line() -> None:
    text = Metrics("test").render()

    for line in text.splitlines():
        if line.startswith("# HELP "):
            name = line.split()[2]
            assert f"# TYPE {name} " in text


def test_unset_metrics_still_render_one_sample() -> None:
    # A fresh pod must not look like a missing target on a dashboard.
    text = Metrics("test").render()

    assert "egress_sync_last_success_timestamp_seconds 0" in text


def test_counters_accumulate_and_gauges_replace(metrics: Metrics) -> None:
    metrics.inc("egress_sync_rounds_total", result="gate_rejected")
    metrics.inc("egress_sync_rounds_total", result="gate_rejected")
    metrics.set("egress_sync_entries", 5, kind="src")
    metrics.set("egress_sync_entries", 6, kind="src")

    assert metrics.get("egress_sync_rounds_total", result="gate_rejected") == 2
    assert metrics.get("egress_sync_entries", kind="src") == 6


def test_one_hot_result_is_exclusive(metrics: Metrics) -> None:
    metrics.set_one_hot("egress_sync_last_round_result", "source_error", RESULTS, "result")

    active = [result for result in RESULTS if metrics.get("egress_sync_last_round_result", result=result) == 1]

    assert active == ["source_error"]


def test_undeclared_metric_is_an_error(metrics: Metrics) -> None:
    with pytest.raises(KeyError):
        metrics.set("egress_sync_something_invented", 1)


def test_label_values_are_escaped(metrics: Metrics) -> None:
    metrics.inc("egress_sync_rounds_total", result='we"ird\nvalue')

    assert 'result="we\\"ird\\nvalue"' in metrics.render()


def test_float_values_are_not_rendered_as_integers(metrics: Metrics) -> None:
    metrics.set("egress_sync_last_success_timestamp_seconds", 1700000000.5)

    assert "egress_sync_last_success_timestamp_seconds 1700000000.5" in metrics.render()
