"""SQLite store: daily snapshots (IV history, OI by contract) and the trade-idea journal.

state/gexscan.db is local and git-ignored (it holds CBOE-derived data and your ideas).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  date TEXT, symbol TEXT, as_of TEXT, spot REAL, atm_iv REAL, iv30 REAL, net_gex REAL,
  call_wall REAL, put_wall REAL, gamma_flip REAL, call_volume REAL, put_volume REAL,
  call_oi REAL, put_oi REAL, rv20 REAL,
  PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS contract_oi (
  date TEXT, symbol TEXT, expiry TEXT, strike REAL, type TEXT, oi REAL, volume REAL,
  PRIMARY KEY (date, symbol, expiry, strike, type)
);
CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT, symbol TEXT, setup TEXT, score REAL, expiry TEXT, legs TEXT,
  entry_net REAL,            -- + credit / - debit per share, at mid
  width REAL, max_profit REAL, max_loss REAL, contracts INTEGER,
  spot REAL, put_wall REAL, call_wall REAL, gamma_flip REAL, net_gex REAL,
  tp_value REAL,             -- close when the position's net value reaches this
  stop_value REAL,           -- credit: close when net value >= this (loss = k x credit)
  stop_below REAL, stop_above REAL,   -- underlying-close stops / invalidation levels
  time_exit TEXT,            -- close on/after this date
  summary TEXT,
  status TEXT DEFAULT 'open', -- open / tp / stop / time / expired
  taken INTEGER DEFAULT 0,   -- 1 if you actually placed it (paper or real), via `gexscan journal take`
  last_mark REAL, last_mark_date TEXT, pnl REAL, closed_date TEXT, note TEXT
);
"""


class Store:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "gexscan.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._import_legacy_csv(Path(state_dir) / "picks.csv")

    def close(self):
        self.con.commit()
        self.con.close()

    # ---- snapshots -----------------------------------------------------------------------------
    def save_snapshot(self, date: dt.date, row: dict, contracts: pd.DataFrame | None = None) -> None:
        cols = ["date", "symbol", "as_of", "spot", "atm_iv", "iv30", "net_gex", "call_wall", "put_wall",
                "gamma_flip", "call_volume", "put_volume", "call_oi", "put_oi", "rv20"]
        vals = [date.isoformat()] + [row.get(c) for c in cols[1:]]
        self.con.execute(f"INSERT OR REPLACE INTO snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        if contracts is not None and not contracts.empty:
            sym = row["symbol"]
            self.con.execute("DELETE FROM contract_oi WHERE date=? AND symbol=?", (date.isoformat(), sym))
            recs = [(date.isoformat(), sym, str(e), float(k), t, float(o), float(v))
                    for e, k, t, o, v in contracts[["expiry", "strike", "type", "oi", "volume"]].itertuples(index=False)]
            self.con.executemany("INSERT OR REPLACE INTO contract_oi VALUES (?,?,?,?,?,?,?)", recs)
        self.con.commit()

    def iv_history(self, symbol: str, before: dt.date, days: int = 365) -> pd.Series:
        q = "SELECT date, atm_iv FROM snapshots WHERE symbol=? AND date<? AND date>=? AND atm_iv IS NOT NULL ORDER BY date"
        start = (before - dt.timedelta(days=days)).isoformat()
        df = pd.read_sql_query(q, self.con, params=(symbol, before.isoformat(), start))
        return pd.Series(df["atm_iv"].to_numpy(), index=pd.to_datetime(df["date"])) if len(df) else pd.Series(dtype=float)

    def previous_contract_oi(self, symbol: str, before: dt.date) -> tuple[str | None, pd.DataFrame]:
        row = self.con.execute("SELECT MAX(date) d FROM contract_oi WHERE symbol=? AND date<?",
                               (symbol, before.isoformat())).fetchone()
        if not row or not row["d"]:
            return None, pd.DataFrame()
        df = pd.read_sql_query("SELECT expiry, strike, type, oi FROM contract_oi WHERE symbol=? AND date=?",
                               self.con, params=(symbol, row["d"]))
        return row["d"], df

    # ---- ideas journal -------------------------------------------------------------------------
    def add_idea(self, rec: dict) -> int:
        rec = dict(rec)
        rec["legs"] = json.dumps(rec.get("legs", []))
        self.con.execute("DELETE FROM ideas WHERE run_date=? AND symbol=? AND taken=0",
                         (rec["run_date"], rec["symbol"]))   # re-running a day replaces its untaken ideas
        cols = list(rec)
        cur = self.con.execute(f"INSERT INTO ideas ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [rec[c] for c in cols])
        self.con.commit()
        return int(cur.lastrowid)

    def ideas(self, status: str | None = None, before: dt.date | None = None) -> list[dict]:
        q, p = "SELECT * FROM ideas WHERE 1=1", []
        if status:
            q += " AND status=?"
            p.append(status)
        if before:
            q += " AND run_date<?"
            p.append(before.isoformat())
        rows = [dict(r) for r in self.con.execute(q + " ORDER BY run_date, id", p)]
        for r in rows:
            r["legs"] = json.loads(r["legs"] or "[]")
        return rows

    def update_idea(self, idea_id: int, **fields) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE ideas SET {sets} WHERE id=?", [*fields.values(), idea_id])
        self.con.commit()

    def stops_on(self, date: dt.date) -> int:
        return self.con.execute("SELECT COUNT(*) FROM ideas WHERE status='stop' AND closed_date=?",
                                (date.isoformat(),)).fetchone()[0]

    # ---- legacy v0.1 pick log ------------------------------------------------------------------
    def _import_legacy_csv(self, path: Path) -> None:
        """One-time import of v0.1 state/picks.csv so earlier picks keep being reviewed."""
        if not path.exists() or self.con.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]:
            return
        try:
            df = pd.read_csv(path)
        except Exception:
            return
        leg_re = re.compile(r"(BUY|SELL) ([\d.]+)([CP])")
        for _, p in df.iterrows():
            legs = [{"action": a, "strike": float(k), "type": t} for a, k, t in leg_re.findall(str(p.get("trade", "")))]
            if not legs:
                continue
            strikes = [l["strike"] for l in legs]
            width = max(strikes) - min(strikes) if len(legs) == 2 else None
            net = float(p["net_mid"])
            self.add_idea({
                "run_date": str(p["run_date"]), "symbol": p["symbol"], "setup": p["setup"], "score": p["score"],
                "expiry": str(p["expiry"]), "legs": legs, "entry_net": net, "width": width,
                "max_profit": (net * 100 if net > 0 else ((width or 0) + net) * 100),
                "max_loss": float(p["max_loss"]), "contracts": 1, "spot": p["spot"], "put_wall": p["put_wall"],
                "call_wall": p["call_wall"], "gamma_flip": p["gamma_flip"], "net_gex": p["net_gex"],
                "summary": p["trade"], "note": "imported from v0.1 picks.csv",
            })
