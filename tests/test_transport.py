"""The trust store must be the system one *plus* the in-cluster CA.

`ca_file` is the Kubernetes API server's CA and signs nothing else, so a
context built from it alone makes every other https call fail. That is how
v0.2.1 shipped and it cost a publish on every round: the worker is https.
"""

from __future__ import annotations

import ssl

from egress_sync import transport


class _RecordingContext:
    def __init__(self, system_cas: int) -> None:
        self.system_cas = system_cas
        self.loaded: list[str] = []

    def load_verify_locations(self, *, cafile: str) -> None:
        self.loaded.append(cafile)


def test_ca_file_is_added_to_the_system_store(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    created: list[bool] = []
    context = _RecordingContext(system_cas=118)

    def fake_default_context(*, cafile=None):
        created.append(cafile)
        return context

    monkeypatch.setattr(ssl, "create_default_context", fake_default_context)

    built = transport.UrllibTransport(timeout=1.0, ca_file=str(ca))

    # The system store is what the context was built from ...
    assert created == [None]
    # ... and the in-cluster CA was loaded on top of it.
    assert context.loaded == [str(ca)]
    assert built._context is context


def test_missing_ca_file_falls_back_to_the_system_store(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **kwargs: (_ for _ in ()).throw(OSError("no such file")),
    )
    assert transport.UrllibTransport(timeout=1.0, ca_file=str(tmp_path / "absent.crt"))._context is None
