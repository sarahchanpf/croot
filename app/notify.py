"""Best-effort event mirror to the durable webhook (a Google Apps Script that
appends to a Sheet), so signups AND searches survive Vercel's ephemeral /tmp.

Never raises and never blocks meaningfully — a webhook hiccup must not break a
signup or a search. No-op when SIGNUP_WEBHOOK_URL is unset.
"""

from __future__ import annotations

import time

import requests

from . import config


def post_event(payload: dict) -> None:
    url = config.SIGNUP_WEBHOOK_URL
    if not url:
        return
    try:
        requests.post(url, json={"ts": int(time.time()), **payload}, timeout=5)
    except Exception:
        pass
