"""Request checks: loopback-only, per-launch token, DNS-rebinding and cross-site defences."""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

LOOPBACK = {"127.0.0.1", "::1", "localhost"}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def host_ok(host_header: str | None, port: int) -> bool:
    """Reject any Host that is not our own loopback address (defeats DNS rebinding)."""
    if not host_header:
        return False
    return host_header.lower() in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def origin_ok(origin_header: str | None, port: int) -> bool:
    """Browsers send Origin on cross-site and POST requests; a missing one (curl, plain GET) is fine."""
    if origin_header is None:
        return True
    try:
        parts = urlsplit(origin_header)
        return parts.scheme == "http" and parts.port == port and parts.hostname in LOOPBACK
    except ValueError:
        return False


def token_ok(authorization: str | None, expected: str) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:].strip().encode(), expected.encode())


def require_loopback(host: str) -> str:
    if host not in LOOPBACK:
        raise ValueError(f"refusing to bind {host!r}: this panel only listens on loopback (127.0.0.1)")
    return "127.0.0.1" if host == "localhost" else host
