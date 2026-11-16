"""Domain allow-list + egress allow-list (Task 3: isolation).

Pure stdlib module — it must never import ``app.config`` (config imports
this module at startup, so an import cycle would break the app).

Semantics
---------
* An empty / unset allow-list means "not configured": navigation is
  allowed everywhere and the operator is warned in the logs at task
  creation. This keeps every pre-Task-3 behaviour (and test) working
  until an operator opts in. Setting ``PRIVAGENT_ALLOWED_DOMAINS``
  (or a per-task ``allowedDomains``) turns the list into a hard gate.
* A plain entry (``example.com``) matches the host itself *and* any
  subdomain (``www.example.com``), mirroring the loop's existing
  ``_host_matches`` helper. A wildcard entry (``*.example.com``)
  matches subdomains only — never the bare domain.
* Matching is on the registrable host only: scheme must be http(s),
  no embedded credentials, no CR/LF/backslash. Ports are ignored for
  matching (a task may use any port on an allowed host).
* Egress (backend outbound calls) is stricter: only loopback hosts
  (local gateways such as the DeepAI gateway on 127.0.0.1), the pinned
  planner-provider hosts, and hosts on the domain allow-list may be
  contacted. Anything else is rejected before any request is made.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

__all__ = [
    "PROVIDER_EGRESS_HOSTS",
    "CONNECTOR_EGRESS_HOSTS",
    "parse_allowed_domains",
    "host_matches_allowlist",
    "url_allowed",
    "check_url",
    "egress_allowed",
    "effective_allowlist",
]

# Pinned planner-provider hosts. These are constants in the provider
# implementations (Groq posts to api.groq.com; the Gemini SDK only talks
# to generativelanguage.googleapis.com), never user input.
PROVIDER_EGRESS_HOSTS = frozenset({
    "generativelanguage.googleapis.com",
    "api.groq.com",
})

# Task 14: pinned Google API hosts for the native connectors (Gmail,
# Calendar, Drive) and the OAuth token endpoints. A connector call
# passes these as ``extra_hosts`` to :func:`egress_allowed` — the
# connector's fixed ``origin`` is the only host ever contacted, never
# a host from user input or page content.
CONNECTOR_EGRESS_HOSTS = frozenset({
    "gmail.googleapis.com",
    "www.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
})

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"|\d{1,3}(?:\.\d{1,3}){3}"
    r"|[0-9a-f:]+:[0-9a-f:.]+)$"
)


def _clean_entry(raw: str) -> str:
    entry = (raw or "").strip().lower().rstrip(".")
    return entry


def parse_allowed_domains(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Parse an allow-list from env text or a JSON list.

    Accepts ``"example.com, *.example.org"`` or ``["example.com"]``.
    Returns a de-duplicated list in first-seen order. Raises ValueError
    on any entry that is not a bare hostname, IPv4 literal, or a
    ``*.``-prefixed wildcard — schemes, paths, ports, queries and a bare
    ``*`` are rejected so a typo can never silently widen the list.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p for p in raw.split(",")]
    else:
        parts = list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        entry = _clean_entry(str(part))
        if not entry:
            continue
        if entry == "*":
            raise ValueError(
                "allow-list entry '*' is not allowed: name the domains explicitly"
            )
        if "://" in entry or "/" in entry or "?" in entry or "#" in entry:
            raise ValueError(
                f"allow-list entry {part!r} must be a bare domain (no scheme, path, or query)"
            )
        if ":" in entry and not entry.startswith("*."):
            # A port (example.com:8080) or an IPv6 literal without brackets.
            # IPv6 literals are rejected: agent navigation targets are
            # ordinary hostnames, and a colon is almost always a typo.
            raise ValueError(
                f"allow-list entry {part!r} must be a bare domain (no port)"
            )
        host = entry[2:] if entry.startswith("*.") else entry
        if not host or not _HOST_RE.fullmatch(host):
            raise ValueError(f"allow-list entry {part!r} is not a valid domain")
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def host_matches_allowlist(host: str, domains: list[str] | tuple[str, ...] | None) -> bool:
    """True when *host* is covered by any entry in *domains*."""
    if not domains:
        return False
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for entry in domains:
        entry = _clean_entry(entry)
        if entry.startswith("*."):
            suffix = entry[2:]
            if host != suffix and host.endswith("." + suffix):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def _host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.username or parts.password:
        return None
    if any(ch in url for ch in "\r\n\t\\"):
        return None
    return (parts.hostname or "").lower() or None


def url_allowed(url: str | None, domains: list[str] | tuple[str, ...] | None) -> bool:
    """Navigation gate. An empty allow-list means "not configured" and
    allows everything (back-compat); a configured list is a hard gate."""
    if not domains:
        return True
    if not url:
        return False
    host = _host_of(url)
    if host is None:
        return False
    return host_matches_allowlist(host, domains)


def check_url(url: str | None, domains: list[str] | tuple[str, ...] | None) -> tuple[bool, str]:
    """Like :func:`url_allowed` but with a human-readable reason."""
    if not domains:
        return True, "no domain allow-list configured"
    if not url:
        return False, "navigation target is empty"
    host = _host_of(url)
    if host is None:
        # REVIEW (Task 10 audit): the raw URL used to be echoed here,
        # and the caller logs `why` at WARNING — a planner-proposed
        # non-http URL carrying a secret (or credentials) would land
        # verbatim in the log stream. The operator does not need the
        # URL to understand the denial; the scheme is enough.
        scheme = ""
        try:
            from urllib.parse import urlsplit as _split

            scheme = (_split(url).scheme or "")[:16]
        except Exception:
            pass
        return False, f"unsafe navigation target (scheme {scheme!r}; needs http(s), no credentials)"
    if host_matches_allowlist(host, domains):
        return True, f"host {host} is on the domain allow-list"
    return False, f"host {host} is not on the domain allow-list"


def egress_allowed(
    url: str | None,
    allowed_domains: list[str] | tuple[str, ...] | None = None,
    extra_hosts: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Backend egress gate: may the backend make an outbound call to *url*?

    Allowed: loopback hosts (local gateways), the pinned planner-provider
    hosts, hosts on the domain allow-list, and *extra_hosts* (e.g. a
    connector's API host, passed explicitly by the caller). Everything
    else — including any other public host — is rejected. This is the
    check a provider must pass before its API key leaves the process.
    """
    if not url:
        return False
    host = _host_of(url)
    if host is None:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    if host in PROVIDER_EGRESS_HOSTS:
        return True
    if host_matches_allowlist(host, allowed_domains):
        return True
    if extra_hosts and host_matches_allowlist(host, extra_hosts):
        return True
    return False


def effective_allowlist(
    per_task: list[str] | None,
    global_list: list[str] | tuple[str, ...] | None,
) -> list[str] | None:
    """Per-task list wins when explicitly supplied; otherwise the global
    list; ``None`` when neither is configured (allow-list not in use)."""
    if per_task is not None:
        return list(per_task)
    if global_list:
        return list(global_list)
    return None
