"""The env contract. These tests are the README table, executable."""

from __future__ import annotations

import pytest
from egress_sync.config import DEFAULTS, REQUIRED, Config, ConfigError


def test_required_variables_are_the_documented_ones() -> None:
    assert REQUIRED == ("WORKER_URL", "WORKER_TOKEN", "QBT_URL", "RPC_STATE_FILE")


def test_minimal_config_uses_documented_defaults(base_env: dict[str, str]) -> None:
    config = Config.from_env(base_env)

    assert config.worker_url == "https://worker.example.workers.dev"
    assert config.rpc_domain == DEFAULTS["RPC_DOMAIN"]
    assert config.interval_seconds == 3600.0
    assert config.min_entry_ratio == 0.6
    assert config.listen_host == "0.0.0.0"
    assert config.listen_port == 8080
    assert config.mihomo_controller_url == ""
    assert config.mihomo_providers == ()
    assert config.kube_api_url == "https://kubernetes.default.svc"


@pytest.mark.parametrize("missing", REQUIRED)
def test_each_required_variable_is_enforced(base_env: dict[str, str], missing: str) -> None:
    env = dict(base_env)
    env.pop(missing)

    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env)

    assert missing in str(excinfo.value)


def test_blank_required_variable_is_missing(base_env: dict[str, str]) -> None:
    env = {**base_env, "WORKER_TOKEN": "   "}

    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_trailing_slashes_are_normalised(base_env: dict[str, str]) -> None:
    config = Config.from_env({**base_env, "WORKER_URL": "https://w/", "QBT_URL": "http://q:8082/"})

    assert config.worker_url == "https://w"
    assert config.qbt_url == "http://q:8082"


def test_provider_list_is_split_and_trimmed(base_env: dict[str, str]) -> None:
    config = Config.from_env({**base_env, "MIHOMO_PROVIDERS": " a , b ,, "})

    assert config.mihomo_providers == ("a", "b")


@pytest.mark.parametrize("value", ["0", "-0.5", "1.5", "nan", "abc", ""])
def test_min_entry_ratio_is_validated(base_env: dict[str, str], value: str) -> None:
    with pytest.raises(ConfigError):
        Config.from_env({**base_env, "MIN_ENTRY_RATIO": value})


def test_ratio_of_one_is_allowed(base_env: dict[str, str]) -> None:
    assert Config.from_env({**base_env, "MIN_ENTRY_RATIO": "1"}).min_entry_ratio == 1.0


@pytest.mark.parametrize("value", ["host:port", ":", "0.0.0.0:"])
def test_listen_addr_must_carry_a_numeric_port(base_env: dict[str, str], value: str) -> None:
    with pytest.raises(ConfigError):
        Config.from_env({**base_env, "LISTEN_ADDR": value})


def test_listen_addr_parses_host_and_port(base_env: dict[str, str]) -> None:
    config = Config.from_env({**base_env, "LISTEN_ADDR": "127.0.0.1:9110"})

    assert (config.listen_host, config.listen_port) == ("127.0.0.1", 9110)


def test_rpc_domain_is_normalised(base_env: dict[str, str]) -> None:
    config = Config.from_env({**base_env, "RPC_DOMAIN": "RPC.HentaiAtHome.NET."})

    assert config.rpc_domain == "rpc.hentaiathome.net"


def test_invalid_rpc_domain_fails_at_startup(base_env: dict[str, str]) -> None:
    from egress_sync.app import assert_config

    with pytest.raises(ValueError):
        assert_config(Config.from_env({**base_env, "RPC_DOMAIN": "not a domain"}))


def test_config_summary_redacts_secrets(base_env: dict[str, str]) -> None:
    from egress_sync.app import config_summary

    summary = config_summary(Config.from_env({**base_env, "MIHOMO_SECRET": "topsecret", "QBT_API_KEY": "k"}))

    assert summary["worker_token"] == "***"
    assert summary["mihomo_secret"] == "***"
    assert summary["qbt_api_key"] == "***"
    assert "s3cret" not in str(summary)


def test_extra_source_entries_are_parsed_and_optional(base_env: dict[str, str]) -> None:
    """The VIP is configured, not derived: one line in the ConfigMap."""
    from egress_sync.config import Config

    assert Config.from_env(base_env).extra_source_entries == ()
    assert (
        Config.from_env({**base_env, "EXTRA_SOURCE_ENTRIES": "192.168.9.20, 10.0.0.9"}).extra_source_entries
        == ("192.168.9.20", "10.0.0.9")
    )
