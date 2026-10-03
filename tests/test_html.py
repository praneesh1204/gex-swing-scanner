"""HTML pages for recommend / explain / alerts / backtest: content rules, escaping, redaction and the CLI writing them."""
import datetime as dt
import io
import re
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from gexscan.alerts import run_alerts
from gexscan.backtest import run_backtest
from gexscan.cli import app
from gexscan.config import load_config
from gexscan.engine.recommend import recommend
from gexscan.engine.selector import Filters
from gexscan.reports.html import render_alerts, render_backtest, render_explain, render_recommend

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)
NAMES = ["UPCO", "DNCO", "PINCO", "EARN", "RICH", "TERM"]
OFFLINE = ["--fixtures", str(FIX), "--today", TODAY.isoformat()]


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def recs(cfg, tmp_path_factory):
    return recommend(NAMES, Filters(), cfg, FIX, TODAY, tmp_path_factory.mktemp("state"), use_news=False)


def _common(html: str) -> None:
    low = html.lower()
    assert "never places" in low and "not financial advice" in low
    assert not re.search(r"\broll", re.sub(r"no rolling", "", low))        # "No rolling" is the only allowed form
    assert "built-in method" not in html and "bound method" not in html   # a dict key shadowed by a dict method
    assert "<script" not in low and "<form" not in low


def test_recommend_page(recs):
    html = render_recommend(recs, chart=False)
    _common(html)
    assert recs.recommendations
    for i, c in enumerate(recs.recommendations, 1):
        assert f'id="idea-{i}-{c.symbol}"' in html
    assert "Exit plan:" in html and "Stop = 2x credit; no rolling" in html
    assert "reaches 2× the credit (loss = 1× the credit)" in html
    assert "Scored but not shown" in html and "How this works" in html
    assert render_recommend(recs, chart=False, stop_mult=3.0).count("reaches 3× the credit (loss = 2× the credit)") == 1


def test_recommend_page_escapes_text(recs, monkeypatch):
    import gexscan.reports.html as h
    real = h.market_lines
    monkeypatch.setattr(h, "market_lines", lambda r: real(r) + ["<img src=x onerror=alert(1)>"])
    html = render_recommend(recs, chart=False)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_explain_page(recs):
    sym = next(c.symbol for c in recs.recommendations)
    html = render_explain(recs, sym, chart=False)
    _common(html)
    assert f"Explain · {sym}" in html and "Signal states" in html and "Gates by strategy" in html
    assert "Ideas that passed (0)" not in html
    assert "is not in this run" in render_explain(recs, "NOPE", chart=False)


def test_alerts_page_redacts_secrets(cfg, tmp_path, monkeypatch):
    pos = tmp_path / "positions.yaml"
    pos.write_text((ROOT / "tests" / "demo_positions.yaml").read_text())
    r = run_alerts(cfg, FIX, TODAY, tmp_path / "state", pos, False, ["RICH", "UPCO", "EARN"], True, ["log"], True,
                   Console(file=io.StringIO()))
    secret = "sk_live_html_secret_987"
    monkeypatch.setenv("FINNHUB_API_KEY", secret)
    r.errors.append(f"GET https://x.io/q?symbol=RICH&token={secret}")
    html = render_alerts(r)
    _common(html)
    assert secret not in html and "symbol=RICH" in html
    assert "RICH" in html and "STOP" in html and "Open short premium (2)" in html
    assert "2× the credit by default, a loss of 1× the credit" in html


def test_backtest_page(cfg, tmp_path):
    r = run_backtest(cfg, ["UPCO", "RICH"], None, None, FIX, TODAY, tmp_path)
    html = render_backtest(r, TODAY, chart=False)
    _common(html)
    assert "SYNTHETIC" in html and html.count(r.label) >= 2                # banner + footer
    assert "Results by strategy" in html and "Entries blocked by gates" in html


def test_cli_writes_html(tmp_path):
    run = CliRunner()
    out, state = tmp_path / "out", tmp_path / "state"
    base = ["--config", str(ROOT / "config.yaml"), *OFFLINE, "--state", str(state), "--out", str(out)]
    res = run.invoke(app, ["recommend", "-s", "RICH,TERM", *base, "--no-news"])
    assert res.exit_code == 0, res.output
    assert (out / f"recommendations_{TODAY}.html").exists() and "HTML:" in res.output
    res = run.invoke(app, ["explain", "RICH", *base, "--no-news"])
    assert res.exit_code == 0, res.output
    assert (out / f"explain_RICH_{TODAY}.html").exists()
    res = run.invoke(app, ["alerts", "-s", "RICH", "--positions", str(ROOT / "tests" / "demo_positions.yaml"),
                           "--no-ibkr", "--dry-run", *base])
    assert res.exit_code == 0, res.output
    assert (out / f"alerts_{TODAY}.html").exists()
    res = run.invoke(app, ["backtest", "-s", "RICH", *base, "--json"])
    assert res.exit_code == 0, res.output
    assert (out / f"backtest_{TODAY}.html").exists()
