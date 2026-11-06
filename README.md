# egress-sync

Derives the four mihomo **direct-egress** rule-sets from runtime state, publishes
them to the edge worker, and asks mihomo to re-pull them.

It exists because "which machine may bypass the proxy" was encoded in the router
as *machine identity* (a MAC skip), which pinned three apps to one node and lived
in a place mihomo's rule language cannot reach. The replacement criterion is
`source ∈ cluster nodes AND destination ∈ derived set → DIRECT`, and this process
owns the "derive + publish + trigger refresh" half of it. The decisions are in
`home-ops` `docs/adr/0024-egress-directness-by-source-and-destination.md`; the
execution slices are `home-ops#194` (this program) and `home-ops#196` (the hath
half, same binary).

Nothing here is a timer/cron job on purpose: a resident process is what makes
"last success 4 hours ago" a metric instead of a surprise.

## What it derives

| kind | source | rule shape |
|---|---|---|
| `src` | `GET /api/v1/nodes` (ServiceAccount token) → `status.addresses[type=InternalIP]` | address set |
| `tracker` | `GET <qbt>/api/v2/sync/maindata` → keys of `.trackers` → host | domain set |
| `rpc-domain` | static `RPC_DOMAIN` (default `rpc.hentaiathome.net`) | domain set |
| `rpc-ip` | hath's RPC state file, one normalised IPv4 per line | address set |

Why these sources, in one line each (evidence in
`home-ops/docs/research/egress-split/0{1,2}-*.md`):

- **`.trackers`, not `torrents/info`.** `torrents/info`'s single `tracker` field
  is libtorrent's *currently used* tracker and misses ~20% of the set
  (1478 distinct vs 1861). The `.trackers` dictionary of `sync/maindata` carries
  every announce URL of every torrent, and is the cheap view (633 KB of a 16 MB
  response). Resume data on disk is the wrong source in this deployment: it is
  SQLite, and `BT_backup/` is a pre-migration snapshot with a 38.5% hit rate.
- **Host, not URL.** A passkey is unexpressible in a rule and appears in three
  shapes (`?passkey=`, `?secure=`, a bare path segment) — all of them vanish with
  the path, as do scheme and port. `udp://` entries are kept: under the current
  port whitelist they are dead entries that cost nothing, and they start working
  the day the whitelist widens.
- **The hath state file, not logs, not DNS.** The H@H server pushes a list of
  bare IPv4s (`::ffff:`-prefixed, semicolon separated) that never lands on disk;
  the only other trace is a debug line in a log that rotates in ~3 hours. The
  fork (`2017fighting/hath-rust`) writes the normalised list to a state file.
  DNS is not a substitute: `rpc.hentaiathome.net` resolves to 3 of the 5
  addresses it actually uses, so "resolved addresses" silently drops two.
- **InternalIP only.** The source set is "traffic leaving from a cluster node";
  external addresses say nothing about that, and 25 of 62 tracker hosts are also
  the PT site itself, so the set must not widen to the VLAN.

## The wire contract (fixed, do not change unilaterally)

```
PUT <WORKER_URL>/api/egress/<kind>
Authorization: Bearer <WORKER_TOKEN>
Content-Type: application/json

{"entries": ["...", "..."], "client": "egress-sync"}
```

- `kind` is one of `src`, `tracker`, `rpc-domain`, `rpc-ip`, published in that
  order. Any 2xx is success; the whole set is replaced server-side.
- **Entries are bare, normalised tokens** — a domain is `tracker.example.com`
  (lowercase, no `+.`, no scheme, no port, no dot-root), an address is
  `10.0.0.11` (no `/32`). Presentation (`+.host`, `host/32`, rule-set YAML) is the
  worker's job.
- Refresh, after a successful publish:

```
PUT <MIHOMO_CONTROLLER_URL>/providers/rules/<provider>     # Authorization: Bearer <MIHOMO_SECRET>
```

`PUT /providers/rules/{name}` is mihomo's "refresh this rule provider now"; with
`MIHOMO_PROVIDERS` empty the provider names are discovered from
`GET /providers/rules` first, so renaming a rule-set does not leave it stale.

## Environment

Everything is configured through the environment; this table **is** the contract
with the deploying repo (its HelmRelease is written from it).

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WORKER_URL` | **yes** | — | Base URL of the edge worker that stores the sets, e.g. `https://egress.example.workers.dev`. No trailing slash needed. |
| `WORKER_TOKEN` | **yes** | — | Bearer token for `PUT /api/egress/<kind>`. Secret (SOPS). |
| `QBT_URL` | **yes** | — | qBittorrent WebUI base, e.g. `http://qbittorrent.qbittorrent-private.svc:8082`. Must be reachable without credentials (pod CIDR in qbt's subnet whitelist, `home-ops#193`). |
| `RPC_STATE_FILE` | **yes** | — | Path of the hath RPC state file, e.g. `/var/lib/hath/rpc-servers.txt`. Written by `2017fighting/hath-rust` (`home-ops#195`). Missing/unreadable blocks the round. |
| `QBT_API_KEY` | no | *(empty)* | Optional bearer for qbt. Empty sends no `Authorization` header — the deployed qbt needs none. |
| `KUBE_API_URL` | no | `https://kubernetes.default.svc` | Cluster API base for `/api/v1/nodes`. |
| `KUBE_SA_TOKEN_FILE` | no | `/var/run/secrets/kubernetes.io/serviceaccount/token` | Projected token; read each round, so rotation is free. |
| `KUBE_CA_FILE` | no | `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | CA bundle for the cluster API. A missing file falls back to the system store. |
| `RPC_DOMAIN` | no | `rpc.hentaiathome.net` | The `rpc-domain` kind. The startup hop of the H@H RPC channel — cannot be derived from the pushed address list. |
| `MIHOMO_CONTROLLER_URL` | no | *(empty)* | mihomo `external-controller` base, e.g. `http://192.168.9.1:9090`. Empty **disables** the refresh trigger (publishes still happen). |
| `MIHOMO_SECRET` | no | *(empty)* | `external-controller` secret. Secret (SOPS). Empty sends no `Authorization` header. |
| `MIHOMO_PROVIDERS` | no | *(empty)* | Comma-separated rule-provider names to refresh, e.g. `egress-src,egress-tracker,egress-rpc-domain,egress-rpc-ip`. Empty = discover them from the controller. |
| `INTERVAL_SECONDS` | no | `3600` | Ticker period. The residual gap is one interval: a tracker added just after a round is uncovered until the next. |
| `LISTEN_ADDR` | no | `0.0.0.0:8080` | `/metrics` + `/healthz` bind address. Port is mandatory. |
| `MIN_ENTRY_RATIO` | no | `0.6` | Shrink floor: a kind with fewer than `ratio × previous` entries is refused. Must be in `(0, 1]`. |
| `HTTP_TIMEOUT_SECONDS` | no | `60` | Per-call timeout for every upstream/publish/refresh call. `sync/maindata` is ~16 MB and takes seconds, hence not a small default. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

Secrets are never logged: the start-up banner prints `***` for `WORKER_TOKEN`,
`MIHOMO_SECRET` and `QBT_API_KEY`.

## The publish gate

All four kinds are judged first; **one refusal cancels the round** — no kind is
published. The failure direction is always "keep the last published version,
which keeps traffic direct".

A kind is refused when:

1. it is empty (0 entries) — the qbt-returns-nothing incident;
2. any entry is not a valid domain (domain kinds) or address (address kinds),
   or the source produced an unparseable token;
3. it has fewer than `MIN_ENTRY_RATIO × previous` entries, where "previous" is
   the count of the last *fully published* round (in memory, so a restart has no
   baseline — one round of protection traded for no shared state).

The `rpc-domain`/`rpc-ip` pair adds one case from `home-ops#196`: a missing or
empty hath state file refuses the round rather than publishing an empty address
set, so the previous version stays in place.

Upstream failures (qbt down, cluster API 403, state file gone) are also refusals,
never a partial publish; they are classified `source_error` so the metric says
which kind of bad day it was.

## Metrics

Scraped from `LISTEN_ADDR` at `/metrics` (Prometheus text format). `/healthz` is
liveness only — a failed round is a metric, not a health check, so the pod is not
restarted for it.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `egress_sync_last_success_timestamp_seconds` | gauge | — | Unix time of the last fully published round; `0` if never. Alert on `time() - this > 2 × INTERVAL_SECONDS`. |
| `egress_sync_last_round_timestamp_seconds` | gauge | — | When the last round (any result) finished. |
| `egress_sync_entries` | gauge | `kind` | Entries in the last successfully published set. |
| `egress_sync_derived_entries` | gauge | `kind` | Entries derived this round, published or not. |
| `egress_sync_invalid_entries` | gauge | `kind` | Unparseable tokens this round. |
| `egress_sync_last_round_result` | gauge | `result` | One-hot: `published`, `gate_rejected`, `source_error`, `publish_error`. |
| `egress_sync_rounds_total` | counter | `result` | Rounds by classification. |
| `egress_sync_publish_rejected_total` | counter | `kind`, `reason` | Gate refusals; `reason` ∈ `empty`, `invalid`, `shrink`, `upstream`. |
| `egress_sync_publish_failures_total` | counter | `kind` | Publish calls that did not return 2xx. |
| `egress_sync_refresh_failures_total` | counter | — | Provider refreshes that failed (publish still counts as success). |
| `egress_sync_last_refresh_success_timestamp_seconds` | gauge | — | Last successful provider refresh; `0` if never. |
| `egress_sync_tracker_ip_literals` | gauge | — | Announce hosts that are bare IP literals. No publish kind carries those today (0 in current data); the gauge makes the gap visible instead of silent. |
| `egress_sync_build_info` | gauge | `version` | Always 1. |

## Running

```bash
# resident: one round immediately, then every INTERVAL_SECONDS
python -m egress_sync

# one round and exit — 0 published, 1 refused/failed, 2 configuration error
python -m egress_sync --once

# derive and gate, print the counts, publish nothing (safe on a live cluster)
python -m egress_sync --once --dry-run
```

`--once` prints `{"result": ..., "detail": ..., "entries": {...}, "dry_run": ...}`
on stderr and exits accordingly, which is what makes it useful for a first
rollout check: with `--dry-run` it verifies the three sources and the gate
without touching the worker.

Note `--dry-run` does not update the shrink baseline, and `--once` on a cold
start has no baseline at all — only the empty/invalid rules apply to it.

## Image and tags

`ghcr.io/2017fighting/egress-sync` — public (the cluster pulls without an
imagePullSecret unless it goes through the registry mirror), `linux/amd64`,
`python:3-alpine`, non-root (`uid/gid 65532`).

Tag convention: **`vMAJOR.MINOR.PATCH`** cut deliberately, plus `latest` moving
with each release. Pinning a tag is preferred in the HelmRelease; `latest` exists
for the mirror warm-up. A pushed `v*` tag is the release signal — the publish
workflow runs the tests, then builds and pushes that tag's commit.

## Layout

```
egress_sync/
  derive.py     pure functions: announce URL -> host, state file -> addresses, nodes -> IPs
  gate.py       all-or-nothing publish decision, per-kind reasons
  sources.py    the three sources + publisher + provider refresher (all take a Transport)
  transport.py  the only HTTP boundary (stdlib urllib; no third-party client)
  metrics.py    Prometheus text exposition over a fixed metric registry
  app.py        one round: derive -> gate -> publish -> refresh, and its classification
  config.py     the env table above, in code
  __main__.py   ticker, /metrics + /healthz, --once / --dry-run
tests/          table-driven; no test opens a socket or reads a real file
```

Only `derive.py` and `gate.py` are "logic"; everything else is wiring, which is
why the tests concentrate on tables in `tests/test_derive.py` and
`tests/test_gate.py` and use fake sources for the whole round in
`tests/test_round.py`.

## Development

Python 3.11+; **no runtime dependencies** (pytest is test-only).

```bash
python -m pytest          # 161 tests, ~0.1s, no network
```

## Deploy prerequisites (rollout order matters)

1. `home-ops#193` — qbt's runtime subnet whitelist admits the pod CIDR, otherwise
   every round is `source_error` with a 403.
2. `home-ops#195` — the hath fork writes the state file. Without it, `rpc-ip`
   derives empty and the gate refuses **every** round, including `src`/`tracker`;
   mount the same volume and point `RPC_STATE_FILE` at it.
3. `home-ops#192` — the worker's table, endpoint and rule-sets. Without the
   endpoint, every publish is a 4xx and the round is `publish_error`.
4. The state file must be readable by uid 65532 (world-readable, or the pod's
   `fsGroup`).
