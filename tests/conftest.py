"""Shared test doubles: nothing here opens a socket or reads a real file."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pytest
from egress_sync.config import Config
from egress_sync.derive import NodeIPs, RpcAddrs, RpcState, TrackerHosts
from egress_sync.gate import Payload
from egress_sync.metrics import Metrics
from egress_sync.transport import Response


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


Responder = Callable[[Call], Response]


class FakeTransport:
    """Records every call and answers from a mapping or a callable.

    A key in `routes` is `(method, url)`; a value is either a `Response` or an
    exception instance to raise.
    """

    def __init__(
        self,
        routes: Mapping[tuple[str, str], Response | Exception] | None = None,
        responder: Responder | None = None,
    ) -> None:
        self.calls: list[Call] = []
        self._routes = dict(routes or {})
        self._responder = responder

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        call = Call(method=method, url=url, headers=dict(headers or {}), body=body)
        self.calls.append(call)
        if self._responder is not None:
            return self._responder(call)
        outcome = self._routes.get((method, url))
        if outcome is None:
            raise AssertionError(f"unexpected call: {method} {url}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def published_kinds(self) -> list[str]:
        prefix = "/api/egress/"
        return [call.url.rsplit(prefix, 1)[1] for call in self.calls if prefix in call.url]


def json_response(payload: object, status: int = 200) -> Response:
    import json

    return Response(status=status, body=json.dumps(payload).encode("utf-8"))


@pytest.fixture
def base_env() -> dict[str, str]:
    return {
        "WORKER_URL": "https://worker.example.workers.dev",
        "WORKER_TOKEN": "s3cret",
        "QBT_URL": "http://qbittorrent.qbittorrent-private.svc:8082",
        "RPC_STATE_FILE": "/var/lib/hath/rpc-servers.txt",
    }


@pytest.fixture
def config(base_env: dict[str, str]) -> Config:
    return Config.from_env(base_env)


def good_payloads(**overrides: tuple[str, ...]) -> list[Payload]:
    """One valid payload per kind; pass a kind as a keyword to override it."""
    defaults = {
        "src": ("10.0.0.11", "10.0.0.12", "10.0.0.13"),
        "tracker": ("a.example.com", "tracker.b.example.com"),
        "rpc-domain": ("rpc.hentaiathome.net",),
        "rpc-ip": ("1.2.3.4", "5.6.7.8", "9.9.9.9"),
    }
    merged = {**defaults, **overrides}
    return [Payload(kind, tuple(entries)) for kind, entries in merged.items()]


@pytest.fixture
def metrics() -> Metrics:
    return Metrics("test")


class StubNodeSource:
    def __init__(self, result: NodeIPs | Exception) -> None:
        self.result = result
        self.calls = 0

    def fetch(self) -> NodeIPs:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubTrackerSource:
    def __init__(self, result: TrackerHosts | Exception) -> None:
        self.result = result
        self.calls = 0

    def fetch(self) -> TrackerHosts:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubRpcSource:
    """A `RpcState`, or a bare `RpcAddrs` meaning "usable addresses"."""

    def __init__(self, result: RpcState | RpcAddrs | Exception) -> None:
        self.result = RpcState(result) if isinstance(result, RpcAddrs) else result
        self.calls = 0

    def fetch(self) -> RpcState:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubPublisher:
    def __init__(self, fail_at: str | None = None, error: Exception | None = None) -> None:
        self.published: list[tuple[str, tuple[str, ...]]] = []
        self._fail_at = fail_at
        self._error = error or RuntimeError("publish failed")

    def publish(self, kind: str, entries) -> None:
        if kind == self._fail_at:
            raise self._error
        self.published.append((kind, tuple(entries)))


class StubRefresher:
    def __init__(self, enabled: bool = True, failure: Exception | None = None, names: tuple[str, ...] = ("p",)) -> None:
        self.enabled = enabled
        self.failure = failure
        self.names = names
        self.calls = 0

    def refresh(self) -> tuple[str, ...]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.names
