"""egress-sync: derive the mihomo "direct egress" sets from runtime state.

One resident process, one round per hour:

    cluster API  ──InternalIPs──┐
    qbt maindata ──tracker hosts┤── derive ── gate ── publish ── refresh provider
    hath state   ──RPC addrs────┘                      │
                                                       └─ /metrics

The four kinds are fixed by the worker-side contract (ADR-0024, home-ops#192):
`PUT /api/egress/<kind>` with `{"entries": [...], "client": "egress-sync"}`.

A round that cannot read the hath state file — missing, unreadable or empty —
SKIPS the two `rpc-*` kinds and publishes `src`/`tracker` as usual (home-ops#196).
An empty RPC set is never published: the worker keeps the previous revision.
"""

__version__ = "0.2.2"

# Publish order is also the order the worker's rule-sets are fed, and it is
# fixed here so a test can assert the contract without importing the app.
KINDS: tuple[str, ...] = ("src", "tracker", "rpc-domain", "rpc-ip")

# The name this program identifies itself with in the publish body. Part of the
# wire contract; deliberately not configurable.
CLIENT_NAME = "egress-sync"
