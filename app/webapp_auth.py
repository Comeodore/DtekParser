"""Who opened the mini app, from Telegram's signed initData (core.telegram.org/bots/webapps#validating-data-
received-via-the-mini-app). The user id the page itself reports is never taken on trust."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qsl

# initData stays the same while the mini app is open; Telegram signs a new one each time it is opened.
MAX_AGE = timedelta(days=1)


def init_data_hash(fields: dict[str, str], bot_token: str) -> str:
    """The hash Telegram puts in initData: HMAC-SHA256 of every other field, sorted, one key=value a line,
    keyed with HMAC-SHA256("WebAppData", the bot's token)."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()) if k != "hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def webapp_user(init_data: str, bot_token: str, now: datetime, max_age: timedelta = MAX_AGE) -> Optional[dict]:
    """The Telegram user of a mini app's initData when the bot's token signed it within `max_age`; else None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    received = fields.get("hash")
    if len(fields) != len(pairs) or not received:
        return None
    if not hmac.compare_digest(init_data_hash(fields, bot_token).encode(), received.encode()):
        return None
    try:
        signed = int(fields["auth_date"])
        user = json.loads(fields["user"])
    except (KeyError, ValueError):
        return None
    age = now.timestamp() - signed
    if not -300 <= age <= max_age.total_seconds():
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user
