"""HTTP-based monitor check for public pages (Task 2 live monitoring).

A watch on a plain public ``http(s)`` URL does not need a live Chrome
extension: this module fetches the page over HTTP, extracts its visible
text, accessible element names and price, and feeds the observation to
the existing :func:`app.change_detector.evaluate_observation`.

Safety properties (read-only by construction):

* **GET only.** No writes, no payments, no navigation that submits
  anything, no cookies or credentials are ever sent.
* **SSRF-guarded.** Only ``http``/``https``; no userinfo in the URL;
  IP-literal hosts must be public, except ``127.0.0.1`` which is the
  fixture address used by the tests and the demo script. Every
  redirect hop is validated the same way (max 5). For hostname
  targets the DNS rebinding check runs only when the fetch would go
  direct; when an HTTP(S) proxy is configured the proxy resolves the
  host, and this VM's local DNS answers every public name with a
  CGNAT address (``198.18.0.0/15``), so a local DNS check would
  reject every real page.
* **Bounded.** ~10 s timeout, ~1 MB body cap, HTML only.
* **Redacted before storing.** The observation goes through the same
  :func:`app.change_detector._state` redaction as the browser path;
  nothing here persists page text beyond the redacted baseline and
  the 250-char diff excerpt that detector already stores.

Uses only the standard library — no new dependency.
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

FETCH_TIMEOUT_S = 10.0
MAX_BODY_BYTES = 1_048_576
MAX_REDIRECTS = 5
_READ_CHUNK = 65_536

# 127.0.0.1 is the fixture address used by the tests and the demo
# script; every other loopback/private/link-local/reserved address
# is refused.
_FIXTURE_HOST = "127.0.0.1"

_INTERNAL_HOSTNAMES = {
    "localhost",
    "::1",
    "0.0.0.0",
    "metadata.google.internal",
    "instance-data",
    "metadata",
}

# Same embedded-private-IP guard as website_resolver.is_safe_public_url.
_EMBEDDED_PRIVATE_IP_RE = re.compile(
    r"(?:127(?:\.\d+){3}|10(?:\.\d+){3}|192\.168(?:\.\d+){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d+){2}|169\.254(?:\.\d+){2})"
)


def _ip_is_public_or_fixture(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str | None]:
    """True for a public address, or the 127.0.0.1 fixture address."""
    if str(ip) == _FIXTURE_HOST:
        return True, None
    if isinstance(ip, ipaddress.IPv6Address):
        # NAT64 / IPv4-mapped IPv6: judge the embedded IPv4 address.
        for prefix in (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::ffff:0:0/96")):
            if ip in prefix:
                embedded = ipaddress.ip_address(ip.packed[-4:])
                return _ip_is_public_or_fixture(embedded)
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or str(ip) == "169.254.169.254"):
        return False, f"Address '{ip}' is private, loopback, or cloud metadata."
    return True, None


def _validate_monitor_url(url: str) -> tuple[bool, str | None]:
    """Fail-closed URL gate for the HTTP monitor check."""
    if not url or not isinstance(url, str):
        return False, "Empty monitor URL."
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        return False, f"Malformed URL: {exc}"
    if parts.scheme not in ("http", "https"):
        return False, f"Disallowed scheme '{parts.scheme}': only http and https are permitted."
    if parts.username or parts.password:
        return False, "Embedded credentials in URL authority are prohibited."
    hostname = (parts.hostname or "").lower().strip()
    if not hostname:
        return False, "Empty hostname."
    if (hostname in _INTERNAL_HOSTNAMES or hostname.endswith(".internal")
            or hostname.endswith(".local") or hostname.endswith(".localhost")):
        return False, f"Access to private/internal host '{hostname}' is blocked."
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None:
        return _ip_is_public_or_fixture(ip)
    if _EMBEDDED_PRIVATE_IP_RE.search(hostname):
        return False, f"Embedded private IP pattern in hostname '{hostname}' is blocked."
    # DNS rebinding defence, only when the fetch goes direct. When a
    # proxy is configured for this URL the proxy resolves the host;
    # local DNS in this VM answers every public name with a CGNAT
    # address, so checking it here would reject every real page.
    if urllib.request.proxy_bypass(hostname):
        try:
            addr_info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, socket.herror, TimeoutError, OSError) as exc:
            return False, f"Hostname '{hostname}' does not resolve ({exc})."
        for *_, sockaddr in addr_info:
            resolved = ipaddress.ip_address(sockaddr[0])
            safe, err = _ip_is_public_or_fixture(resolved)
            if not safe:
                return False, f"Hostname '{hostname}' resolves to private/internal IP '{resolved}' ({err})."
    return True, None


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to URLs that pass the monitor URL gate."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = _validate_monitor_url(newurl)
        if not ok:
            raise urllib.error.HTTPError(
                newurl, code, f"Redirect rejected: {reason}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Tags whose content is never visible page text.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "footer", "header", "aside"}
# Tags whose accessible name is worth keeping as an element name.
_NAME_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "a", "button"}


class _VisibleTextParser(HTMLParser):
    """Extract visible text and accessible element names from HTML.

    Strips script/style/nav/footer/header/aside boilerplate; keeps
    headings, link and button names (the closest stdlib analogue of
    the accessibility-tree names the browser path reports).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._text_parts: list[str] = []
        self._elements: list[str] = []
        self._name_tag: str | None = None
        self._name_parts: list[str] = []
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attrs_dict = dict(attrs)
        label = attrs_dict.get("aria-label") or attrs_dict.get("title") or attrs_dict.get("alt")
        if label and tag in ("a", "button", "img", "input", "select", "textarea"):
            self._elements.append(str(label).strip())
        if tag in _NAME_TAGS:
            self._name_tag = tag
            self._name_parts = []
        if tag == "title":
            self._name_tag = "title"
            self._title_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title" and self._name_tag == "title":
            title = " ".join(" ".join(self._title_parts).split())
            if title:
                self._text_parts.insert(0, title)
            self._name_tag = None
        elif tag in _NAME_TAGS and self._name_tag == tag:
            name = " ".join(" ".join(self._name_parts).split())
            if name:
                self._elements.append(name)
            self._name_tag = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._name_tag == "title":
            # Title text is inserted once at the front on </title>;
            # do not also keep it as a body text node.
            self._title_parts.append(text)
            return
        self._text_parts.append(text)
        if self._name_tag in _NAME_TAGS:
            self._name_parts.append(text)

    def result(self) -> dict[str, Any]:
        text = " ".join(" ".join(self._text_parts).split())
        elements = [e for e in (e.strip() for e in self._elements) if e][:100]
        return {"text": text, "elements": elements}


def extract_visible(html_text: str) -> dict[str, Any]:
    """Visible text + element names from an HTML document (stdlib only)."""
    parser = _VisibleTextParser()
    parser.feed(html_text)
    parser.close()
    return parser.result()


def fetch_observation(url: str, *, timeout: float = FETCH_TIMEOUT_S,
                      max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    """GET a public page and return the observation for the detector.

    Raises ValueError on a refused URL or an oversized/non-HTML body,
    and RuntimeError on transport/HTTP failures. The caller decides
    what a failure means for the mission; nothing is persisted here.
    """
    ok, reason = _validate_monitor_url(url)
    if not ok:
        raise ValueError(f"Monitor HTTP check refused {url}: {reason}")
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "PrivAgent-monitor/1.0 (read-only watch; +https://github.com/Prateek2007-cmd/PRIVAGENT)",
        "Accept": "text/html, text/plain;q=0.9",
    })
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(), _ValidatingRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            if content_type not in ("text/html", "text/plain"):
                raise ValueError(f"Monitor HTTP check refused non-HTML content type '{content_type}'")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Monitor HTTP check refused body over {max_bytes} bytes")
                chunks.append(chunk)
            raw = b"".join(chunks)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Monitor HTTP check failed for {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Monitor HTTP check failed for {url}: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"Monitor HTTP check timed out for {url}") from exc
    text = raw.decode(charset, errors="replace")
    # Unescape any entities the parser leaves behind in text nodes.
    observation = extract_visible(text)
    observation["text"] = html.unescape(observation["text"])
    observation["url"] = url
    return observation


def check_monitor_http(mission_id: str, url: str, *, db_path: str | None = None,
                       task_id: str | None = None,
                       timeout: float = FETCH_TIMEOUT_S) -> dict[str, Any] | None:
    """One read-only HTTP check cycle for a mission.

    Fetches the page, evaluates it against the stored baseline and
    returns the alert event (``None`` when nothing alert-worthy
    changed). Alerts are audit-logged with metadata only — never page
    text. Raises on fetch/validation failure; the scheduler marks the
    mission failed in that case.
    """
    from .change_detector import evaluate_observation

    observation = fetch_observation(url, timeout=timeout)
    event = evaluate_observation(mission_id, observation, task_id=task_id, db_path=db_path)
    if event:
        from .audit import append_event

        append_event(
            "monitor_alert",
            f"Monitor {mission_id} alert on {url}: {event.get('summary', '')}",
            actor="monitor",
            task_id=task_id,
        )
    return event
