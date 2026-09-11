"""Session helpers for JWT wtcg_session cookie."""
from __future__ import annotations

import base64
import json
import time

PLACEHOLDER = "PASTE_YOUR"


def is_real_session(token: str) -> bool:
    return bool(token) and PLACEHOLDER not in token


def decode_jwt(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def token_status(token: str) -> dict:
    token = token or ""
    claims = decode_jwt(token)
    exp = claims.get("exp")
    now = int(time.time())
    return {
        "present": is_real_session(token),
        "email": claims.get("email"),
        "name": claims.get("name"),
        "exp": exp,
        "expires_in": (exp - now) if exp else None,
        "expired": (exp <= now) if exp is not None else None,
    }
