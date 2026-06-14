"""Aide à la gestion de la session (cookie JWT wtcg_session).

wikitcg.net n'expose AUCUN endpoint de refresh (vérifié : /api/auth/refresh, /token,
/renew… → 404) et ne renvoie pas de cookie rafraîchi (pas de Set-Cookie). Le jeton est
un JWT Google-OAuth de ~14 jours. On ne peut donc pas le « refaire » par requête : il faut
se reconnecter dans le navigateur et coller le nouveau cookie. Ce module sert à :
  * lire l'expiration du JWT (sans vérif de signature — lecture des claims uniquement) ;
  * exposer un état « valide / expire dans X / expiré » pour l'UI et les bandeaux.
"""
from __future__ import annotations

import base64
import json
import time


def decode_jwt(token: str) -> dict:
    """Décode le payload d'un JWT sans vérifier la signature. {} si illisible."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def token_status(token: str) -> dict:
    """État synthétique du jeton pour l'UI."""
    token = token or ""
    placeholder = (not token) or ("PASTE_YOUR" in token)
    claims = decode_jwt(token)
    exp = claims.get("exp")
    now = int(time.time())
    out: dict = {
        "present": bool(token) and not placeholder,
        "email": claims.get("email"),
        "name": claims.get("name"),
        "exp": exp,
        "expires_in": (exp - now) if exp else None,
        "expired": (exp is not None and exp <= now),
    }
    return out
