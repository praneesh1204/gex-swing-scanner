"""Alert delivery (spec §7): console and log first; webhook / Telegram / email when configured.

Each optional sink reads its settings from the environment (.env, see .env.example) and is skipped with a
note when they're missing. Every outgoing text goes through logutil.redact, and errors are redacted too,
so a bot token in a URL never reaches a log. Sinks send *messages to you*; nothing reaches a broker.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests
from rich.console import Console
from rich.markup import escape

from ..logutil import redact
from .rules import Alert

log = logging.getLogger(__name__)
FOOTER = "Alert only: gexscan never places, changes or cancels orders. You act on it yourself."
STYLE = {"STOP": "bold red", "WARN": "yellow", "REGIME": "magenta", "INFO": "cyan"}


def _body(alerts: list[Alert]) -> str:
    return redact("\n".join(a.text() for a in alerts) + "\n\n" + FOOTER)


class ConsoleSink:
    name = "console"

    def __init__(self, con: Console | None = None):
        self.con = con or Console()

    def ready(self) -> tuple[bool, str]:
        return True, ""

    def send(self, alerts: list[Alert]) -> None:
        for a in alerts:
            st = STYLE.get(a.level, "")
            self.con.print(f"[{st}]{a.level:<6}[/{st}] [bold]{escape(redact(a.title))}[/bold]: {escape(redact(a.message))}")


class LogSink:
    """JSON lines, one per alert (append)."""
    name = "log"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def ready(self) -> tuple[bool, str]:
        return True, ""

    def send(self, alerts: list[Alert]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for a in alerts:
                f.write(redact(json.dumps(a.to_dict(), default=str)) + "\n")
        for a in alerts:
            log.info("alert %s", redact(a.text()))


class WebhookSink:
    """POST JSON to ALERT_WEBHOOK_URL. Carries `text` (Slack) and `content` (Discord) plus the structured alerts."""
    name = "webhook"

    def ready(self) -> tuple[bool, str]:
        return (True, "") if os.environ.get("ALERT_WEBHOOK_URL") else (False, "ALERT_WEBHOOK_URL not set")

    def send(self, alerts: list[Alert]) -> None:
        body = _body(alerts)
        payload = {"text": body, "content": body[:1900],
                   "alerts": [json.loads(redact(json.dumps(a.to_dict(), default=str))) for a in alerts]}
        r = requests.post(os.environ["ALERT_WEBHOOK_URL"], json=payload, timeout=10)
        r.raise_for_status()


class TelegramSink:
    name = "telegram"

    def ready(self) -> tuple[bool, str]:
        miss = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
        return (not miss, f"{', '.join(miss)} not set" if miss else "")

    def send(self, alerts: list[Alert]) -> None:
        url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
        r = requests.post(url, data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": _body(alerts)[:4000]}, timeout=10)
        r.raise_for_status()


class EmailSink:
    """SMTP: SMTP_HOST, SMTP_PORT (587 STARTTLS / 465 SSL), SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO."""
    name = "email"

    def ready(self) -> tuple[bool, str]:
        miss = [k for k in ("SMTP_HOST", "ALERT_EMAIL_TO") if not os.environ.get(k)]
        return (not miss, f"{', '.join(miss)} not set" if miss else "")

    def send(self, alerts: list[Alert]) -> None:
        e = os.environ
        msg = EmailMessage()
        top = alerts[0]
        msg["Subject"] = redact(f"gexscan: {top.level} {top.title}" + (f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""))
        msg["From"] = e.get("ALERT_EMAIL_FROM") or e.get("SMTP_USER") or "gexscan@localhost"
        msg["To"] = e["ALERT_EMAIL_TO"]
        msg.set_content(_body(alerts))
        port = int(e.get("SMTP_PORT") or 587)
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(e["SMTP_HOST"], port, timeout=15) as s:
            if port != 465:
                s.starttls()
            if e.get("SMTP_USER") and e.get("SMTP_PASSWORD"):
                s.login(e["SMTP_USER"], e["SMTP_PASSWORD"])
            s.send_message(msg)


def make_sinks(names: list[str], log_file: str | Path, con: Console | None = None) -> list:
    table = {"console": lambda: ConsoleSink(con), "log": lambda: LogSink(log_file), "webhook": WebhookSink,
             "telegram": TelegramSink, "email": EmailSink}
    out = []
    for n in names:
        if n not in table:
            log.warning("unknown alert sink %r (known: %s)", n, ", ".join(table))
            continue
        out.append(table[n]())
    return out


def deliver(alerts: list[Alert], sinks: list) -> dict[str, str]:
    """-> {sink: 'sent' | 'skipped: why' | 'error: ...'}. One failing sink never blocks the others."""
    status = {}
    for s in sinks:
        ok, why = s.ready()
        if not ok:
            status[s.name] = f"skipped: {why}"
            continue
        if not alerts:
            status[s.name] = "nothing to send"
            continue
        try:
            s.send(alerts)
            status[s.name] = "sent"
        except Exception as ex:  # network / auth problems: report, redacted, and carry on
            status[s.name] = redact(f"error: {type(ex).__name__}: {ex}")
            log.warning("alert sink %s failed: %s", s.name, status[s.name])
    return status
