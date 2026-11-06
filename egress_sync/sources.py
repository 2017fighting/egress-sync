"""The three upstream sources, plus the publisher and the refresh trigger.

Each class takes a `Transport`, so a test replaces one object to run a whole
round without a network. The derivation itself lives in derive.py and never sees
any of this.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Iterable, Mapping

from . import CLIENT_NAME
from .derive import NodeIPs, RpcAddrs, TrackerHosts, derive_node_ips, derive_tracker_hosts, parse_rpc_state
from .transport import HttpError, Response, Transport


class SourceError(RuntimeError):
    """An upstream could not be read. Blocks the round — never a partial publish."""


class ClusterNodeSource:
    """`GET /api/v1/nodes` with the pod's ServiceAccount token."""

    def __init__(self, transport: Transport, api_url: str, token_file: str) -> None:
        self._transport = transport
        self._api_url = api_url.rstrip("/")
        self._token_file = token_file

    def fetch(self) -> NodeIPs:
        token = _read_file(self._token_file, "ServiceAccount token").strip()
        if not token:
            raise SourceError(f"ServiceAccount token file is empty: {self._token_file}")
        response = _expect(
            self._transport.request(
                "GET",
                f"{self._api_url}/api/v1/nodes",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ),
            "cluster API /api/v1/nodes",
        )
        try:
            return derive_node_ips(response.body)
        except ValueError as exc:
            raise SourceError(f"cluster API /api/v1/nodes returned unusable JSON: {exc}") from exc


class QbtTrackerSource:
    """`GET /api/v2/sync/maindata` -> the `.trackers` dictionary keys.

    No credentials by design (ADR-0024): qbt's runtime subnet whitelist admits
    the pod CIDR. `api_key` exists for a future qbt that needs one; empty means
    no Authorization header at all.
    """

    def __init__(self, transport: Transport, qbt_url: str, api_key: str = "") -> None:
        self._transport = transport
        self._qbt_url = qbt_url.rstrip("/")
        self._api_key = api_key.strip()

    def fetch(self) -> TrackerHosts:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = _expect(
            self._transport.request("GET", f"{self._qbt_url}/api/v2/sync/maindata", headers=headers),
            "qbt sync/maindata",
        )
        body = response.json()
        if not isinstance(body, Mapping):
            raise SourceError("qbt sync/maindata is not a JSON object")
        trackers = body.get("trackers")
        if trackers is None:
            raise SourceError("qbt sync/maindata has no `.trackers` dictionary")
        if not isinstance(trackers, Mapping):
            raise SourceError("qbt sync/maindata `.trackers` is not a dictionary")
        return derive_tracker_hosts(trackers.keys())


class RpcStateSource:
    """The hath fork's state file: one normalised IPv4 per line.

    Missing or unreadable is a `SourceError`, not an empty set: an empty set
    would look like "the server pushed nothing", and the difference matters when
    reading the failure afterwards.
    """

    def __init__(self, path: str, read_text: Callable[[str], str] | None = None) -> None:
        self._path = path
        self._read_text = read_text or (lambda p: pathlib.Path(p).read_text(encoding="utf-8", errors="replace"))

    def fetch(self) -> RpcAddrs:
        try:
            text = self._read_text(self._path)
        except FileNotFoundError as exc:
            raise SourceError(f"hath RPC state file does not exist: {self._path}") from exc
        except OSError as exc:
            raise SourceError(f"hath RPC state file is unreadable: {self._path}: {exc}") from exc
        return parse_rpc_state(text)


class Publisher:
    """`PUT {worker}/api/egress/{kind}` — the fixed contract with the worker."""

    def __init__(self, transport: Transport, worker_url: str, token: str) -> None:
        self._transport = transport
        self._worker_url = worker_url.rstrip("/")
        self._token = token

    def publish(self, kind: str, entries: Iterable[str]) -> None:
        payload = json.dumps({"entries": list(entries), "client": CLIENT_NAME}).encode("utf-8")
        _expect(
            self._transport.request(
                "PUT",
                f"{self._worker_url}/api/egress/{kind}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=payload,
            ),
            f"worker PUT /api/egress/{kind}",
        )


class ProviderRefresher:
    """Ask mihomo to re-pull its rule providers (external-controller).

    Failure is logged and counted, never fatal: the publish already happened and
    the provider picks the change up on its own schedule.
    """

    def __init__(self, transport: Transport, controller_url: str, secret: str, providers: Iterable[str] = ()) -> None:
        self._transport = transport
        self._controller_url = controller_url.rstrip("/")
        self._secret = secret.strip()
        self._providers = tuple(p.strip() for p in providers if p.strip())

    @property
    def enabled(self) -> bool:
        return bool(self._controller_url)

    def refresh(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        names = self.providers()
        for name in names:
            _expect(
                self._transport.request(
                    "PUT",
                    f"{self._controller_url}/providers/rules/{name}",
                    headers=self._headers(),
                ),
                f"mihomo provider refresh {name}",
            )
        return names

    def providers(self) -> tuple[str, ...]:
        """Configured names, or the controller's own list when none are given.

        Discovered rather than defaulted so renaming a rule-set in the mihomo
        config does not silently leave it stale.
        """
        if self._providers:
            return self._providers
        response = _expect(
            self._transport.request("GET", f"{self._controller_url}/providers/rules", headers=self._headers()),
            "mihomo GET /providers/rules",
        )
        body = response.json()
        if not isinstance(body, Mapping):
            raise HttpError("mihomo /providers/rules is not a JSON object")
        providers = body.get("providers") or {}
        if not isinstance(providers, Mapping):
            raise HttpError("mihomo /providers/rules has no `.providers` object")
        return tuple(sorted(str(name) for name in providers))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        return headers


def _read_file(path: str, what: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise SourceError(f"{what} is unreadable: {path}: {exc}") from exc


def _expect(response: Response, what: str) -> Response:
    if 200 <= response.status < 300:
        return response
    raise HttpError(
        f"{what} -> HTTP {response.status}: {response.body[:400]!r}", status=response.status, body=response.body
    )
