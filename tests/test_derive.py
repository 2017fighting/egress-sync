"""Table-driven derivation tests.

Each table row is one behaviour the spec calls out: passkey removal, explicit
ports, `udp://` entries, `::ffff:` normalisation, `;` separated input, empty
items. Inputs are the raw upstream text; assertions are on the derived sets.
"""

from __future__ import annotations

import pytest
from egress_sync.derive import (
    SKIP_EMPTY,
    SKIP_MISSING,
    classify_rpc_state,
    derive_node_ips,
    derive_tracker_hosts,
    host_from_announce_url,
    is_valid_domain,
    normalize_ip,
    parse_rpc_state,
)

HOST_CASES = [
    # (announce URL, expected host)
    ("https://tracker.example.com/announce.php?passkey=abc123", "tracker.example.com"),
    ("https://tracker.example.com/8d079574abc/announce", "tracker.example.com"),
    ("https://tracker.example.com/announce.php?secure=xyz", "tracker.example.com"),
    ("http://tracker.example.com:2710/announce", "tracker.example.com"),
    ("https://tracker.example.com:443/announce", "tracker.example.com"),
    ("udp://tracker.opentrackr.org:1337/announce", "tracker.opentrackr.org"),
    ("udp://open.stealth.si:80/announce", "open.stealth.si"),
    ("http://user:pass@tracker.example.com/announce", "tracker.example.com"),
    ("http://[2001:db8::1]:8080/announce", "2001:db8::1"),
    ("http://1.2.3.4:2710/announce", "1.2.3.4"),
    ("HTTPS://Tracker.Example.COM/Announce", "tracker.example.com"),
    ("https://tracker.example.com./announce", "tracker.example.com"),
    ("tracker.example.com/announce", "tracker.example.com"),
    ("https://tracker.example.com", "tracker.example.com"),
    # qBittorrent's sticky pseudo-entries are not announce URLs.
    ("** [DHT] **", None),
    ("** [PeX] **", None),
    ("", None),
    ("   ", None),
    ("https://", None),
]


@pytest.mark.parametrize("url,expected", HOST_CASES)
def test_host_from_announce_url(url: str, expected: str | None) -> None:
    assert host_from_announce_url(url) == expected


IP_CASES = [
    ("10.0.0.1", "10.0.0.1"),
    ("::ffff:37.48.81.219", "37.48.81.219"),
    ("::FFFF:212.7.200.99", "212.7.200.99"),
    ("[2001:db8::1]", "2001:db8::1"),
    ("[::ffff:5.79.104.109]", "5.79.104.109"),
    (" 1.2.3.4 ", "1.2.3.4"),
    ("1.2.3.4/32", None),
    ("999.1.1.1", None),
    ("tracker.example.com", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize("token,expected", IP_CASES)
def test_normalize_ip(token: object, expected: str | None) -> None:
    assert normalize_ip(token) == expected


DOMAIN_CASES = [
    ("tracker.example.com", True),
    ("a.x-anime.cc", True),
    ("tracker.ipv6tracker.org", True),
    ("tracker810.xyz", True),
    ("_dmhy.example.com", True),
    ("Tracker.Example.COM", True),
    ("tracker.example.com.", True),
    ("localhost", False),
    ("", False),
    ("1.2.3.4", False),
    ("-bad.example.com", False),
    ("bad-.example.com", False),
    ("has space.example.com", False),
    ("https://tracker.example.com", False),
    ("tracker.example.com:8080", False),
    ("example.123", False),
]


@pytest.mark.parametrize("value,expected", DOMAIN_CASES)
def test_is_valid_domain(value: str, expected: bool) -> None:
    assert is_valid_domain(value) is expected


def test_derive_tracker_hosts_dedupes_and_splits() -> None:
    urls = [
        "https://tracker.example.com/announce.php?passkey=a",
        "https://tracker.example.com/announce.php?passkey=b",
        "http://tracker.example.com:2710/announce",
        "udp://open.stealth.si:80/announce",
        "http://1.2.3.4:2710/announce",
        "** [DHT] **",
    ]
    result = derive_tracker_hosts(urls)

    assert result.domains == ("open.stealth.si", "tracker.example.com")
    assert result.ip_literals == ("1.2.3.4",)
    assert result.invalid == ()


def test_derive_tracker_hosts_reports_unusable_urls() -> None:
    result = derive_tracker_hosts(["https://ok.example.com/announce", "not a url at all"])

    assert result.domains == ("ok.example.com",)
    assert result.invalid == ("not a url at all",)


def test_derive_tracker_hosts_empty_input() -> None:
    result = derive_tracker_hosts([])

    assert result == type(result)(domains=(), ip_literals=(), invalid=())


RPC_STATE_CASES = [
    # (file content, expected ips, expected invalid)
    ("1.2.3.4\n5.6.7.8\n", ("1.2.3.4", "5.6.7.8"), ()),
    # The writer normalises, the reader tolerates the pre-normalised form.
    ("::ffff:37.48.81.219\n::ffff:212.7.200.99\n", ("37.48.81.219", "212.7.200.99"), ()),
    # Raw `rpc_server_ip=` shape, semicolon separated, in one line.
    # Order is numeric, not file order, so the published set is deterministic.
    ("::ffff:37.48.81.219;::ffff:5.79.104.109\n", ("5.79.104.109", "37.48.81.219"), ()),
    # Blank lines, comments, CRLF and duplicates are not entries.
    ("# pushed 2026-11-06\n\n1.2.3.4\r\n1.2.3.4\n   \n", ("1.2.3.4",), ()),
    ("1.2.3.4 # inline comment\n", ("1.2.3.4",), ()),
    ("\n\n\n", (), ()),
    ("", (), ()),
    # Garbage is reported, not silently dropped: the gate turns it into a refusal.
    ("1.2.3.4\nnot-an-ip\n", ("1.2.3.4",), ("not-an-ip",)),
]


@pytest.mark.parametrize("text,expected_ips,expected_invalid", RPC_STATE_CASES)
def test_parse_rpc_state(text: str, expected_ips: tuple[str, ...], expected_invalid: tuple[str, ...]) -> None:
    result = parse_rpc_state(text)

    assert result.ips == expected_ips
    assert result.invalid == expected_invalid


CLASSIFY_CASES = [
    # (case, state-file read (None = the file does not exist), skip_reason, ips, invalid)
    ("missing file", None, SKIP_MISSING, (), ()),
    ("empty file", "", SKIP_EMPTY, (), ()),
    ("whitespace only", "\n \n\t\n", SKIP_EMPTY, (), ()),
    ("only comments", "# pushed 2026-11-06\n# 5 addresses\n", SKIP_EMPTY, (), ()),
    # Unparseable lines are "empty" (no usable address) and still reported, so the
    # log/metric can name them instead of the round failing outright.
    ("only unparseable lines", "not-an-ip\n999.1.1.1\n", SKIP_EMPTY, (), ("999.1.1.1", "not-an-ip")),
    # The real payload shape: one normalised address per line, LF terminated.
    (
        "valid content",
        "37.48.81.219\n37.48.81.200\n212.7.200.99\n212.7.202.50\n5.79.104.109\n",
        "",
        ("5.79.104.109", "37.48.81.200", "37.48.81.219", "212.7.200.99", "212.7.202.50"),
        (),
    ),
    # One bad line does not hide the good ones: the kind stays usable and the gate
    # turns the garbage into a refusal ("not silently dropped").
    ("valid plus garbage", "1.2.3.4\nnot-an-ip\n", "", ("1.2.3.4",), ("not-an-ip",)),
]


@pytest.mark.parametrize("case,text,expected_skip,expected_ips,expected_invalid", CLASSIFY_CASES)
def test_classify_rpc_state(
    case: str,
    text: str | None,
    expected_skip: str,
    expected_ips: tuple[str, ...],
    expected_invalid: tuple[str, ...],
) -> None:
    state = classify_rpc_state(text)

    assert state.skip_reason == expected_skip, case
    assert state.addrs.ips == expected_ips, case
    assert state.addrs.invalid == expected_invalid, case
    assert state.usable is (expected_skip == ""), case


def test_classify_rpc_state_explains_an_empty_file() -> None:
    # The detail is what the log line and the alert description quote, so it has to
    # name what was wrong rather than repeat "no addresses".
    state = classify_rpc_state("not-an-ip\n")

    assert "no usable address" in state.detail
    assert "not-an-ip" in state.detail


def test_classify_rpc_state_is_usable_unchanged_by_order() -> None:
    # Same content in a different order is the same set (the file is re-written by
    # the server push, and a reordered push must not look like a change).
    one = classify_rpc_state("1.2.3.4\n5.6.7.8\n")
    two = classify_rpc_state("5.6.7.8\n1.2.3.4\n")

    assert one == two


NODES_PAYLOAD = {
    "items": [
        {
            "metadata": {"name": "cp-1"},
            "status": {
                "addresses": [
                    {"type": "InternalIP", "address": "192.168.9.11"},
                    {"type": "Hostname", "address": "cp-1"},
                    {"type": "InternalIP", "address": "192.168.9.12"},
                ]
            },
        },
        {
            "metadata": {"name": "cp-2"},
            "status": {"addresses": [{"type": "InternalIP", "address": "::ffff:192.168.9.12"}]},
        },
        {"metadata": {"name": "cp-3"}, "status": {}},
    ]
}


def test_derive_node_ips_keeps_internal_ips_only() -> None:
    result = derive_node_ips(NODES_PAYLOAD)

    # Hostname is not a source address, and the mapped/duplicate form folds in.
    assert result.ips == ("192.168.9.11", "192.168.9.12")
    assert result.invalid == ()


def test_derive_node_ips_accepts_raw_bytes() -> None:
    import json

    result = derive_node_ips(json.dumps(NODES_PAYLOAD).encode("utf-8"))

    assert result.ips == ("192.168.9.11", "192.168.9.12")


def test_derive_node_ips_reports_garbage_addresses() -> None:
    payload = {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "not-an-ip"}]}}]}

    assert derive_node_ips(payload).invalid == ("not-an-ip",)


@pytest.mark.parametrize("payload", [b"[]", b"null", b'{"items": 3}', b"not json"])
def test_derive_node_ips_rejects_unusable_payloads(payload: bytes) -> None:
    with pytest.raises(ValueError):
        derive_node_ips(payload)
