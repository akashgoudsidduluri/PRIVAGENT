"""Regression tests for the connector fixes (review of Task 14).

Every test here is proven to fail on the pre-fix code: the review
findings were real, and these pin the fixed behaviour.
"""
import httpx
import pytest

from app.connectors import GmailConnector, GoogleCalendarConnector, GoogleDriveConnector
from app.connectors.base import ConnectorError
from app.connectors.oauth import _PROVIDER


def _client(responder):
    return httpx.Client(transport=httpx.MockTransport(responder))


# R1 — the Google scopes must cover reading existing Drive files.
def test_google_scopes_include_drive_readonly_for_existing_files():
    scopes = _PROVIDER["google"]["scopes"]
    assert "https://www.googleapis.com/auth/drive.readonly" in scopes
    assert "https://www.googleapis.com/auth/drive.file" in scopes  # uploads keep file scope


# R2 — Drive 302 redirects are followed once, to a trusted host, token-free.
def test_drive_export_follows_googleusercontent_redirect_without_token():
    seen = []

    def respond(request):
        seen.append(request)
        if "googleusercontent.com" in request.url.host:
            assert "Authorization" not in request.headers  # signed URL carries its own auth
            return httpx.Response(200, content=b"doc text", headers={"content-type": "text/plain"})
        return httpx.Response(302, headers={"location": "https://download.googleusercontent.com/file1"})

    conn = GoogleDriveConnector("token", client=_client(respond))
    assert conn.extract_text("F1", "application/vnd.google-apps.document") == "doc text"
    assert len(seen) == 2


def test_drive_redirect_to_untrusted_host_fails_honestly():
    def respond(request):
        return httpx.Response(302, headers={"location": "https://evil.example.com/file1"})

    with pytest.raises(ConnectorError, match="untrusted host"):
        GoogleDriveConnector("token", client=_client(respond)).extract_text(
            "F1", "application/vnd.google-apps.document")


def test_drive_redirect_without_location_and_on_post_fails_honestly():
    def respond(request):
        return httpx.Response(302)

    with pytest.raises(ConnectorError, match="without a Location"):
        GoogleDriveConnector("token", client=_client(respond)).extract_text(
            "F1", "application/vnd.google-apps.document")
    with pytest.raises(ConnectorError, match="redirect refused"):
        GmailConnector("token", client=_client(respond)).trash_message("M1")


def test_drive_redirect_requires_https_and_refuses_a_second_redirect():
    def http_scheme(request):
        return httpx.Response(302, headers={"location": "http://download.googleusercontent.com/file1"})

    with pytest.raises(ConnectorError, match="unsupported scheme"):
        GoogleDriveConnector("token", client=_client(http_scheme)).extract_text(
            "F1", "application/vnd.google-apps.document")

    def second_redirect(request):
        if "googleusercontent.com" in request.url.host:
            return httpx.Response(302, headers={"location": "https://download.googleusercontent.com/file2"})
        return httpx.Response(302, headers={"location": "https://download.googleusercontent.com/file1"})

    with pytest.raises(ConnectorError, match="second redirect"):
        GoogleDriveConnector("token", client=_client(second_redirect)).extract_text(
            "F1", "application/vnd.google-apps.document")


def test_none_query_does_not_crash_search():
    def respond(request):
        return httpx.Response(200, json={"threads": [], "files": []})

    assert GmailConnector("token", client=_client(respond)).search_threads(None) == []
    assert GoogleDriveConnector("token", client=_client(respond)).search_documents(None) == []


# R3 — summarize_messages uses the list endpoint (snippet included), not N+1 threads.
def test_summarize_messages_uses_list_not_threads():
    seen = []

    def respond(request):
        seen.append(request)
        url = str(request.url)
        if "/messages?" in url and "maxResults=2" in url:
            return httpx.Response(200, json={"messages": [
                {"id": "M1", "threadId": "T1", "snippet": "snip1"},
                {"id": "M2", "threadId": "T2", "snippet": "snip2"}]})
        if "/messages/M1" in url:
            return httpx.Response(200, json={"payload": {"headers": [
                {"name": "From", "value": "a@x.com"}, {"name": "Subject", "value": "Hello"}]}})
        if "/messages/M2" in url:
            return httpx.Response(200, json={"payload": {"headers": [
                {"name": "From", "value": "b@x.com"}, {"name": "Subject", "value": "World"}]}})
        raise AssertionError(f"unexpected request: {url}")

    got = GmailConnector("token", client=_client(respond)).summarize_messages("report", 2)
    assert got == [{"thread_id": "T1", "from": "a@x.com", "subject": "Hello", "snippet": "snip1"},
                   {"thread_id": "T2", "from": "b@x.com", "subject": "World", "snippet": "snip2"}]
    assert not any("/threads" in str(r.url) for r in seen)


# R4 — a failed refresh is a distinct error, not a generic 401.
def _refresh_client(hook_result, retry_status=200):
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401)
        return httpx.Response(retry_status, json={"emailAddress": "me@example.com"})

    return _client(respond), calls


def test_refresh_returning_nothing_or_same_token_is_distinct():
    for bad in (None, "old-token"):
        client, _ = _refresh_client(bad)
        conn = GmailConnector("old-token", client=client, refresh_hook=lambda: bad)
        with pytest.raises(ConnectorError, match="token refresh did not return a new token"):
            conn.get_profile()


def test_retry_still_401_after_refresh_is_distinct():
    client, _ = _refresh_client("new-token", retry_status=401)
    conn = GmailConnector("old-token", client=client, refresh_hook=lambda: "new-token")
    with pytest.raises(ConnectorError, match="after token refresh"):
        conn.get_profile()


def test_successful_refresh_retries_and_returns():
    client, calls = _refresh_client("new-token")
    conn = GmailConnector("old-token", client=client, refresh_hook=lambda: "new-token")
    assert conn.get_profile() == {"emailAddress": "me@example.com"}
    assert calls["n"] == 2


# R5 — get_thread takes a format; metadata is the default.
def test_get_thread_format_param():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "T1", "messages": []})

    conn = GmailConnector("token", client=_client(respond))
    conn.get_thread("T1", format="full")
    assert "format=full" in str(seen[0].url)
    conn.get_thread("T1")
    assert "format=metadata" in str(seen[1].url)


# R6 — Drive search exposes the next page.
def test_search_documents_page_returns_next_page_token():
    def respond(request):
        return httpx.Response(200, json={"files": [{"id": "F1"}], "nextPageToken": "tok"})

    conn = GoogleDriveConnector("token", client=_client(respond))
    assert conn.search_documents_page("report") == {"files": [{"id": "F1"}], "nextPageToken": "tok"}
    assert conn.search_documents("report") == [{"id": "F1"}]


# R7 — a caller-supplied Authorization header can never override the token.
def test_caller_headers_cannot_override_authorization():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"emailAddress": "me@example.com"})

    conn = GmailConnector("real-token", client=_client(respond))
    conn._request("GET", "/gmail/v1/users/me/profile", headers={"Authorization": "Bearer fake"})
    assert seen[0].headers["Authorization"] == "Bearer real-token"


# R8 — ? and # in a path are rejected like .. is.
def test_path_with_query_or_fragment_is_rejected():
    conn = GmailConnector("token", client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ConnectorError, match="unsafe API path"):
        conn._request("GET", "/gmail/v1/users/me/messages?x=1")
    with pytest.raises(ConnectorError, match="unsafe API path"):
        conn._request("GET", "/gmail/v1/users/me/messages#frag")


# R9 — Calendar rejects non-RFC3339 times with a named error, and sends reminders.
def test_calendar_rejects_non_rfc3339_times():
    conn = GoogleCalendarConnector("token", client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ConnectorError, match="RFC3339 start"):
        conn.list_events(start="tomorrow", end="2026-11-19T10:00:00+05:30")
    with pytest.raises(ConnectorError, match="RFC3339 end"):
        conn.create_invite("primary", "T", "2026-11-19T09:00:00+05:30", "tomorrow")


def test_create_invite_sends_reminders_when_given():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "E1"})

    conn = GoogleCalendarConnector("token", client=_client(respond))
    conn.create_invite("primary", "Interview", "2026-11-19T09:00:00+05:30", "2026-11-19T10:00:00+05:30",
                       reminders=[{"method": "popup", "minutes": 4320}, {"method": "popup", "minutes": 1440}])
    import json as _json
    body = _json.loads(seen[0].content)
    assert body["reminders"] == {"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 4320}, {"method": "popup", "minutes": 1440}]}
    with pytest.raises(ConnectorError, match="reminders must be"):
        conn.create_invite("primary", "T", "2026-11-19T09:00:00+05:30", "2026-11-19T10:00:00+05:30",
                           reminders=[{"method": "sms", "minutes": 10}])
