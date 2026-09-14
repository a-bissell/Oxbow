"""The shared-password gate: off by default, and when on, everything but the login page and
the health endpoint waits for the cookie."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from oxide_triage.server.app import create_app
from oxide_triage.server.auth import COOKIE, Limiter, make_token, safe_next, token_valid

PASSWORD = "correct horse battery staple"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", str(tmp_path / "cache.sqlite"))
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", str(tmp_path / "site.yaml"))
    monkeypatch.setenv("OXIDE_TRIAGE_OFFLINE", "1")
    monkeypatch.setenv("OXIDE_TRIAGE_ADMIN", "0")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OXBOW_PASSWORD", raising=False)
    return monkeypatch


@pytest.fixture
def gated(env):
    env.setenv("OXBOW_PASSWORD", PASSWORD)
    return TestClient(create_app(), follow_redirects=False)


def test_off_by_default_nothing_changes(env):
    client = TestClient(create_app(), follow_redirects=False)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/").status_code == 200
    # No login route was installed: the catch-all serves the front end for that path too.
    assert client.get("/login").status_code == 200 and "Sign in" not in client.get("/login").text


def test_gate_redirects_browsers_and_refuses_api_calls(gated):
    r = gated.get("/?tab=results")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F%3Ftab%3Dresults"
    assert gated.get("/api/status").status_code == 401
    assert gated.get("/api/conversations").status_code == 401
    assert gated.get("/api/health").status_code == 200  # the load balancer's check stays open
    page = gated.get("/login?next=%2Fsomewhere")
    assert page.status_code == 200 and "Sign in" in page.text and 'value="/somewhere"' in page.text


def test_right_password_sets_a_cookie_that_opens_everything(gated):
    r = gated.post("/login", data={"password": PASSWORD, "next": "/somewhere"})
    assert r.status_code == 303 and r.headers["location"] == "/somewhere"
    cookie = r.cookies.get(COOKIE)
    assert cookie and token_valid(cookie, PASSWORD)
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert gated.get("/api/status").status_code == 200
    assert gated.get("/").status_code == 200
    # Sign out clears it.
    assert gated.get("/logout").status_code == 303
    assert gated.get("/api/status").status_code == 401


def test_wrong_password_is_refused_and_then_limited(gated):
    for _ in range(5):
        r = gated.post("/login", data={"password": "nope", "next": "/"})
        assert r.status_code == 401 and "not right" in r.text
    r = gated.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 429 and "Too many" in r.text  # even the right one waits now
    assert COOKIE not in r.cookies


def test_forged_and_expired_cookies_are_rejected(gated):
    gated.cookies.set(COOKIE, "9999999999.deadbeef")
    assert gated.get("/api/status").status_code == 401
    gated.cookies.set(COOKIE, make_token("another password"))
    assert gated.get("/api/status").status_code == 401
    old = make_token(PASSWORD, now=0)  # issued at the epoch: long expired
    assert not token_valid(old, PASSWORD)
    gated.cookies.set(COOKIE, old)
    assert gated.get("/api/status").status_code == 401


def test_token_and_limiter_units():
    tok = make_token(PASSWORD, now=1_000_000)
    assert token_valid(tok, PASSWORD, now=1_000_000 + 60)
    assert not token_valid(tok, PASSWORD, now=1_000_000 + 8 * 24 * 3600)
    assert not token_valid(tok, "other", now=1_000_000)
    assert (
        not token_valid(None, PASSWORD)
        and not token_valid("junk", PASSWORD)
        and not token_valid("x.y", PASSWORD)
    )
    lim = Limiter(max_failures=2, window_s=10)
    lim.record_failure("a", now=0)
    assert not lim.blocked("a", now=1)
    lim.record_failure("a", now=2)
    assert lim.blocked("a", now=3) and not lim.blocked("b", now=3)
    assert not lim.blocked("a", now=13)  # the window has passed
    lim.clear("a")
    assert not lim.blocked("a", now=3)


def test_next_only_ever_points_at_this_site():
    assert safe_next("/results/abc") == "/results/abc"
    for bad in (None, "", "//evil.example", "http://evil.example", "/login?next=/", "relative"):
        assert safe_next(bad) == "/"
