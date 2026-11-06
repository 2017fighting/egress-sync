"""The wire contract with the other two processes.

Worker: `PUT /api/egress/<kind>`, bearer token, body `{"entries": [...],
"client": "egress-sync"}`. mihomo: `PUT /providers/rules/<name>`. Both are
asserted here because changing either silently breaks the other repo.
"""

from __future__ import annotations

import json

import pytest
from egress_sync import CLIENT_NAME, KINDS
from egress_sync.derive import SKIP_EMPTY, SKIP_MISSING, SKIP_UNREADABLE
from egress_sync.sources import (
    ClusterNodeSource,
    ProviderRefresher,
    Publisher,
    QbtTrackerSource,
    RpcStateSource,
    SourceError,
)
from egress_sync.transport import HttpError, Response

from tests.conftest import FakeTransport, json_response


def test_kinds_are_the_agreed_four() -> None:
    assert KINDS == ("src", "tracker", "rpc-domain", "rpc-ip")
    assert CLIENT_NAME == "egress-sync"


@pytest.mark.parametrize("kind", KINDS)
def test_publisher_contract(kind: str) -> None:
    transport = FakeTransport(responder=lambda call: json_response({"ok": True}))
    publisher = Publisher(transport, "https://worker.example.workers.dev/", "tok")

    publisher.publish(kind, ["a.example.com", "b.example.com"])

    call = transport.calls[0]
    assert call.method == "PUT"
    assert call.url == f"https://worker.example.workers.dev/api/egress/{kind}"
    assert call.headers["Authorization"] == "Bearer tok"
    assert call.headers["Content-Type"] == "application/json"
    assert json.loads(call.body) == {"entries": ["a.example.com", "b.example.com"], "client": "egress-sync"}


def test_publisher_empty_entries_still_serialise_as_a_list() -> None:
    transport = FakeTransport(responder=lambda call: json_response({}))

    Publisher(transport, "https://w", "tok").publish("tracker", [])

    assert json.loads(transport.calls[0].body) == {"entries": [], "client": "egress-sync"}


@pytest.mark.parametrize("status", [400, 401, 422, 500])
def test_publisher_raises_on_non_2xx(status: int) -> None:
    transport = FakeTransport(responder=lambda call: Response(status, b'{"error":"rejected"}'))

    with pytest.raises(HttpError) as excinfo:
        Publisher(transport, "https://w", "tok").publish("tracker", ["a.example.com"])

    assert excinfo.value.status == status


def test_tracker_source_reads_the_maindata_dictionary() -> None:
    payload = {
        "torrents": {"hash": {}},
        "trackers": {
            "https://tracker.example.com/announce.php?passkey=a": ["hash1"],
            "https://tracker.example.com/announce.php?passkey=b": ["hash2"],
            "udp://open.stealth.si:80/announce": ["hash3"],
        },
        "trackers_removed": [],
    }
    transport = FakeTransport(responder=lambda call: json_response(payload))

    result = QbtTrackerSource(transport, "http://qbt:8082").fetch()

    assert transport.calls[0].url == "http://qbt:8082/api/v2/sync/maindata"
    assert "Authorization" not in transport.calls[0].headers
    assert result.domains == ("open.stealth.si", "tracker.example.com")


def test_tracker_source_sends_a_key_only_when_configured() -> None:
    transport = FakeTransport(responder=lambda call: json_response({"trackers": {}}))

    QbtTrackerSource(transport, "http://qbt:8082", "key").fetch()

    assert transport.calls[0].headers["Authorization"] == "Bearer key"


@pytest.mark.parametrize("payload", [{}, {"trackers": None}, {"trackers": []}, {"trackers": "x"}])
def test_tracker_source_rejects_a_payload_without_a_tracker_dictionary(payload: dict) -> None:
    transport = FakeTransport(responder=lambda call: json_response(payload))

    with pytest.raises(SourceError):
        QbtTrackerSource(transport, "http://qbt:8082").fetch()


def test_node_source_uses_the_service_account() -> None:
    payload = {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.11"}]}}]}
    transport = FakeTransport(responder=lambda call: json_response(payload))
    source = ClusterNodeSource(transport, "https://kubernetes.default.svc/", token_file=_token_file("sa-token"))

    result = source.fetch()

    assert transport.calls[0].url == "https://kubernetes.default.svc/api/v1/nodes"
    assert transport.calls[0].headers["Authorization"] == "Bearer sa-token"
    assert result.ips == ("10.0.0.11",)


def _token_file(token: str) -> str:
    import pathlib
    import tempfile

    path = pathlib.Path(tempfile.mkdtemp()) / "token"
    path.write_text(token, encoding="utf-8")
    return str(path)


def test_node_source_reports_an_unreadable_token_file() -> None:
    transport = FakeTransport()

    with pytest.raises(SourceError):
        ClusterNodeSource(transport, "https://k", token_file="/nonexistent/token").fetch()


def test_node_source_reports_an_api_error() -> None:
    transport = FakeTransport(responder=lambda call: Response(403, b'{"reason":"Forbidden"}'))

    source = ClusterNodeSource(transport, "https://k", token_file=_token_file("t"))
    with pytest.raises(HttpError):
        source.fetch()


RPC_STATE_READS = [
    # (case, file content, expected skip_reason, expected addresses)
    ("valid content", "::ffff:1.2.3.4\n5.6.7.8\n", "", ("1.2.3.4", "5.6.7.8")),
    ("empty file", "", SKIP_EMPTY, ()),
    ("only comments", "# pushed 2026-11-06\n# 5 addresses\n", SKIP_EMPTY, ()),
    ("only unparseable lines", "not-an-ip\n999.1.1.1\n", SKIP_EMPTY, ()),
    ("valid plus garbage stays usable", "1.2.3.4\nnot-an-ip\n", "", ("1.2.3.4",)),
]


@pytest.mark.parametrize("case,text,expected_skip,expected_ips", RPC_STATE_READS)
def test_rpc_state_source_classifies_one_read(
    case: str, text: str, expected_skip: str, expected_ips: tuple[str, ...]
) -> None:
    result = RpcStateSource("/state/rpc.txt", read_text=lambda path: text).fetch()

    assert result.skip_reason == expected_skip, case
    assert result.addrs.ips == expected_ips, case
    assert result.usable is (expected_skip == ""), case


def test_rpc_state_source_reports_a_missing_file_as_a_skip() -> None:
    # home-ops#196: no exception — the round decides what to do with it.
    source = RpcStateSource("/state/rpc.txt", read_text=lambda path: (_ for _ in ()).throw(FileNotFoundError("gone")))

    state = source.fetch()

    assert state.skip_reason == SKIP_MISSING
    assert state.addrs.ips == ()
    assert "does not exist" in state.detail


def test_rpc_state_source_keeps_unreadable_distinct_from_missing() -> None:
    source = RpcStateSource(
        "/state/rpc.txt",
        read_text=lambda path: (_ for _ in ()).throw(PermissionError("denied")),
    )

    state = source.fetch()

    # A broken mount/permission needs a human; a not-yet-written file is #195 in
    # flight. Same round behaviour, different series.
    assert state.skip_reason == SKIP_UNREADABLE
    assert "unreadable" in state.detail


def test_refresher_uses_configured_provider_names() -> None:
    transport = FakeTransport(responder=lambda call: Response(204, b""))
    refresher = ProviderRefresher(transport, "http://mihomo:9090/", "sec", ["egress-tracker", "egress-src"])

    assert refresher.refresh() == ("egress-tracker", "egress-src")
    assert [(c.method, c.url) for c in transport.calls] == [
        ("PUT", "http://mihomo:9090/providers/rules/egress-tracker"),
        ("PUT", "http://mihomo:9090/providers/rules/egress-src"),
    ]
    assert transport.calls[0].headers["Authorization"] == "Bearer sec"


def test_refresher_discovers_providers_when_none_are_configured() -> None:
    transport = FakeTransport(
        responder=lambda call: (
            json_response({"providers": {"egress-src": {}, "egress-tracker": {}}})
            if call.method == "GET"
            else Response(204, b"")
        )
    )

    refreshed = ProviderRefresher(transport, "http://mihomo:9090", "").refresh()

    assert refreshed == ("egress-src", "egress-tracker")
    assert [c.method for c in transport.calls] == ["GET", "PUT", "PUT"]
    assert "Authorization" not in transport.calls[0].headers


def test_refresher_is_disabled_without_a_controller_url() -> None:
    refresher = ProviderRefresher(FakeTransport(), "", "sec")

    assert refresher.enabled is False
    assert refresher.refresh() == ()


def test_refresher_propagates_controller_errors() -> None:
    transport = FakeTransport(responder=lambda call: Response(503, b'{"message":"refresh failed"}'))

    with pytest.raises(HttpError):
        ProviderRefresher(transport, "http://mihomo:9090", "s", ["p"]).refresh()
