"""Pure derivation: raw upstream text in, rule-set entries out.

Nothing here touches the network, the filesystem or the clock, which is what
makes every case in tests/test_derive.py a table row instead of a fixture.

The three sources and why they are read the way they are
(`docs/research/egress-split/0{1,2}-*.md` in the home-ops repo):

- trackers: `sync/maindata`'s `.trackers` dictionary is the only complete view
  of announce URLs (`torrents/info`'s single `tracker` field misses ~20%).
- RPC addresses: the hath fork writes a state file, because the server-pushed
  list never lands on disk anywhere else.
- nodes: the cluster API, so the source set is not maintained by hand.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# `scheme://` including the `udp://` and `ws://` trackers actually in the wild.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

# qBittorrent's sticky entries (`** [DHT] **`, `** [PeX] **`, `** [LSD] **`).
# They only come from `torrents/trackers?hash=`, never from `sync/maindata`, but
# they would poison the domain set if they ever did.
_QBT_PSEUDO = "**"

# A DNS label: 1-63 chars, no leading/trailing hyphen. Underscore is allowed
# because real announce hosts occasionally carry one (`_` is invalid in DNS but
# appears in the wild and is not our business to reject).
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")

_IPV4_MAPPED = "::ffff:"


@dataclass(frozen=True)
class TrackerHosts:
    """`.trackers` keys folded down to hosts, split by what a rule can express."""

    domains: tuple[str, ...]
    # Current data has 0 of these (all 1861 announce URLs are domains), but the
    # branch stays: `http://1.2.3.4:2710/announce` is a real historical shape and
    # dropping it silently would be invisible.
    ip_literals: tuple[str, ...]
    # Raw URLs that produced neither. Non-empty blocks the whole round.
    invalid: tuple[str, ...]


@dataclass(frozen=True)
class RpcAddrs:
    ips: tuple[str, ...]
    invalid: tuple[str, ...]


@dataclass(frozen=True)
class NodeIPs:
    ips: tuple[str, ...]
    invalid: tuple[str, ...]


def normalize_ip(token: object) -> str | None:
    """Canonicalise one address token, or None if it is not an address.

    Handles the two shapes that actually occur: the `::ffff:` prefixed form the
    H@H server pushes (`rpc_server_ip=::ffff:37.48.81.219;...`) and bracketed
    IPv6 literals from announce URLs.
    """
    text = str(token or "").strip()
    if not text:
        return None
    if text.lower().startswith(_IPV4_MAPPED):
        text = text[len(_IPV4_MAPPED) :]
    text = text.strip("[]")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def is_valid_domain(value: object) -> bool:
    """A domain a `behavior: domain` rule-set can carry, and nothing else."""
    text = str(value or "").strip().lower().rstrip(".")
    if not text or len(text) > 253 or "." not in text:
        return False
    if normalize_ip(text) is not None:
        # `1.2.3.4` is an address, not a domain; the ip kinds carry those.
        return False
    labels = text.split(".")
    if not _LABEL_RE.match(labels[-1]) or not any(c.isalpha() for c in labels[-1]):
        return False
    return all(_LABEL_RE.match(label) for label in labels)


def host_from_announce_url(url: object) -> str | None:
    """Announce URL -> host, dropping scheme, userinfo, port and path/passkey.

    The passkey is unexpressible in a rule and impossible to normalise away: it
    turns up as `?passkey=`, as `?secure=` and as a bare path segment. All three
    disappear with the path.
    """
    text = str(url or "").strip()
    if not text or text.startswith(_QBT_PSEUDO):
        return None
    text = _SCHEME_RE.sub("", text, count=1)
    authority = re.split(r"[/?#]", text, maxsplit=1)[0]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    # `[2001:db8::1]:8080` is one address with a port; `host:port` is the usual form.
    host = authority[1:].split("]", 1)[0] if authority.startswith("[") else authority.split(":", 1)[0]
    host = host.strip().lower().rstrip(".")
    return host or None


def _sorted_ips(ips: Iterable[str]) -> tuple[str, ...]:
    """Deterministic, human-sane order: IPv4 before IPv6, then numerically."""
    return tuple(sorted(ips, key=lambda s: (ipaddress.ip_address(s).version, int(ipaddress.ip_address(s)))))


def derive_tracker_hosts(announce_urls: Iterable[object]) -> TrackerHosts:
    """Fold every announce URL down to a unique host, then split by kind."""
    domains: set[str] = set()
    ip_literals: set[str] = set()
    invalid: set[str] = set()

    for url in announce_urls:
        raw = str(url or "").strip()
        if not raw or raw.startswith(_QBT_PSEUDO):
            continue
        host = host_from_announce_url(raw)
        ip = normalize_ip(host) if host else None
        if ip is not None:
            ip_literals.add(ip)
            continue
        if host is not None and is_valid_domain(host):
            domains.add(host)
            continue
        invalid.add(raw)

    return TrackerHosts(
        domains=tuple(sorted(domains)),
        ip_literals=_sorted_ips(ip_literals),
        invalid=tuple(sorted(invalid)),
    )


def parse_rpc_state(text: str) -> RpcAddrs:
    """Parse the hath state file: one IPv4 per line, LF-terminated.

    Deliberately more tolerant than the writer: `;` is accepted as a separator
    too (that is the raw `rpc_server_ip=` shape, so a hand-pasted value parses
    the same way), blank lines and `#` comments are ignored, and the `::ffff:`
    prefix is stripped so a pre-normalisation dump still works.
    """
    ips: set[str] = set()
    invalid: set[str] = set()

    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for token in line.split(";"):
            token = token.strip()
            if not token:
                continue
            ip = normalize_ip(token)
            if ip is None:
                invalid.add(token)
            else:
                ips.add(ip)

    return RpcAddrs(ips=_sorted_ips(ips), invalid=tuple(sorted(invalid)))


def derive_node_ips(payload: bytes | str | Mapping[str, object]) -> NodeIPs:
    """`GET /api/v1/nodes` -> the nodes' InternalIPs.

    Only InternalIP: the point of the source set is "traffic that leaves from a
    cluster node", and the external addresses say nothing about that.
    """
    if isinstance(payload, (bytes, bytearray)):
        data = json.loads(payload.decode("utf-8"))
    elif isinstance(payload, str):
        data = json.loads(payload)
    else:
        data = payload

    if not isinstance(data, Mapping):
        raise ValueError("nodes payload is not a JSON object")

    ips: set[str] = set()
    invalid: set[str] = set()

    items = data.get("items") or []
    if not isinstance(items, list):
        raise ValueError("nodes payload has no items list")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status") or {}
        if not isinstance(status, Mapping):
            continue
        for address in status.get("addresses") or []:
            if not isinstance(address, Mapping) or address.get("type") != "InternalIP":
                continue
            ip = normalize_ip(address.get("address"))
            if ip is None:
                invalid.add(str(address.get("address")))
            else:
                ips.add(ip)

    return NodeIPs(ips=_sorted_ips(ips), invalid=tuple(sorted(invalid)))
