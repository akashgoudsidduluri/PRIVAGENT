"""Shared test harness for the backend suite.

Local auth (Task 1): every ``/api/*`` route requires a bearer token.
The suite therefore runs as the authenticated local operator by
default:

* ``PRIVAGENT_LOCAL_TOKEN`` is pinned to a fixed test value here, set
  before any test module imports ``app`` (pytest loads this conftest
  first). Tests must never depend on an ambient token.
* An autouse fixture patches ``TestClient.request`` so requests to
  ``/api/*`` paths automatically carry that token when the caller did
  not supply an ``Authorization`` header. This keeps the 40+ existing
  call sites (which test endpoint behaviour, not auth) unchanged and
  exercising the real, authenticated production path.
* Tests that verify auth itself (``test_auth.py``) use the
  ``disable_auto_auth`` fixture, which turns the injection off for
  that test so "no token" really means no token.

New tests that want to be explicit can use ``AUTH_HEADERS`` or the
``authed_client`` fixture instead of relying on the injection.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

# Fixed token for the whole suite. Set unconditionally: tests must be
# deterministic and must never pick up an operator's real token.
TEST_TOKEN = "test-local-token"
os.environ["PRIVAGENT_LOCAL_TOKEN"] = TEST_TOKEN

AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}

from app.main import app  # noqa: E402  (env token must be set first)


@pytest.fixture
def authed_client():
    """A TestClient that always sends the local operator token."""
    with TestClient(app) as client:
        yield client

# Read at request time (not fixture-setup time) so the
# ``disable_auto_auth`` fixture works regardless of fixture order.
_AUTO_AUTH = {"enabled": True}


@pytest.fixture
def disable_auto_auth(monkeypatch):
    """Turn off automatic auth injection for one test.

    Use in tests that must observe unauthenticated behaviour
    (missing/wrong token -> 401).
    """
    monkeypatch.setitem(_AUTO_AUTH, "enabled", False)
    yield


@pytest.fixture
def authed_client():
    """A TestClient that always sends the local operator token."""
    with TestClient(app) as client:
        yield client


def _is_api_path(url) -> bool:
    try:
        path = urlparse(str(url)).path
    except Exception:
        return False
    return path.startswith("/api/")


@pytest.fixture(autouse=True)
def _inject_local_auth(monkeypatch):
    """Authenticate TestClient requests to /api/* as the local operator.

    Only injects when the caller supplied no Authorization header, and
    only for /api/* paths. Health checks, websockets, and explicitly
    authenticated calls are untouched.
    """
    from starlette.testclient import TestClient as StarletteTestClient

    original_request = StarletteTestClient.request

    def request_with_auth(self, method, url, **kwargs):
        if _AUTO_AUTH["enabled"] and _is_api_path(url):
            headers = kwargs.get("headers")
            if headers is None:
                kwargs["headers"] = dict(AUTH_HEADERS)
            elif isinstance(headers, dict) and not any(
                k.lower() == "authorization" for k in headers
            ):
                kwargs["headers"] = {**headers, **AUTH_HEADERS}
        return original_request(self, method, url, **kwargs)

    monkeypatch.setattr(StarletteTestClient, "request", request_with_auth)
    yield
