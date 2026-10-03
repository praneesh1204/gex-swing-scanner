"""Tech universe (spec v2.2 §4): themes, the outside-the-universe bar, theme caps, correlated-risk warning.

Preference is a filter, never a score boost: confidence grades the setup only. Universe ideas fill `top_n`;
names outside every theme are shown only above `outside.min_confidence`, at most `outside.max_per_day`, flagged
OUTSIDE UNIVERSE and never in a universe slot. Theme caps and the outside bar apply to the default run; an
explicit pick (`-s`, `--theme`, a ticker page in the web app) is yours and is not held back by them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FIXTURE_THEME = {"name": "Fixtures (synthetic)", "tickers": ["UPCO", "DNCO", "PINCO", "EARN", "RICH", "TERM"],
                 "blurb": "Synthetic test names for offline demos. Not real companies."}


@dataclass
class Theme:
    key: str
    name: str
    tickers: list[str]
    kind: str = "universe"          # universe / outside / fixtures
    blurb: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "tickers": list(self.tickers), "kind": self.kind,
                "blurb": self.blurb}


@dataclass
class Universe:
    mode: str = "prefer"            # prefer / only / off
    themes: dict = field(default_factory=dict)       # key -> Theme (universe + fixtures)
    outside: list = field(default_factory=list)
    outside_allow: bool = True
    outside_min_confidence: float = 85.0
    outside_max_per_day: int = 2
    max_per_theme: int = 3
    correlated_warn_pct: float = 0.60

    def themes_of(self, sym: str) -> list[str]:
        s = sym.upper()
        return [k for k, t in self.themes.items() if s in t.tickers]

    def in_universe(self, sym: str) -> bool:
        return bool(self.themes_of(sym))

    def symbols(self, themes: list[str] | None = None) -> list[str]:
        keys = themes or [k for k, t in self.themes.items() if t.kind == "universe"]
        out: list[str] = []
        for k in keys:
            t = self.themes.get(k)
            if t is None and k == "outside":
                out += [s for s in self.outside if s not in out]
                continue
            for s in (t.tickers if t else []):
                if s not in out:
                    out.append(s)
        return out

    def scan_list(self, watchlist: list[str]) -> list[str]:
        """The default run: mode off = the watchlist (v2.1); prefer = universe + outside + watchlist; only = universe."""
        if self.mode == "off":
            return list(watchlist)
        out = self.symbols()
        if self.mode == "prefer":
            extra = (self.outside if self.outside_allow else []) + list(watchlist)
            out += [s for s in extra if s not in out]
        return out

    def theme_list(self) -> list[dict]:
        rows = [t.to_dict() for t in self.themes.values()]
        if self.outside:
            rows.append(Theme("outside", "Outside the universe", list(self.outside), "outside",
                              f"Scanned in the daily run only above confidence {self.outside_min_confidence:g}, "
                              f"at most {self.outside_max_per_day} a day.").to_dict())
        return rows

    def theme(self, key: str) -> dict | None:
        return next((t for t in self.theme_list() if t["key"] == key), None)


def _tickers(xs, where: str) -> list[str]:
    bad = [x for x in xs if not isinstance(x, str)]
    if bad:  # YAML 1.1 reads ON / YES / NO / Y / N as booleans
        raise ValueError(f"universe.{where}: ticker {bad[0]!r} is not text; quote it in config.yaml, e.g. \"ON\"")
    return [x.upper() for x in xs]


def load_universe(cfg: dict, fixtures: bool = False) -> Universe:
    u = cfg.get("universe") or {}
    out = u.get("outside") or {}
    conc = u.get("concentration") or {}
    themes = {}
    for k, t in (u.get("themes") or {}).items():
        themes[k] = Theme(k, t.get("name", k), _tickers(t.get("tickers", []), k), "universe", t.get("blurb", ""))
    if fixtures:
        themes["fixtures"] = Theme("fixtures", FIXTURE_THEME["name"], list(FIXTURE_THEME["tickers"]), "fixtures",
                                   FIXTURE_THEME["blurb"])
    return Universe(mode=str(u.get("mode", "prefer")), themes=themes,
                    outside=_tickers(out.get("symbols", []), "outside"), outside_allow=bool(out.get("allow", True)),
                    outside_min_confidence=float(out.get("min_confidence", 85)),
                    outside_max_per_day=int(out.get("max_per_day", 2)),
                    max_per_theme=int(conc.get("max_ideas_per_theme", 3)),
                    correlated_warn_pct=float(conc.get("correlated_warn_pct", 0.60)))


def correlated_warning(ideas, uni: Universe) -> str | None:
    """Most of this universe moves together: warn when one theme carries > X of the shown ideas' max loss."""
    total, per = 0.0, {}
    for c in ideas:
        ml = c.max_loss if c.max_loss is not None else c.metrics.get("stop_loss_usd")
        if not ml:
            continue
        ml *= max(c.contracts, 1)
        total += ml
        for k in c.metrics.get("themes") or []:
            per[k] = per.get(k, 0.0) + ml
    if total <= 0 or not per:
        return None
    k, v = max(per.items(), key=lambda kv: kv[1])
    if v / total > uni.correlated_warn_pct and len(ideas) > 1:
        name = uni.themes[k].name if k in uni.themes else k
        return (f"Correlated risk: {v / total:.0%} of the shown ideas' max loss is in one theme ({name}). "
                "These names tend to move together; size as if it were one trade.")
    return None
