"""The round: what gets published, what does not, and what the metrics say after.

Fake sources stand in for the three upstreams, so these tests exercise the real
pipeline (derive -> gate -> publish -> refresh) without a socket, a cluster or a
file.
"""

from __future__ import annotations

from egress_sync.app import GATE_REJECTED, PARTIAL, PUBLISH_ERROR, PUBLISHED, RPC_KINDS, SOURCE_ERROR, EgressSync
from egress_sync.config import Config
from egress_sync.derive import SKIP_EMPTY, SKIP_MISSING, NodeIPs, RpcAddrs, RpcState, TrackerHosts
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
    rpc: RpcState | RpcAddrs | Exception | None = None,
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


def test_missing_rpc_state_file_skips_both_rpc_kinds_and_publishes_the_rest(config: Config, metrics: Metrics) -> None:
    # home-ops#196: the state file is another app's output. Its absence must not
    # stop the tracker/source sets from being refreshed, and must never publish an
    # empty RPC set (the worker keeps the previous revision).
    app, publisher, refresher = make_app(
        config, metrics, rpc=RpcState(RpcAddrs((), ()), SKIP_MISSING, "hath RPC state file does not exist")
    )

    outcome = app.run_round()

    assert outcome.result == PARTIAL
    assert set(outcome.skipped) == set(RPC_KINDS)
    assert [kind for kind, _ in publisher.published] == ["src", "tracker"]
    assert refresher.calls == 1  # the sets that DID publish are refreshed
    assert metrics.get("egress_sync_last_round_result", result=PARTIAL) == 1
    assert metrics.get("egress_sync_last_round_result", result=PUBLISHED) == 0
    for kind in RPC_KINDS:
        assert metrics.get("egress_sync_skipped_total", kind=kind, reason=SKIP_MISSING) == 1
        assert metrics.get("egress_sync_derived_entries", kind=kind) == 0
        # Never published by this round: the previous version is what stays live.
        assert metrics.get("egress_sync_entries", kind=kind) == 0
    # The sets that did publish are a success by the "can still publish" measure.
    assert metrics.get("egress_sync_last_success_timestamp_seconds") > 0
    assert "missing" in outcome.detail


def test_empty_rpc_state_file_is_a_skip_not_a_refusal(config: Config, metrics: Metrics) -> None:
    app, publisher, _ = make_app(config, metrics, rpc=RpcState(RpcAddrs((), ()), SKIP_EMPTY, "holds no usable address"))

    outcome = app.run_round()

    assert outcome.result == PARTIAL
    assert [kind for kind, _ in publisher.published] == ["src", "tracker"]
    for kind in RPC_KINDS:
        assert metrics.get("egress_sync_skipped_total", kind=kind, reason=SKIP_EMPTY) == 1
        # The distinct reason is the whole point: "empty" and "missing" name
        # different owners, so they must not collapse into one series.
        assert metrics.get("egress_sync_skipped_total", kind=kind, reason=SKIP_MISSING) == 0


def test_a_skipped_kind_keeps_its_shrink_baseline(config: Config, metrics: Metrics) -> None:
    """A skipped round must not erase what the RPC set looked like when it last published."""
    app, publisher, _ = make_app(config, metrics)
    assert app.run_round().result == PUBLISHED
    assert app._published_counts["rpc-ip"] == 2

    app._rpc = StubRpcSource(RpcState(RpcAddrs((), ()), SKIP_MISSING))
    publisher.published.clear()

    assert app.run_round().result == PARTIAL
    # Baseline intact: when the file comes back with fewer addresses than before,
    # the shrink gate still has something to compare against.
    assert app._published_counts == {"src": 2, "tracker": 2, "rpc-domain": 1, "rpc-ip": 2}


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


def test_dry_run_reports_partial_when_a_source_was_skipped(config: Config, metrics: Metrics) -> None:
    app, publisher, refresher = make_app(config, metrics, rpc=RpcState(RpcAddrs((), ()), SKIP_MISSING))

    outcome = app.run_round(dry_run=True)

    # A dry run must not look healthier than the real round it is predicting.
    assert outcome.result == PARTIAL
    assert publisher.published == []
    assert refresher.calls == 0
    assert set(outcome.skipped) == set(RPC_KINDS)
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


def test_the_vip_is_published_as_a_source_even_though_it_is_not_a_node(
    config: Config, metrics: Metrics
) -> None:
    """kube-vip floats the control-plane VIP between nodes.

    It never shows up in `GET /api/v1/nodes`, but libtorrent binds it as its
    source address on whichever node holds it — observed in production: qbt
    announcing from the VIP, which sent those announces down the proxy path
    because the VIP was not in the source set.
    """
    from dataclasses import replace

    app, publisher, _ = make_app(replace(config, extra_source_entries=("10.0.0.99",)), metrics)

    outcome = app.run_round()

    assert outcome.result == PUBLISHED
    assert dict(publisher.published)["src"] == ("10.0.0.11", "10.0.0.12", "10.0.0.99")
