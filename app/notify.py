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
    """Fire-and-forget mirror (signups, searches). Never blocks on a response."""
    url = config.SIGNUP_WEBHOOK_URL
    if not url:
        return
    try:
        requests.post(url, json={"ts": int(time.time()), **payload}, timeout=5)
    except Exception:
        pass


def request_webhook(payload: dict):
    """POST and return the parsed JSON response (or None). Used for saved-search
    save/delete, where we need the webhook's {ok, id} back. Fail-soft."""
    url = config.SIGNUP_WEBHOOK_URL
    if not url:
        return None
    try:
        r = requests.post(url, json={"ts": int(time.time()), **payload}, timeout=8)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def get_webhook(params: dict):
    """GET and return the parsed JSON response (or None). Used to list a user's
    saved searches. Fail-soft."""
    url = config.SIGNUP_WEBHOOK_URL
    if not url:
        return None
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None
