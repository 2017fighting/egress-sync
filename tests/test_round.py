"""The round: what gets published, what does not, and what the metrics say after.

Fake sources stand in for the three upstreams, so these tests exercise the real
pipeline (derive -> gate -> publish -> refresh) without a socket, a cluster or a
file.
"""

from __future__ import annotations

from egress_sync.app import GATE_REJECTED, PUBLISH_ERROR, PUBLISHED, SOURCE_ERROR, EgressSync
from egress_sync.config import Config
from egress_sync.derive import NodeIPs, RpcAddrs, TrackerHosts
from egress_sync.metrics import Metrics

from tests.conftest import (
    StubNodeSource,
    StubPublisher,
    StubRefresher,
    StubRpcSource,
    StubTrackerSource,
)


def make_app(
    config: Config,
    metrics: Metrics,
    *,
    nodes: NodeIPs | Exception | None = None,
    trackers: TrackerHosts | Exception | None = None,
    rpc: RpcAddrs | Exception | None = None,
    publisher: StubPublisher | None = None,
    refresher: StubRefresher | None = None,
) -> tuple[EgressSync, StubPublisher, StubRefresher]:
    publisher = publisher or StubPublisher()
    refresher = refresher or StubRefresher()
    app = EgressSync(
        config,
        transport=object(),  # never used: every boundary is injected
        metrics=metrics,
        node_source=StubNodeSource(nodes or NodeIPs(("10.0.0.11", "10.0.0.12"), ())),
        tracker_source=StubTrackerSource(trackers or TrackerHosts(("a.example.com", "b.example.com"), (), ())),
        rpc_source=StubRpcSource(rpc or RpcAddrs(("1.2.3.4", "5.6.7.8"), ())),
        publisher=publisher,
        refresher=refresher,
    )
    return app, publisher, refresher


def test_successful_round_publishes_all_four_kinds(config: Config, metrics: Metrics) -> None:
    app, publisher, refresher = make_app(config, metrics)

    outcome = app.run_round()

    assert outcome.result == PUBLISHED
    assert [kind for kind, _ in publisher.published] == ["src", "tracker", "rpc-domain", "rpc-ip"]
    assert dict(publisher.published)["rpc-domain"] == ("rpc.hentaiathome.net",)
    assert refresher.calls == 1
    assert metrics.get("egress_sync_entries", kind="tracker") == 2
    assert metrics.get("egress_sync_last_success_timestamp_seconds") > 0
    assert metrics.get("egress_sync_last_round_result", result=PUBLISHED) == 1
    assert metrics.get("egress_sync_rounds_total", result=PUBLISHED) == 1


def test_gate_rejection_publishes_nothing_and_counts_it(config: Config, metrics: Metrics) -> None:
    # qbt answering with an empty dictionary: the incident this gate exists for.
    app, publisher, refresher = make_app(config, metrics, trackers=TrackerHosts((), (), ()))

    outcome = app.run_round()

    assert outcome.result == GATE_REJECTED
    assert publisher.published == []
    assert refresher.calls == 0
    assert metrics.get("egress_sync_publish_rejected_total", kind="tracker", reason="empty") == 1
    assert metrics.get("egress_sync_entries", kind="tracker") == 0
    assert metrics.get("egress_sync_last_success_timestamp_seconds") == 0
    assert metrics.get("egress_sync_last_round_result", result=GATE_REJECTED) == 1


def test_shrinking_set_is_rejected_after_a_successful_round(config: Config, metrics: Metrics) -> None:
    app, publisher, _ = make_app(config, metrics)
    app.run_round()
    assert len(publisher.published) == 4

    # Same app, fewer trackers next round: the in-memory baseline catches it.
    app._trackers = StubTrackerSource(TrackerHosts(("a.example.com",), (), ()))
    publisher.published.clear()

    outcome = app.run_round()

    assert outcome.result == GATE_REJECTED
    assert publisher.published == []
    assert metrics.get("egress_sync_publish_rejected_total", kind="tracker", reason="shrink") == 1


def test_growth_publishes_normally(config: Config, metrics: Metrics) -> None:
    app, publisher, _ = make_app(config, metrics)
    app.run_round()

    app._trackers = StubTrackerSource(TrackerHosts(("a.example.com", "c.example.com", "d.example.com"), (), ()))
    publisher.published.clear()

    outcome = app.run_round()

    assert outcome.result == PUBLISHED
    assert dict(publisher.published)["tracker"] == ("a.example.com", "c.example.com", "d.example.com")


def test_unreachable_upstream_publishes_nothing(config: Config, metrics: Metrics) -> None:
    app, publisher, refresher = make_app(config, metrics, nodes=RuntimeError("boom"))
    app._nodes = StubNodeSource(Exception("connection refused"))

    outcome = app.run_round()

    assert outcome.result == SOURCE_ERROR
    assert publisher.published == []
    assert refresher.calls == 0
    assert metrics.get("egress_sync_last_round_result", result=SOURCE_ERROR) == 1
    assert "connection refused" in outcome.detail


def test_missing_rpc_state_file_publishes_nothing(config: Config, metrics: Metrics) -> None:
    from egress_sync.sources import SourceError

    app, publisher, _ = make_app(config, metrics)
    app._rpc = StubRpcSource(SourceError("hath RPC state file does not exist: /var/lib/hath/rpc-servers.txt"))

    outcome = app.run_round()

    assert outcome.result == SOURCE_ERROR
    assert publisher.published == []
    assert "does not exist" in outcome.detail


def test_partial_publish_failure_is_recorded(config: Config, metrics: Metrics) -> None:
    publisher = StubPublisher(fail_at="rpc-domain")
    app, _, refresher = make_app(config, metrics, publisher=publisher)

    outcome = app.run_round()

    assert outcome.result == PUBLISH_ERROR
    assert [kind for kind, _ in publisher.published] == ["src", "tracker"]
    assert "rpc-domain" in outcome.detail
    assert metrics.get("egress_sync_publish_failures_total", kind="rpc-domain") == 1
    assert metrics.get("egress_sync_last_success_timestamp_seconds") == 0
    # A failed round leaves no baseline behind, so the next round cannot shrink-gate
    # against a version that never fully existed.
    assert app._published_counts == {}
    assert refresher.calls == 0


def test_refresh_failure_does_not_downgrade_the_round(config: Config, metrics: Metrics) -> None:
    refresher = StubRefresher(failure=RuntimeError("controller unreachable"))
    app, publisher, _ = make_app(config, metrics, refresher=refresher)

    outcome = app.run_round()

    assert outcome.result == PUBLISHED
    assert len(publisher.published) == 4
    assert metrics.get("egress_sync_refresh_failures_total") == 1
    assert metrics.get("egress_sync_last_success_timestamp_seconds") > 0
    assert metrics.get("egress_sync_last_refresh_success_timestamp_seconds") == 0


def test_refresh_disabled_is_not_a_failure(config: Config, metrics: Metrics) -> None:
    app, _, refresher = make_app(config, metrics, refresher=StubRefresher(enabled=False))

    outcome = app.run_round()

    assert outcome.result == PUBLISHED
    assert refresher.calls == 0
    assert metrics.get("egress_sync_refresh_failures_total") == 0


def test_dry_run_derives_and_gates_but_publishes_nothing(config: Config, metrics: Metrics) -> None:
    app, publisher, refresher = make_app(config, metrics)

    outcome = app.run_round(dry_run=True)

    assert outcome.result == PUBLISHED
    assert outcome.dry_run is True
    assert publisher.published == []
    assert refresher.calls == 0
    assert metrics.get("egress_sync_derived_entries", kind="tracker") == 2
    assert metrics.get("egress_sync_entries", kind="tracker") == 0
    assert metrics.get("egress_sync_last_success_timestamp_seconds") == 0


def test_dry_run_still_reports_gate_rejections(config: Config, metrics: Metrics) -> None:
    app, publisher, _ = make_app(config, metrics, rpc=RpcAddrs((), ()))

    outcome = app.run_round(dry_run=True)

    assert outcome.result == GATE_REJECTED
    assert publisher.published == []


def test_tracker_ip_literals_are_counted_not_dropped_silently(config: Config, metrics: Metrics) -> None:
    app, publisher, _ = make_app(
        config,
        metrics,
        trackers=TrackerHosts(("a.example.com",), ("1.2.3.4",), ()),
    )

    outcome = app.run_round()

    assert outcome.result == PUBLISHED
    assert metrics.get("egress_sync_tracker_ip_literals") == 1
    # The domain kind carries only domains; the literal is a known gap, visible in
    # the metric rather than folded into a domain rule-set.
    assert dict(publisher.published)["tracker"] == ("a.example.com",)


def test_invalid_entries_are_rejected_with_the_offending_value(config: Config, metrics: Metrics) -> None:
    app, publisher, _ = make_app(config, metrics, rpc=RpcAddrs(("1.2.3.4",), ("garbage",)))

    outcome = app.run_round()

    assert outcome.result == GATE_REJECTED
    assert publisher.published == []
    assert "garbage" in outcome.detail


def test_entry_counts_are_reported_per_kind(config: Config, metrics: Metrics) -> None:
    app, _, _ = make_app(config, metrics)

    outcome = app.run_round()

    assert outcome.entries == {"src": 2, "tracker": 2, "rpc-domain": 1, "rpc-ip": 2}
    assert metrics.get("egress_sync_entries", kind="src") == 2
    assert metrics.get("egress_sync_entries", kind="rpc-ip") == 2
