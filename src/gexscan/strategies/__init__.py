"""Strategy plugins (spec §4). Each plugin: `name` + `evaluate(universe, signals) -> list[Candidate]`.

Add a strategy: subclass strategies.base.Strategy, implement build(), add its gates to signal_sheet.yaml and
register it here (or call register()).
"""
from __future__ import annotations

from .base import Candidate, Confidence, OptLeg, Strategy
from .calendars import Calendar, DoubleCalendar, DoubleDiagonal
from .covered_call import CoveredCall
from .csp import CashSecuredPut
from .debit_verticals import BearPut, BullCall
from .earnings_crush import EarningsCrush
from .iron_condor import IronCondor
from .verticals import BearCall, BullPut

REGISTRY: dict[str, type[Strategy]] = {c.name: c for c in (
    CashSecuredPut, CoveredCall, BullPut, BearCall, BullCall, BearPut, IronCondor, Calendar, DoubleCalendar,
    DoubleDiagonal, EarningsCrush)}


def register(cls: type[Strategy]) -> None:
    REGISTRY[cls.name] = cls


def load_strategies(sheet: dict, cfg: dict, names: list[str] | None = None) -> list[Strategy]:
    names = names or list(cfg.get("engine", {}).get("strategies") or REGISTRY)
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown strategies {unknown}; known: {sorted(REGISTRY)}")
    missing = [n for n in names if n not in sheet.get("strategies", {})]
    if missing:
        raise ValueError(f"strategies {missing} have no gates in the signal sheet")
    return [REGISTRY[n](sheet, cfg) for n in names]


__all__ = ["REGISTRY", "Candidate", "Confidence", "OptLeg", "Strategy", "load_strategies", "register"]
