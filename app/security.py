"""Password hashing, session tokens, TOTP and in-memory rate limiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from collections import defaultdict, deque

SCRYPT_N_LOG2 = 15


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** SCRYPT_N_LOG2, r=8, p=1, dklen=32, maxmem=2 ** 27)
    return f"scrypt${SCRYPT_N_LOG2}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_log2, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=2 ** int(n_log2), r=8, p=1,
            dklen=len(expected), maxmem=2 ** 27,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- TOTP (RFC 6238, SHA-1, 6 digits, 30 s) ---------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode()


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def verify_totp(secret: str, code: str, last_counter: int = 0) -> int | None:
    """Return the matched time counter (to be stored, preventing reuse) or None."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    current = int(time.time()) // 30
    for counter in (current, current - 1, current + 1):
        if counter > last_counter and hmac.compare_digest(_totp_at(secret, counter), code):
            return counter
    return None


def totp_uri(secret: str, username: str, issuer: str = "Stash") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}"


# --- rate limiting -----------------------------------------------------------

class RateLimiter:
    """Sliding-window counter kept in memory (the app runs as a single worker)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, window: float) -> deque[float]:
        hits = self._hits[key]
        cutoff = time.monotonic() - window
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def blocked(self, key: str, limit: int, window: float) -> bool:
        blocked = len(self._prune(key, window)) >= limit
        if not self._hits[key]:
            del self._hits[key]
        return blocked

    def hit(self, key: str) -> None:
        self._hits[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


limiter = RateLimiter()
