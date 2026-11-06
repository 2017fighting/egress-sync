"""The publish gate: the only thing standing between a broken source and a
rule-set that silently stops covering the trackers.

Failure direction is always "keep the last published version" (ADR-0024), which
means the gate answers a single question per round: may *any* kind go out? A
partial publish is worse than no publish — it would leave, say, the tracker set
updated and the RPC set stale, and neither half is verifiable from here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import KINDS
from .derive import is_valid_domain, normalize_ip

# Which validator judges each kind's entries. `src` and `rpc-ip` are address
# sets, `tracker` and `rpc-domain` are domain sets.
ADDRESS_KINDS = frozenset({"src", "rpc-ip"})

REASON_EMPTY = "empty"
REASON_INVALID = "invalid"
REASON_SHRINK = "shrink"
REASON_UPSTREAM = "upstream"


def validate_entries(kind: str, entries: Sequence[str]) -> list[str]:
    """Return the entries that are not expressible in this kind's rule-set."""
    bad: list[str] = []
    for entry in entries:
        text = str(entry)
        if kind in ADDRESS_KINDS:
            if normalize_ip(text) is None:
                bad.append(text)
        elif not is_valid_domain(text):
            bad.append(text)
    return bad


@dataclass(frozen=True)
class Payload:
    """One kind's derived set, before the gate looks at it."""

    kind: str
    entries: tuple[str, ...]
    # Tokens the source produced that could not be normalised at all (e.g. a
    # corrupt line in the hath state file). Non-empty is a rejection.
    invalid: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind: {self.kind}")


@dataclass(frozen=True)
class Rejection:
    kind: str
    reason: str
    detail: str


@dataclass(frozen=True)
class GateResult:
    ok: bool
    rejections: tuple[Rejection, ...]

    @property
    def summary(self) -> str:
        return "; ".join(f"{r.kind}:{r.reason}({r.detail})" for r in self.rejections)


def evaluate(
    payloads: Sequence[Payload],
    previous: Mapping[str, int],
    min_ratio: float,
) -> GateResult:
    """Judge every kind; one rejection blocks the round.

    `previous` is the entry count of the last *published* version of each kind.
    A kind with no previous version is only subject to the empty/invalid rules —
    there is no baseline to shrink from, which is also why `--once` cannot
    enforce the ratio check on a first run.
    """
    rejections: list[Rejection] = []
    seen = {p.kind for p in payloads}

    for kind in KINDS:
        if kind not in seen:
            rejections.append(Rejection(kind, REASON_UPSTREAM, "not derived this round"))
    for payload in payloads:
        if not payload.entries:
            rejections.append(Rejection(payload.kind, REASON_EMPTY, "0 entries"))
        if payload.invalid:
            rejections.append(Rejection(payload.kind, REASON_INVALID, f"unparseable: {', '.join(payload.invalid[:5])}"))
        invalid = validate_entries(payload.kind, payload.entries)
        if invalid:
            rejections.append(
                Rejection(
                    payload.kind,
                    REASON_INVALID,
                    f"not a valid {'address' if payload.kind in ADDRESS_KINDS else 'domain'}: {', '.join(invalid[:5])}",
                )
            )
        baseline = previous.get(payload.kind)
        if baseline and payload.entries:
            floor = baseline * min_ratio
            if len(payload.entries) < floor:
                rejections.append(
                    Rejection(
                        payload.kind,
                        REASON_SHRINK,
                        f"{len(payload.entries)} < {floor:.1f} ({min_ratio:.0%} of {baseline})",
                    )
                )

    return GateResult(ok=not rejections, rejections=tuple(rejections))
