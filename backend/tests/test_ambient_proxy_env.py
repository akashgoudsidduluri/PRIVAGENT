"""Regression: the app must import and build its HTTP clients even when the
ambient proxy environment is hostile.

A stock shell often exports no_proxy entries such as ``[::1]`` (IPv6
literals). httpx parses those entries into URL patterns when a default
``httpx.Client()`` is constructed, and ``[::1]`` breaks its port parser
(``InvalidURL: Invalid port: ':1]'``). Because ``app.connectors.oauth``
instantiates its manager at module level, one bad env var used to break
collection of every test module that imports the app.

PrivAgent's own clients must never route through an ambient proxy anyway
(the product exists to keep data from leaking to third parties), so every
default client is built with ``trust_env=False``. These tests poison the env
to prove the app survives it.
"""

import importlib

import httpx
import pytest


@pytest.fixture()
def poisoned_proxy_env(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.local:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.local:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("all_proxy", "http://proxy.local:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.local:3128")
    # The exact shape that breaks httpx's URLPattern parser: IPv6 literals.
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1],[fd8b:4f84:7d32:99::1]")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1],[fd8b:4f84:7d32:99::1]")


def test_poison_is_real_plain_client_breaks(poisoned_proxy_env):
    """Control: a default httpx.Client really does crash under this env,
    so the tests below passing is meaningful, not vacuous."""
    with pytest.raises(httpx.InvalidURL):
        httpx.Client(timeout=5)


def test_oauth_module_imports_under_poisoned_proxy_env(poisoned_proxy_env):
    import app.connectors.oauth as oauth_mod

    reloaded = importlib.reload(oauth_mod)
    assert reloaded.oauth_manager is not None
    assert reloaded.oauth_manager._client is not None


def test_oauth_manager_constructs_under_poisoned_proxy_env(poisoned_proxy_env):
    from app.connectors.oauth import OAuthManager

    manager = OAuthManager()
    assert manager._client is not None


def test_native_connector_constructs_under_poisoned_proxy_env(poisoned_proxy_env):
    from app.connectors.base import NativeConnector

    connector = NativeConnector(token="test-token")
    assert connector._client is not None


def test_provider_client_constructs_under_poisoned_proxy_env(poisoned_proxy_env):
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
    )
    assert provider._client is not None
