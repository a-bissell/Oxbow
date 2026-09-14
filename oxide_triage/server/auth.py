"""A shared-password gate for a publicly reachable deployment.

Switched on by ``OXBOW_PASSWORD``; unset, the app is open, as on a laptop or an air-gapped
site. It stops drive-by visitors, not a determined attacker, and it is not user
authentication: everyone shares one password and nothing records who signed in. On success the
browser gets a cookie holding an expiry and an HMAC of that expiry keyed from the password, so
there is no session store and nothing to persist across redeploys. The login page, its form,
and ``/api/health`` (for a load balancer's check) stay open; every other path redirects a
browser to the login page and answers an API call with 401.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

PASSWORD_ENV = "OXBOW_PASSWORD"
COOKIE = "oxbow_session"
TTL_S = 7 * 24 * 3600  # a reviewer who comes back within the week is not asked again
OPEN_PATHS = frozenset({"/login", "/logout", "/api/health"})
MAX_FAILURES = 5  # wrong passwords per address within WINDOW_S before a minute's wait
WINDOW_S = 60.0


# ---- the token -----------------------------------------------------------------------------


def _key(password: str) -> bytes:
    return hashlib.sha256(("oxbow-session:" + password).encode()).digest()


def make_token(password: str, now: float | None = None) -> str:
    """``<expiry>.<hmac>``: the expiry is plain so the browser's cookie and the server agree
    on it, the signature is what proves the server issued it."""
    exp = int((time.time() if now is None else now) + TTL_S)
    sig = hmac.new(_key(password), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def token_valid(token: str | None, password: str, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, _, sig = token.partition(".")
    if not exp_s.isdigit():
        return False
    if int(exp_s) <= (time.time() if now is None else now):
        return False
    expected = hmac.new(_key(password), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# ---- a small failure limiter ----------------------------------------------------------------


class Limiter:
    """Wrong passwords per client address in a sliding window. In-process: enough to make
    guessing slow on a single-task deployment, which is the only kind this gate is for."""

    def __init__(self, max_failures: int = MAX_FAILURES, window_s: float = WINDOW_S):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window_s]
        if recent:
            self._failures[ip] = recent
        else:
            self._failures.pop(ip, None)
        return recent

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return len(self._recent(ip, now)) >= self.max_failures

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._failures[ip] = self._recent(ip, now) + [now]

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


def client_ip(request: Request) -> str:
    """The first hop of X-Forwarded-For when a load balancer set it, else the peer."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def safe_next(value: str | None) -> str:
    """Only a path on this site; never a scheme, a host, or the login page itself."""
    if not value or not value.startswith("/") or value.startswith("//") or value.startswith("/login"):
        return "/"
    return value


# ---- the page ------------------------------------------------------------------------------


def login_page(error: str | None, next_path: str) -> str:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oxbow · sign in</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font: 15px/1.5 system-ui, sans-serif;
         background: Canvas; color: CanvasText; }}
  form {{ width: min(22rem, 90vw); padding: 2rem; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
         border-radius: 12px; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.4rem; letter-spacing: .01em; }}
  p {{ margin: 0 0 1.25rem; opacity: .75; }}
  label {{ display: block; font-weight: 600; margin-bottom: .4rem; }}
  input {{ width: 100%; box-sizing: border-box; font: inherit; padding: .55rem .7rem; border-radius: 8px;
          border: 1px solid color-mix(in srgb, CanvasText 30%, transparent); background: Field; color: FieldText; }}
  button {{ margin-top: 1rem; width: 100%; font: inherit; font-weight: 600; padding: .6rem; border-radius: 8px;
           border: 0; background: CanvasText; color: Canvas; cursor: pointer; }}
  .err {{ color: #c0392b; opacity: 1; margin: .75rem 0 0; }}
</style></head>
<body><form method="post" action="/login" autocomplete="off">
  <h1>Oxbow</h1>
  <p>Materials triage, ranked on public data. This demo is password-protected.</p>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autofocus required>
  <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
  <button type="submit">Sign in</button>
  {err}
</form></body></html>
"""


# ---- installation --------------------------------------------------------------------------


def install_auth(app: FastAPI, password: str | None = None) -> bool:
    """Gate ``app`` behind the shared password. Returns whether a gate was installed."""
    password = os.environ.get(PASSWORD_ENV, "") if password is None else password
    if not password:
        return False
    limiter = Limiter()

    @app.middleware("http")
    async def gate(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path in OPEN_PATHS or token_valid(request.cookies.get(COOKIE), password):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(next: str = "/") -> HTMLResponse:
        return HTMLResponse(login_page(None, safe_next(next)))

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_submit(request: Request) -> Response:
        # Parsed by hand: the form is one field, and Starlette's form parser needs an extra package.
        data = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        supplied = data.get("password", [""])[0]
        next_path = safe_next(data.get("next", ["/"])[0])
        ip = client_ip(request)
        if limiter.blocked(ip):
            return HTMLResponse(
                login_page("Too many attempts. Wait a minute and try again.", next_path), status_code=429
            )
        if hmac.compare_digest(supplied.encode(), password.encode()):
            limiter.clear(ip)
            resp: Response = RedirectResponse(next_path, status_code=303)
            resp.set_cookie(
                COOKIE,
                make_token(password),
                max_age=TTL_S,
                httponly=True,
                samesite="lax",
                secure=is_https(request),
                path="/",
            )
            return resp
        limiter.record_failure(ip)
        log.info("sign-in failed from %s", ip)
        return HTMLResponse(login_page("That password is not right.", next_path), status_code=401)

    @app.get("/logout", include_in_schema=False)
    async def logout() -> Response:
        resp: Response = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    log.info("shared-password gate on (%s is set)", PASSWORD_ENV)
    return True
