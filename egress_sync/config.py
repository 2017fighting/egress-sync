"""Environment-driven configuration.

The table in README.md is this module's contract: every name read here appears
there, with the same meaning and the same default. Adding a variable here
without adding the row means the HelmRelease cannot know about it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Defaults are chosen so that a local `--once --dry-run` in-cluster needs only
# the four required variables.
DEFAULTS = {
    "KUBE_API_URL": "https://kubernetes.default.svc",
    "KUBE_SA_TOKEN_FILE": "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "KUBE_CA_FILE": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    "QBT_API_KEY": "",
    "RPC_DOMAIN": "rpc.hentaiathome.net",
    "MIHOMO_CONTROLLER_URL": "",
    "MIHOMO_SECRET": "",
    "MIHOMO_PROVIDERS": "",
    "EXTRA_SOURCE_ENTRIES": "",
    "INTERVAL_SECONDS": "3600",
    "LISTEN_ADDR": "0.0.0.0:8080",
    "MIN_ENTRY_RATIO": "0.6",
    "HTTP_TIMEOUT_SECONDS": "60",
    "LOG_LEVEL": "INFO",
}

REQUIRED = ("WORKER_URL", "WORKER_TOKEN", "QBT_URL", "RPC_STATE_FILE")


class ConfigError(ValueError):
    """A variable is missing or unusable; the process must not start."""


@dataclass(frozen=True)
class Config:
    worker_url: str
    worker_token: str
    qbt_url: str
    qbt_api_key: str
    rpc_state_file: str
    rpc_domain: str
    kube_api_url: str
    kube_sa_token_file: str
    kube_ca_file: str
    mihomo_controller_url: str
    mihomo_secret: str
    mihomo_providers: tuple[str, ...]
    extra_source_entries: tuple[str, ...]
    interval_seconds: float
    listen_host: str
    listen_port: int
    min_entry_ratio: float
    http_timeout_seconds: float
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        missing = [name for name in REQUIRED if not _get(env, name).strip()]
        if missing:
            raise ConfigError("missing required environment variable(s): " + ", ".join(missing))

        host, port = _parse_listen(_get(env, "LISTEN_ADDR"))

        ratio = _parse_float(env, "MIN_ENTRY_RATIO")
        if not 0.0 < ratio <= 1.0:
            raise ConfigError("MIN_ENTRY_RATIO must be in (0, 1]")

        providers = tuple(p.strip() for p in _get(env, "MIHOMO_PROVIDERS").split(",") if p.strip())
        extra_sources = tuple(p.strip() for p in _get(env, "EXTRA_SOURCE_ENTRIES").split(",") if p.strip())

        return cls(
            worker_url=_get(env, "WORKER_URL").rstrip("/"),
            worker_token=_get(env, "WORKER_TOKEN").strip(),
            qbt_url=_get(env, "QBT_URL").rstrip("/"),
            qbt_api_key=_get(env, "QBT_API_KEY").strip(),
            rpc_state_file=_get(env, "RPC_STATE_FILE").strip(),
            rpc_domain=_get(env, "RPC_DOMAIN").strip().lower().rstrip("."),
            kube_api_url=_get(env, "KUBE_API_URL").rstrip("/"),
            kube_sa_token_file=_get(env, "KUBE_SA_TOKEN_FILE"),
            kube_ca_file=_get(env, "KUBE_CA_FILE"),
            mihomo_controller_url=_get(env, "MIHOMO_CONTROLLER_URL").rstrip("/"),
            mihomo_secret=_get(env, "MIHOMO_SECRET").strip(),
            mihomo_providers=providers,
            extra_source_entries=extra_sources,
            interval_seconds=_parse_float(env, "INTERVAL_SECONDS"),
            listen_host=host,
            listen_port=port,
            min_entry_ratio=ratio,
            http_timeout_seconds=_parse_float(env, "HTTP_TIMEOUT_SECONDS"),
            log_level=_get(env, "LOG_LEVEL").strip().upper() or "INFO",
        )


def _get(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None:
        return DEFAULTS.get(name, "")
    return value


def _parse_float(env: Mapping[str, str], name: str) -> float:
    raw = _get(env, name).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} is not a number: {raw!r}") from exc


def _parse_listen(raw: str) -> tuple[str, int]:
    """`host:port` -> (host, port); the port is the only part that must hold."""
    text = (raw or "").strip()
    if not text:
        return ("0.0.0.0", 8080)
    host, _, port_text = text.rpartition(":")
    if not port_text.isdigit():
        raise ConfigError(f"LISTEN_ADDR has no numeric port: {raw!r}")
    return (host or "0.0.0.0", int(port_text))
