#!/usr/bin/env python3
"""Basic-auth reverse proxy that puts the loopback dashboard on a private network.

WHY THIS EXISTS
    app.py has no auth of its own. gunicorn serves it on 127.0.0.1:8899; if you have nginx,
    put basic auth there. If you don't (or can't edit the dashboard's unit), run this: it
    binds ONE address you choose (e.g. your tailscale IP), checks credentials, and forwards
    to loopback.

WHY THE AUTH IS NOT OPTIONAL
    The dashboard exposes POST {PREFIX}/api/send/<sid>, which types into live claude
    sessions. Unauthenticated, every device that can reach the port could drive every
    claude on the box.

WHY IT BINDS ONE ADDRESS AND NOT 0.0.0.0
    Binding your tailscale (or other private-network) address keeps the dashboard off the
    LAN and the internet; 0.0.0.0 would not.

RUN
    gunicorn --bind <your-private-ip>:8900 --workers 2 --threads 4 --timeout 60 \
             dashboard_proxy:app

    Credentials come from DASH_USER / DASH_PASS in .env. There are deliberately NO
    defaults: this file is committed, and a default password in a repo is a published
    password. Missing either one is a hard startup failure rather than a silent fallback
    — refusing to boot is the safe direction for something guarding session control.
"""

from __future__ import annotations

import base64
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, request

UPSTREAM = os.environ.get("DASH_UPSTREAM", "http://127.0.0.1:8899").rstrip("/")
USER = os.environ.get("DASH_USER", "")
PASSWORD = os.environ.get("DASH_PASS", "")
if not USER or not PASSWORD:
    raise RuntimeError(
        "DASH_USER and DASH_PASS must be set (see .env.example). Refusing to start: "
        "this proxy fronts POST /claudes/api/send/<sid>, which types into live claude "
        "sessions, so there is no safe default credential.")
REALM = "chert dashboard"
TIMEOUT = 60

# Hop-by-hop headers must not be forwarded in either direction (RFC 9110 §7.6.1).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}

app = Flask(__name__)


def authorized() -> bool:
    """Constant-time credential check against the Authorization header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except Exception:
        return False
    user, _, password = decoded.partition(":")
    # Compared as bytes: hmac.compare_digest raises TypeError on non-ASCII str, which
    # turned a crafted username into a 500 instead of a 401.
    # Both compared unconditionally — no early return on a bad username.
    return (hmac.compare_digest(user.encode("utf-8"), USER.encode("utf-8"))
            & hmac.compare_digest(password.encode("utf-8"), PASSWORD.encode("utf-8")))


def challenge() -> Response:
    return Response(
        "authentication required\n", 401,
        {"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})


@app.route("/", defaults={"path": ""},
           methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>",
           methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"])
def proxy(path: str) -> Response:
    if not authorized():
        return challenge()

    url = f"{UPSTREAM}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")

    # Authorization is consumed here and deliberately not forwarded upstream.
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP | {"authorization", "host"}}
    body = request.get_data() if request.method in ("POST", "PUT", "PATCH") else None

    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=request.method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload, status, out_headers = r.read(), r.status, r.headers.items()
    except urllib.error.HTTPError as e:
        # Pass upstream 4xx/5xx through verbatim rather than masking them as 502.
        payload, status, out_headers = e.read(), e.code, e.headers.items()
    except urllib.error.URLError as e:
        return Response(f"dashboard upstream unreachable: {e.reason}\n", 502,
                        {"Content-Type": "text/plain"})

    forwarded = [(k, v) for k, v in out_headers
                 if k.lower() not in HOP_BY_HOP | {"content-length"}]
    return Response(payload, status, forwarded)


@app.route("/_proxy/health")
def health() -> Response:
    """Unauthenticated liveness check — reveals nothing about sessions."""
    return Response("ok\n", 200, {"Content-Type": "text/plain"})
