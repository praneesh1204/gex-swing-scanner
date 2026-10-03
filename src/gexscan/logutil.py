"""Structured logging with secret redaction.

Every log record is formatted first and then scrubbed. The scrub masks the values of environment
variables that look like secrets (…_KEY, …_TOKEN, …_SECRET, …_PASSWORD), `apikey=`/`token=` style URL
parameters, and `Bearer` headers. Keys never reach the console, a log file or a report.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS)$", re.I)
PARAM_RE = re.compile(r"((?:api_?key|apikey|token|access_token|key|secret|password)=)[^&\s\"']+", re.I)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I)
MASK = "***"


def secret_values() -> list[str]:
    vals = [v for k, v in os.environ.items() if SECRET_ENV.search(k) and v and len(v) >= 6]
    return sorted(set(vals), key=len, reverse=True)


def redact(s: str) -> str:
    if not s:
        return s
    for v in secret_values():
        s = s.replace(v, MASK)
    s = PARAM_RE.sub(lambda m: m.group(1) + MASK, s)
    return BEARER_RE.sub(lambda m: m.group(1) + MASK, s)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg, record.args = redact(msg), None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
             "logger": record.name, "msg": record.getMessage()}
        for k in ("symbol", "strategy", "gate", "provider"):
            if hasattr(record, k):
                d[k] = getattr(record, k)
        if record.exc_text:
            d["exc"] = record.exc_text
        return json.dumps(d, default=str)


def setup_logging(verbose: bool = False, json_logs: bool = False, log_file: str | Path | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    fmt = JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    for noisy in ("yfinance", "urllib3", "matplotlib", "peewee"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
