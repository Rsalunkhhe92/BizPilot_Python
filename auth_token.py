"""
Signed login tokens.

/api/login hands the app a token proving which userdetails.id it logged in as;
APIs that opt in (currently the inventory API) require it, so a caller can no
longer read or change another owner's data just by sending a different owner_id.
"""

import hashlib
import hmac
import os
import time

TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def _secret():
    # AUTH_SECRET should be set in production. The fallback keeps tokens stable
    # across restarts in development without any extra setup.
    secret = os.getenv("AUTH_SECRET") or ("bizpilot:" + os.getenv("DATABASE_URL", ""))
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _sign(body):
    return hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(user_id):
    body = f"{int(user_id)}.{int(time.time()) + TOKEN_TTL_SECONDS}"
    return f"{body}.{_sign(body)}"


def verify_token(token):
    """Returns the user id the token was issued to, or None if it is missing,
    tampered with, or expired."""
    try:
        user_id, expires_at, signature = str(token or "").split(".")
        body = f"{user_id}.{expires_at}"
        if not hmac.compare_digest(signature, _sign(body)):
            return None
        if int(expires_at) < time.time():
            return None
        return int(user_id)
    except (ValueError, TypeError):
        return None
