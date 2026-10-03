"""The local web app (spec v2.2): server hardening, the JSON API on fixtures, and the front-end source rules."""
import datetime as dt
import http.client
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from gexscan.config import load_config
from gexscan.web.api import Api
from gexscan.web.server import CSP, SECURITY_HEADERS, make_server

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
STATIC = ROOT / "src" / "gexscan" / "web" / "static"
TODAY = dt.date(2026, 9, 25)
SECRET = "sk-TESTSECRET-0123456789"
KEYS = [k for k in re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M)
        if re.search(r"KEY|TOKEN|PASSWORD", k)]


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    """A real server on a free port, backed by the fixtures; every key in .env.example set to a sentinel."""
    mp = pytest.MonkeyPatch()
    for k in KEYS:
        mp.setenv(k, SECRET)
    state = tmp_path_factory.mktemp("webstate")
    srv = make_server(Api(load_config(ROOT / "config.yaml"), FIX, TODAY, state, use_news=False), 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"port": srv.server_address[1], "state": state}
    srv.shutdown()
    srv.server_close()
    mp.undo()


def req(web, path, method="GET", host=None):
    c = http.client.HTTPConnection("127.0.0.1", web["port"], timeout=60)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", host or f"127.0.0.1:{web['port']}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def jget(web, path):
    code, h, body = req(web, path)
    assert h["content-type"].startswith("application/json")
    assert h["cache-control"] == "no-store"
    return code, json.loads(body)


def _assert_secure(h):
    for k, v in SECURITY_HEADERS.items():
        assert h.get(k.lower()) == v, k
    assert "access-control-allow-origin" not in h


# ---------- server hardening ----------

@pytest.mark.parametrize("path", ["/", "/about", "/builder", "/theme/ai_compute", "/ticker/UPCO", "/scan/UPCO",
                                  "/scan/theme/ai_compute", "/static/app.js", "/static/silver.css", "/api/meta",
                                  "/nope", "/api/nope"])
def test_security_headers_everywhere(web, path):
    code, h, _ = req(web, path)
    assert code in (200, 404)
    _assert_secure(h)
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP and "script-src 'self'" in CSP


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_only_get_and_head(web, method):
    code, h, _ = req(web, "/api/meta", method)
    assert code == 405 and h["allow"] == "GET, HEAD"
    _assert_secure(h)


def test_head_has_no_body(web):
    code, h, body = req(web, "/api/meta", "HEAD")
    assert code == 200 and body == b"" and int(h["content-length"]) > 0


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "0.0.0.0:{port}"])
def test_host_allow_list(web, host):
    code, h, _ = req(web, "/api/meta", host=host.format(port=web["port"]))
    assert code == 421
    _assert_secure(h)


def test_localhost_host_is_allowed(web):
    assert req(web, "/api/meta", host=f"localhost:{web['port']}")[0] == 200


@pytest.mark.parametrize("path", ["/static/../pyproject.toml", "/static/%2e%2e/api.py", "/static/..%2fserver.py",
                                  "/static/app.html", "/static/APP.JS", "/static/", "/static/x.py", "/static//etc/passwd",
                                  "/ticker/upco", "/ticker/TOOLONGSYMBOL1", "/theme/../x", "/scan", "/index.html",
                                  "/api", "/api/", "/api/meta/x"])
def test_unknown_paths_404(web, path):
    assert req(web, path)[0] == 404


def test_url_too_long(web):
    assert req(web, "/api/ticker?symbol=" + "A" * 9000)[0] == 414


def test_pages_are_the_shell_and_static_is_typed(web):
    code, h, body = req(web, "/ticker/UPCO")
    assert code == 200 and h["content-type"].startswith("text/html")
    assert b'<script type="module" src="/static/app.js">' in body
    code, h, _ = req(web, "/static/app.js")
    assert code == 200 and h["content-type"].startswith("text/javascript")
    code, h, _ = req(web, "/static/silver.css")
    assert code == 200 and h["content-type"].startswith("text/css")


def test_binds_loopback_only():
    with pytest.raises(ValueError):
        make_server(None, 0, host="0.0.0.0")


# ---------- the API on fixtures ----------

def test_meta_and_themes(web):
    code, m = jget(web, "/api/meta")
    assert code == 200 and m["mode"] == "fixtures" and m["today"] == TODAY.isoformat()
    assert "never places" in m["never_orders"] and "not financial advice" in m["disclaimer"].lower()
    code, t = jget(web, "/api/themes")
    assert code == 200 and t["themes"] and all(th["tickers"] for th in t["themes"])


def test_ticker(web):
    code, t = jget(web, "/api/ticker?symbol=UPCO")
    assert code == 200 and t["symbol"] == "UPCO" and t["spot"] > 0
    for k in ("performance", "catalysts", "risks", "outlook", "levels", "fresh"):
        assert k in t


@pytest.mark.parametrize("path", ["/api/ticker", "/api/ticker?symbol=", "/api/ticker?symbol=../etc",
                                  "/api/ticker?symbol=%3Cscript%3E", "/api/scan", "/api/scan?theme=nope",
                                  "/api/scan?symbol=UPCO&strategies=martingale", "/api/scan?symbol=UPCO&dte_min=50&dte_max=10",
                                  "/api/chain?symbol=UPCO&expiry=2026-02-30", "/api/simulate?spot=100",
                                  "/api/simulate?legs=garbage&spot=100", "/api/simulate?legs=B:C:100:2026-10-16:1:2:0.5&spot=nan",
                                  "/api/simulate?legs=B:C:100:2026-10-16:1:2:0.5&spot=100&event=2026-10-01:9"])
def test_bad_requests_are_400(web, path):
    code, out = jget(web, path)
    assert code == 400 and out["error"] and "Traceback" not in out["error"]


def test_chain(web):
    code, c = jget(web, "/api/chain?symbol=UPCO")
    assert code == 200 and c["strikes"] and c["expiry"] in {e["expiry"] for e in c["expiries"]}
    assert all(e["dte"] >= 0 for e in c["expiries"]) and c["levels"]["call_wall"]


def _db_counts(state: Path) -> dict:
    db = state / "gexscan.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    try:
        return {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in ("ideas", "snapshots")}
    finally:
        con.close()


def test_scan_has_exit_plans_and_never_journals(web):
    before = _db_counts(web["state"])
    code, s = jget(web, "/api/scan?symbol=UPCO")
    assert code == 200 and s["ideas"] and s["mode"] == "fixtures"
    assert s["defined_only"] is True and "not financial advice" in s["disclaimer"].lower()
    for d in s["ideas"]:
        v = d["viz"]
        assert v["legs"] and v["plan"]["kind"] in ("credit", "debit") and v["plan"]["texit"]
        if v["plan"]["kind"] == "credit":
            assert v["plan"]["stop"] == pytest.approx(2.0)       # cost to close at 2x the credit
        else:
            assert 0 < v["plan"]["stop"] < 1
    assert _db_counts(web["state"]).get("ideas", 0) == before.get("ideas", 0) == 0
    assert _db_counts(web["state"]).get("snapshots", 0) == before.get("snapshots", 0)


def test_theme_scan(web):
    code, t = jget(web, "/api/themes")
    key = t["themes"][0]["key"]
    code, s = jget(web, f"/api/scan?theme={key}")
    assert code == 200 and s["theme"]["key"] == key


def _sim_query(v, **extra):
    q = {"legs": ",".join(v["legs"]), "spot": v["spot"], "symbol": v["symbol"], "n": 1000, "seed": 7,
         "tp": v["plan"]["tp"], "stop": v["plan"]["stop"], "texit": v["plan"]["texit"], **extra}
    return "/api/simulate?" + "&".join(f"{k}={v}" for k, v in q.items())


def test_simulate_scan_idea_reproducible(web):
    _, s = jget(web, "/api/scan?symbol=UPCO")
    v = s["ideas"][0]["viz"]
    code, a = jget(web, _sim_query(v))
    assert code == 200 and a["n"] == 1000 and a["stats"] and a["fan"] and a["histogram"]
    code, b = jget(web, _sim_query(v))
    assert a == b                                                # same seed, same answer


def test_simulate_with_event(web):
    """Regression: the cache key held the Event dataclass (unhashable): every event simulation was a 500."""
    _, s = jget(web, "/api/scan?symbol=EARN")
    v = next(d["viz"] for d in s["ideas"] if d["viz"].get("event"))
    e = v["event"]
    code, out = jget(web, _sim_query(v, event=f"{e['date']}:{e['move']}"))
    assert code == 200 and out["stats"]


def test_golden(web):
    code, g = jget(web, "/api/golden")
    assert code == 200 and g


def test_no_secret_in_any_response(web):
    assert KEYS
    paths = ["/", "/api/meta", "/api/themes", "/api/ticker?symbol=UPCO", "/api/chain?symbol=UPCO",
             "/api/scan?symbol=UPCO", "/api/ticker?symbol=ZZZZ", "/api/nope"]
    for p in paths:
        body = req(web, p)[2].decode()
        assert SECRET not in body and "TESTSECRET" not in body, p


# ---------- front-end source rules ----------

def _static(*exts):
    return [p for p in sorted(STATIC.iterdir()) if p.suffix in exts]


ORDER = re.compile(r"place_?order|create_order|submit_?order|cancel_?order|modify_?order|/iserver/account/\S*order"
                   r"|/orders?\b|reqPlaceOrder|placeOrder", re.I)


def test_front_end_has_no_order_code():
    hits = [f"{p.name}:{i}" for p in _static(".js", ".html", ".css")
            for i, line in enumerate(p.read_text().splitlines(), 1) if ORDER.search(line)]
    assert hits == []


def test_front_end_builds_dom_safely():
    banned = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function|"
                        r"setTimeout\s*\(\s*['\"`]|<form", re.I)
    hits = [f"{p.name}:{i}" for p in _static(".js", ".html")
            for i, line in enumerate(p.read_text().splitlines(), 1) if banned.search(line)]
    assert hits == []
    html = (STATIC / "app.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and " style=" not in html and not re.search(r"\son\w+=", html)


def test_only_ui_js_fetches():
    users = [p.name for p in _static(".js") if re.search(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon",
                                                          p.read_text())]
    assert users == ["ui.js"]
    ui = (STATIC / "ui.js").read_text()
    assert ui.count("fetch(") == 1 and 'method: "GET"' in ui


def test_front_end_says_no_rolling_only():
    for p in _static(".js", ".html", ".css", ".json"):
        low = re.sub(r"no rolling", "", p.read_text().lower())
        assert not re.search(r"\broll", low), p.name
