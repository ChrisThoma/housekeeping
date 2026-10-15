"""Sign-in sessions: a signed cookie, so a browser (or a password manager) only has to sign in once.

A session token is "<first>.<issued>.<expires>.<signature>": when you signed in, when this copy of the token was
issued (it's re-issued as you use the app), and when it expires. The signature is an HMAC over those numbers with
a key derived from a random secret kept next to the database and from the configured user name and password, so
changing the password signs every browser out, and so does replacing the secret ("Sign out everywhere").
No session state is kept on the server, so signing out in one browser doesn't affect the others.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import db

log = logging.getLogger("uvicorn.error")

COOKIE = "hk_session"
# Marks a browser that has signed in before. Kept after signing out (not after "Sign out everywhere"), so that
# browser can still sign in while strangers are locked out for guessing; see main._attempt_keys.
DEVICE_COOKIE = "hk_device"
DEVICE_SECONDS = 400 * 24 * 3600  # the longest browsers keep a cookie
REMEMBER_SECONDS = 90 * 24 * 3600   # "Keep me signed in": 90 days, extended as you use the app
SHORT_SECONDS = 12 * 3600           # otherwise: until the browser closes, or 12 hours without using the app
MAX_AGE = 365 * 24 * 3600           # no session outlives this, however much it's used; then you sign in again

_TOKEN = re.compile(r"(\d{1,12})\.(\d{1,12})\.(\d{1,12})\.([0-9a-f]{64})")
_KEY_BYTES = 32
_keys: dict[Path, bytes] = {}
_lock = threading.Lock()


def _key_path() -> Path:
    return Path(db.DB_PATH).parent / "session.key"


def _write_key(path: Path) -> bytes:
    """Write a new random key in one step (temp file, then rename), readable only by this user."""
    key = secrets.token_bytes(_KEY_BYTES)
    tmp = path.with_name(f".session.key.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
        os.replace(tmp, path)
    except OSError as e:
        log.error("Couldn't save the sign-in key to %s (%s). Sign-ins will work until the app restarts. "
                  "Make the data folder writable to keep people signed in across restarts.", path, e)
    return key


def _secret() -> bytes:
    path = _key_path()
    with _lock:
        if path not in _keys:
            try:
                key = bytes.fromhex(path.read_text().strip())
            except (OSError, ValueError):
                key = b""
            _keys[path] = key if len(key) == _KEY_BYTES else _write_key(path)  # missing, empty or damaged: replace it
        return _keys[path]


def rotate():
    """Replace the key, which signs out every browser at once."""
    path = _key_path()
    with _lock:
        _keys[path] = _write_key(path)


def _signature(username: str, password: str, body: str, purpose: str = "session") -> str:
    # Separate keys per purpose, so a device mark can never be used as a session or the other way round.
    material = f"v2\0{username}\0{password}" if purpose == "session" else f"v2-{purpose}\0{username}"
    key = hmac.new(_secret(), material.encode(), hashlib.sha256).digest()
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def issue(username: str, password: str, seconds: int, first: int | None = None, now: float | None = None,
          purpose: str = "session") -> str:
    issued = int(now if now is not None else time.time())
    body = f"{first if first is not None else issued}.{issued}.{issued + seconds}"
    return f"{body}.{_signature(username, password, body, purpose)}"


def check(token: str, username: str, password: str, now: float | None = None,
          purpose: str = "session") -> tuple[int, int, int] | None:
    """Return (first, issued, expires) for a valid, current token; None otherwise."""
    m = _TOKEN.fullmatch(token)
    if not m:
        return None
    first, issued, expires = (int(g) for g in m.groups()[:3])
    expected = _signature(username, password, f"{first}.{issued}.{expires}", purpose)
    if not hmac.compare_digest(m.group(4).encode(), expected.encode()):
        return None
    now = now if now is not None else time.time()
    if expires <= now or (purpose == "session" and now - first > MAX_AGE):
        return None
    return first, issued, expires


def renewal(session: tuple[int, int, int], now: float | None = None) -> int | None:
    """If a session should be extended now, the length to extend it by; otherwise None.
    Remembered sessions are extended once a day, short ones once an hour, so regular use never expires them."""
    first, issued, expires = session
    seconds = REMEMBER_SECONDS if expires - issued > SHORT_SECONDS else SHORT_SECONDS
    every = 24 * 3600 if seconds == REMEMBER_SECONDS else 3600
    return seconds if (now if now is not None else time.time()) - issued > every else None
