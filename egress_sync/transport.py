"""The single HTTP boundary.

Everything that speaks to another process goes through `Transport`. Tests swap
in `FakeTransport` (tests/conftest.py) and assert on the calls, so no test in
this repository opens a socket.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

USER_AGENT = "egress-sync"


class HttpError(RuntimeError):
    """A call failed: transport error, or a status outside the accepted set."""

    def __init__(self, message: str, *, status: int | None = None, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> object:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(
                f"response is not JSON ({len(self.body)} bytes): {exc}", status=self.status, body=self.body
            ) from exc


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response: ...


class UrllibTransport:
    """stdlib-only implementation; no third-party HTTP client by design."""

    def __init__(self, timeout: float = 60.0, ca_file: str | None = None) -> None:
        self._timeout = timeout
        self._context: ssl.SSLContext | None = None
        if ca_file:
            # ADD to the system trust store, never replace it. `ca_file` is the
            # Kubernetes API server's CA, which signs nothing else — building the
            # context from it alone makes every other https call fail with
            # "unable to get local issuer certificate" (found in production
            # 2026-11-09: the worker is https, so no publish ever landed).
            try:
                self._context = ssl.create_default_context()
                self._context.load_verify_locations(cafile=ca_file)
            except OSError:
                # The CA file only exists in-cluster; locally we fall back to the
                # system store rather than refusing to start.
                self._context = None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method.upper())
        request.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=self._context) as response:
                return Response(status=response.status, body=response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400]
            raise HttpError(
                f"{method.upper()} {url} -> HTTP {exc.code}: {detail!r}", status=exc.code, body=detail
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError(f"{method.upper()} {url} -> {exc}") from exc
