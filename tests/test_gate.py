"""The publish gate: empty, too small, or malformed blocks the whole round."""

from __future__ import annotations

import pytest
from egress_sync.gate import (
    ADDRESS_KINDS,
    REASON_EMPTY,
    REASON_INVALID,
    REASON_SHRINK,
    REASON_UPSTREAM,
    Payload,
    evaluate,
    validate_entries,
)

from tests.conftest import good_payloads

RATIO = 0.6


def reasons(result) -> set[tuple[str, str]]:
    return {(r.kind, r.reason) for r in result.rejections}


def test_everything_valid_passes() -> None:
    result = evaluate(good_payloads(), {}, RATIO)

    assert result.ok
    assert result.rejections == ()


@pytest.mark.parametrize("kind", ["src", "tracker", "rpc-domain", "rpc-ip"])
def test_empty_set_is_rejected(kind: str) -> None:
    result = evaluate(good_payloads(**{kind: ()}), {}, RATIO)

    assert not result.ok
    assert (kind, REASON_EMPTY) in reasons(result)


def test_empty_rpc_ip_keeps_the_round_close() -> None:
    # home-ops#196: a missing/empty hath state file must not publish an empty set,
    # and must not let the other kinds through either.
    result = evaluate(good_payloads(**{"rpc-ip": ()}), {}, RATIO)

    assert not result.ok
    assert reasons(result) == {("rpc-ip", REASON_EMPTY)}


@pytest.mark.parametrize(
    "kind,bad_entry",
    [
        ("src", "10.0.0.300"),
        ("rpc-ip", "tracker.example.com"),
        ("tracker", "https://tracker.example.com/announce"),
        ("tracker", "1.2.3.4"),
        ("tracker", "not a domain"),
        ("rpc-domain", "localhost"),
    ],
)
def test_malformed_entry_is_rejected(kind: str, bad_entry: str) -> None:
    result = evaluate(good_payloads(**{kind: (bad_entry,)}), {}, RATIO)

    assert not result.ok
    assert (kind, REASON_INVALID) in reasons(result)


def test_unparseable_source_token_is_rejected() -> None:
    payloads = good_payloads()
    payloads[3] = Payload("rpc-ip", ("1.2.3.4",), invalid=("not-an-ip",))

    result = evaluate(payloads, {}, RATIO)

    assert not result.ok
    assert reasons(result) == {("rpc-ip", REASON_INVALID)}


def test_shrink_below_the_ratio_is_rejected() -> None:
    result = evaluate(good_payloads(tracker=("a.example.com", "b.example.com")), {"tracker": 10}, RATIO)

    assert not result.ok
    assert reasons(result) == {("tracker", REASON_SHRINK)}


def test_shrink_exactly_at_the_ratio_passes() -> None:
    entries = tuple(f"host{index}.example.com" for index in range(6))

    result = evaluate(good_payloads(tracker=entries), {"tracker": 10}, RATIO)

    assert result.ok


def test_growth_is_never_rejected() -> None:
    entries = tuple(f"host{index}.example.com" for index in range(40))

    assert evaluate(good_payloads(tracker=entries), {"tracker": 10}, RATIO).ok


def test_first_publish_has_no_baseline() -> None:
    # `--once` on a cold start: nothing to shrink from, so only empty/invalid apply.
    assert evaluate(good_payloads(tracker=("a.example.com",)), {}, RATIO).ok


def test_a_missing_kind_is_rejected_as_upstream() -> None:
    payloads = [p for p in good_payloads() if p.kind != "tracker"]

    result = evaluate(payloads, {}, RATIO)

    assert not result.ok
    assert reasons(result) == {("tracker", REASON_UPSTREAM)}


def test_one_rejection_blocks_every_kind() -> None:
    result = evaluate(good_payloads(src=()), {"tracker": 100}, RATIO)

    assert not result.ok
    # The gate is all-or-nothing: the round's caller publishes nothing, and the
    # summary names both the empty set and the stale baseline's shrink.
    assert ("src", REASON_EMPTY) in reasons(result)


def test_unknown_kind_cannot_be_constructed() -> None:
    with pytest.raises(ValueError):
        Payload("tracker-ip", ("1.2.3.4",))


def test_address_kinds_are_the_two_ip_sets() -> None:
    assert {"src", "rpc-ip"} == ADDRESS_KINDS


@pytest.mark.parametrize(
    "kind,entry,expected_valid",
    [
        ("src", "10.0.0.1", True),
        ("rpc-ip", "::ffff:1.2.3.4", True),
        ("rpc-ip", "1.2.3.4/32", False),
        ("tracker", "tracker.example.com", True),
        ("tracker", "10.0.0.1", False),
    ],
)
def test_validate_entries(kind: str, entry: str, expected_valid: bool) -> None:
    assert (validate_entries(kind, [entry]) == []) is expected_valid
