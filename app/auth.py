"""Session helpers (the JWT wtcg_session cookie).

wikitcg.net exposes NO refresh endpoint (verified: /api/auth/refresh, /token,
/renew… → 404) and never returns a refreshed cookie (no Set-Cookie). The token is
a ~14-day Google-OAuth JWT. We therefore cannot "remint" it via a request: you have
to log in again in the browser and paste the new cookie. This module's job is to:
  * read the JWT's expiry (no signature check — claims only);
  * expose a "valid / expires in X / expired" state for the UI and banners.
"""
from __future__ import annotations

import base64
import json
import time

# Sample value shipped in config.example.toml / accounts.example.toml: a cookie
# containing this marker is NOT a real token (the user hasn't filled it in yet).
PLACEHOLDER = "PASTE_YOUR"


def is_real_session(token: str) -> bool:
    """True if `token` is a real session cookie (present and not the example placeholder)."""
    return bool(token) and PLACEHOLDER not in token


def decode_jwt(token: str) -> dict:
    """Decode a JWT payload without verifying the signature. {} if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def token_status(token: str) -> dict:
    """Summary state of the token for the UI."""
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
        "expired": (exp is not None and exp <= now),
    }
