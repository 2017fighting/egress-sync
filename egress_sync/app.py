"""The round: derive -> gate -> publish -> refresh, and the classification of it.

A round is all-or-nothing. The four kinds are published in `KINDS` order; if the
gate rejects any of them, none are sent. If a publish call fails midway the
remaining kinds are abandoned (also recorded) rather than half-updated — the
worker stores per-kind sets, so a partial round is exactly the "half a rule-set"
state the spec forbids.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field

from . import KINDS, __version__
from .config import Config
from .derive import is_valid_domain
from .gate import Payload, evaluate
from .metrics import RESULTS, Metrics
from .sources import (
    ClusterNodeSource,
    ProviderRefresher,
    Publisher,
    QbtTrackerSource,
    RpcStateSource,
    SourceError,
)
from .transport import HttpError, Transport

log = logging.getLogger("egress_sync")

PUBLISHED = "published"
# Published, but not everything a round could have published: one source said
# there was nothing to derive this round (`home-ops#196` — the hath state file
# is missing, unreadable or empty). The kinds that WERE derived went out, so this
# is a success for them and an incident for the skipped pair, which is why it is
# neither `published` nor an error class.
PARTIAL = "partial"
GATE_REJECTED = "gate_rejected"
SOURCE_ERROR = "source_error"
PUBLISH_ERROR = "publish_error"

# The kinds that come from the hath state file. Skipped as a pair on purpose:
# they are one destination set (the RPC channel), and publishing the domain with
# no address set beside it would be the "half a rule-set" state the spec forbids
# — the sets are only meaningful together.
RPC_KINDS: tuple[str, ...] = ("rpc-domain", "rpc-ip")


@dataclass(frozen=True)
class Derived:
    """One round's derivation: what to publish, and what a source skipped."""

    payloads: tuple[Payload, ...]
    # kind -> reason, for the kinds whose source deliberately produced nothing.
    skipped: dict[str, str] = field(default_factory=dict)
    # Tokens the state file carried that could not be normalised. Reported even
    # when the kind is skipped, so "the file is a mess" stays visible.
    rpc_invalid: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoundOutcome:
    result: str
    detail: str = ""
    entries: dict[str, int] = field(default_factory=dict)
    # True when the round's derived sets were computed but not sent (`--dry-run`).
    dry_run: bool = False
    # Kinds this round skipped, by reason. Empty on a full publish.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        # Deliberately strict: `--once` exits 0 only for a complete publish, so a
        # skipped RPC pair is not mistaken for a healthy release check.
        return self.result == PUBLISHED


class EgressSync:
    def __init__(
        self,
        config: Config,
        *,
        transport: Transport,
        metrics: Metrics | None = None,
        node_source: ClusterNodeSource | None = None,
        tracker_source: QbtTrackerSource | None = None,
        rpc_source: RpcStateSource | None = None,
        publisher: Publisher | None = None,
        refresher: ProviderRefresher | None = None,
    ) -> None:
        self.config = config
        self.metrics = metrics or Metrics(__version__)
        self._nodes = node_source or ClusterNodeSource(transport, config.kube_api_url, config.kube_sa_token_file)
        self._trackers = tracker_source or QbtTrackerSource(transport, config.qbt_url, config.qbt_api_key)
        self._rpc = rpc_source or RpcStateSource(config.rpc_state_file)
        self._publisher = publisher or Publisher(transport, config.worker_url, config.worker_token)
        self._refresher = refresher or ProviderRefresher(
            transport, config.mihomo_controller_url, config.mihomo_secret, config.mihomo_providers
        )
        # Last *published* entry count per kind — the baseline for the shrink
        # gate. In memory on purpose: after a restart there is no baseline, which
        # costs one round of protection and saves a shared-state dependency.
        self._published_counts: dict[str, int] = {}
        self._tracker_ip_literals: tuple[str, ...] = ()

    # -- one round -------------------------------------------------------
    def run_round(self, *, dry_run: bool = False) -> RoundOutcome:
        try:
            derived = self._derive()
        except SourceError as exc:
            return self._finish(SOURCE_ERROR, str(exc))
        except HttpError as exc:
            return self._finish(SOURCE_ERROR, f"upstream call failed: {exc}")
        except Exception as exc:
            # The resident form exists so failures land in metrics; an uncaught
            # exception would take /metrics down with it and hide the cause.
            log.exception("unexpected error while deriving")
            return self._finish(SOURCE_ERROR, f"unexpected error while deriving: {exc!r}")

        payloads = list(derived.payloads)
        for payload in payloads:
            self.metrics.set("egress_sync_derived_entries", len(payload.entries), kind=payload.kind)
            self.metrics.set("egress_sync_invalid_entries", len(payload.invalid), kind=payload.kind)
        for kind, reason in derived.skipped.items():
            # A skipped kind derived nothing, and its previous version stays live
            # (so `egress_sync_entries{kind}` keeps the old count: that is the point).
            self.metrics.set("egress_sync_derived_entries", 0, kind=kind)
            self.metrics.inc("egress_sync_skipped_total", kind=kind, reason=reason)
        if "rpc-ip" in derived.skipped:
            self.metrics.set("egress_sync_invalid_entries", len(derived.rpc_invalid), kind="rpc-ip")
        self.metrics.set("egress_sync_tracker_ip_literals", len(self._tracker_ip_literals))

        verdict = evaluate(payloads, self._published_counts, self.config.min_entry_ratio, skipped=derived.skipped)
        if not verdict.ok:
            for rejection in verdict.rejections:
                self.metrics.inc("egress_sync_publish_rejected_total", kind=rejection.kind, reason=rejection.reason)
            return self._finish(GATE_REJECTED, verdict.summary, payloads, skipped=derived.skipped)

        # One classification for both paths: a round that skipped the RPC pair is
        # `partial` whether or not it actually published, so a dry run cannot look
        # healthier than the real thing.
        result = PARTIAL if derived.skipped else PUBLISHED

        if dry_run:
            log.info("dry run: gate passed for %s; nothing published", ", ".join(p.kind for p in payloads))
            return self._finish(
                result, "dry run: derived and gated, not published", payloads, dry_run=True, skipped=derived.skipped
            )

        published: list[str] = []
        for payload in payloads:
            try:
                self._publisher.publish(payload.kind, payload.entries)
            except Exception as exc:
                self.metrics.inc("egress_sync_publish_failures_total", kind=payload.kind)
                detail = f"publish failed at {payload.kind}: {exc}"
                if published:
                    detail += f" (already published: {', '.join(published)})"
                return self._finish(PUBLISH_ERROR, detail, payloads, skipped=derived.skipped)
            published.append(payload.kind)

        for payload in payloads:
            self.metrics.set("egress_sync_entries", len(payload.entries), kind=payload.kind)
            self._published_counts[payload.kind] = len(payload.entries)
        detail = "published " + ", ".join(published)
        if derived.skipped:
            detail += "; skipped " + ", ".join(f"{kind} ({reason})" for kind, reason in derived.skipped.items())
        outcome = self._finish(result, detail, payloads, skipped=derived.skipped)
        self._refresh()
        return outcome

    # -- pieces ----------------------------------------------------------
    def _derive(self) -> Derived:
        nodes = self._nodes.fetch()
        trackers = self._trackers.fetch()
        rpc = self._rpc.fetch()
        self._tracker_ip_literals = trackers.ip_literals
        if trackers.ip_literals:
            # No publish kind carries a tracker's bare IP today; the set exists so
            # the loss is counted instead of invisible (home-ops#191 "IP 字面量 0 条").
            log.warning(
                "tracker IP literals present but not publishable as a rule: %s", ", ".join(trackers.ip_literals)
            )
        payloads = [
            Payload("src", nodes.ips, nodes.invalid),
            Payload("tracker", trackers.domains, trackers.invalid),
        ]
        skipped: dict[str, str] = {}
        if rpc.usable:
            payloads.append(Payload("rpc-domain", (self.config.rpc_domain,)))
            payloads.append(Payload("rpc-ip", rpc.addrs.ips, rpc.addrs.invalid))
        else:
            # home-ops#196: the state file belongs to another app, so its absence
            # must not stop the two sets that are still derivable. Everything goes
            # out as usual except the RPC pair, which keeps its previous revision.
            skipped = dict.fromkeys(RPC_KINDS, rpc.skip_reason)
            log.warning(
                "%s [%s]: skipping %s this round; publishing the rest",
                rpc.detail,
                rpc.skip_reason,
                ", ".join(RPC_KINDS),
            )
        return Derived(payloads=tuple(payloads), skipped=skipped, rpc_invalid=rpc.addrs.invalid)

    def _refresh(self) -> None:
        if not self._refresher.enabled:
            return
        try:
            names = self._refresher.refresh()
        except Exception as exc:
            # Only a metric and a log line: the publish already stands, and the
            # provider's own refresh cycle limits the damage to one interval.
            self.metrics.inc("egress_sync_refresh_failures_total")
            log.warning("provider refresh failed: %s", exc)
            return
        self.metrics.set("egress_sync_last_refresh_success_timestamp_seconds", time.time())
        log.info("refreshed rule providers: %s", ", ".join(names) or "(none)")

    def _finish(
        self,
        result: str,
        detail: str = "",
        payloads: list[Payload] | None = None,
        *,
        dry_run: bool = False,
        skipped: dict[str, str] | None = None,
    ) -> RoundOutcome:
        now = time.time()
        self.metrics.inc("egress_sync_rounds_total", result=result)
        self.metrics.set_one_hot("egress_sync_last_round_result", result, RESULTS, "result")
        self.metrics.set("egress_sync_last_round_timestamp_seconds", now)
        # `partial` moves last_success: the publishes that were made all succeeded,
        # and the skipped pair is a separate signal (`egress_sync_skipped_total` +
        # `last_round_result{result="partial"}`). Keeping this gauge at "anything
        # published" is what lets the "has stopped publishing" alert stay about
        # the whole pipeline instead of firing forever on one missing file.
        if result in (PUBLISHED, PARTIAL) and not dry_run:
            self.metrics.set("egress_sync_last_success_timestamp_seconds", now)
        counts = {p.kind: len(p.entries) for p in payloads} if payloads else {}
        level = logging.INFO if result == PUBLISHED else logging.WARNING
        log.log(level, "round %s: %s", result, detail or "ok")
        return RoundOutcome(result=result, detail=detail, entries=counts, dry_run=dry_run, skipped=dict(skipped or {}))


def build_from_config(config: Config, transport: Transport) -> EgressSync:
    """Wire a process; the only place that touches the environment."""
    metrics = Metrics(__version__)
    return EgressSync(config, transport=transport, metrics=metrics)


def assert_config(config: Config) -> None:
    """Fail at startup on a configuration the gate would reject every round."""
    if not is_valid_domain(config.rpc_domain):
        raise ValueError(f"RPC_DOMAIN is not a valid domain: {config.rpc_domain!r}")
    if tuple(config.mihomo_providers) and not config.mihomo_controller_url:
        log.warning("MIHOMO_PROVIDERS is set but MIHOMO_CONTROLLER_URL is empty; refresh is disabled")
    if not config.mihomo_controller_url:
        log.warning("MIHOMO_CONTROLLER_URL is empty; publishes will not trigger a provider refresh")


def config_summary(config: Config) -> dict[str, object]:
    """Redacted view for the start-up log: never print the token."""
    summary = dataclasses.asdict(config)
    summary["worker_token"] = "***" if config.worker_token else ""
    summary["mihomo_secret"] = "***" if config.mihomo_secret else ""
    summary["qbt_api_key"] = "***" if config.qbt_api_key else ""
    summary["mihomo_providers"] = list(config.mihomo_providers)
    return summary


__all__ = [
    "GATE_REJECTED",
    "KINDS",
    "PARTIAL",
    "PUBLISHED",
    "PUBLISH_ERROR",
    "RPC_KINDS",
    "SOURCE_ERROR",
    "Derived",
    "EgressSync",
    "RoundOutcome",
]
