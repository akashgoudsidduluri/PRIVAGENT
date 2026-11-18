"""Second, web-blind checker + provenance tagging (Task 3: isolation).

The planner prompt already tells the model to treat page text as data,
but a prompt instruction is not a boundary: a sufficiently persuasive
page can still steer a plan. This module is the second layer, and it is
deliberately *not* a model:

* It never sees page prose as instructions. It sees only the *plan*
  (actions, targets, domains, the user's directive) plus a list of
  *findings* produced by scanning page text for instruction-like
  language. The scan is a deterministic regex heuristic — no LLM call,
  so it works with the mock provider and in tests.
* It runs in the agent loop before any consequential action executes.
  When it flags something, the loop halts for human confirmation
  (reusing the existing confirmation gate); with no gate available it
  fails the task instead of executing. It never silently rewrites a
  plan.

What it can and cannot do
-------------------------
It catches the common, loud injection shapes: "ignore previous
instructions", "reveal your system prompt", "send the password/OTP to
…", "click here to …", "navigate to <attacker domain>", credential
harvesting ("enter your password here"), and download-and-run lures.
It does *not* catch paraphrased, encoded, or subtly implied
instructions, and it cannot judge whether a benign-looking navigation
is what the user really wanted — that is why a flag means "ask the
human", never "block forever". A measured attack-success rate (ASR)
has not been established for this checker; do not claim one.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from .allowlist import host_matches_allowlist, url_allowed
from .privacy import redact_text_urls

__all__ = [
    "Finding",
    "CheckerResult",
    "scan_page_text",
    "check_plan",
    "tag_provenance",
    "INJECTION_RULES",
]

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


# (rule, severity, pattern). Patterns are deliberately narrow: page text
# legitimately contains words like "click" and "password" (e.g. a login
# form's own labels), so a finding needs the instruction *shape*, not
# just a keyword.
INJECTION_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "ignore_instructions",
        "high",
        _compile(r"ignore\s+(?:all\s+)?(?:previous|above|prior|earlier)\s+instructions"),
    ),
    (
        "reveal_system_prompt",
        "high",
        _compile(r"(?:reveal|print|repeat|show|output)\s+(?:your|the)\s+(?:system|developer|hidden)\s+(?:prompt|instructions|message)"),
    ),
    (
        "exfiltrate_secret",
        "high",
        _compile(r"\b(?:send|exfiltrate|upload|post|share|leak)\b[\s\S]{0,60}\b(?:password|passwd|secret|api[_\s-]?key|token|otp|one[_\s-]?time[_\s-]?code|credit[_\s-]?card)\b"),
    ),
    (
        "otp_exfiltration",
        "high",
        _compile(r"\b(?:otp|one[_\s-]?time[_\s-]?code|verification[_\s-]?code)\b[\s\S]{0,80}\b(?:send|share|message|text|paste|enter)\b"),
    ),
    (
        "credential_harvest",
        "high",
        _compile(r"\b(?:enter|paste|type|provide)\s+(?:your\s+)?(?:password|passwd|secret|api[_\s-]?key|otp|credit[_\s-]?card)\b"),
    ),
    (
        "navigate_instruction",
        "medium",
        _compile(r"\b(?:navigate\s+to|go\s+to|visit|open)\s+(?:https?://[^\s\"'<>]+|www\.[^\s\"'<>]+)"),
    ),
    (
        "click_instruction",
        "medium",
        _compile(r"\bclick\s+(?:here|this)\s+to\b[\s\S]{0,40}"),
    ),
    (
        "download_and_run",
        "high",
        _compile(r"\b(?:download|install|run|execute)\b[\s\S]{0,30}\b(?:\.exe|\.bat|\.sh|installer|script)\b"),
    ),
    (
        "disable_security",
        "high",
        _compile(r"\b(?:disable|turn\s+off|bypass)\b[\s\S]{0,30}\b(?:security|antivirus|firewall|protection|verification)\b"),
    ),
]


@dataclass
class Finding:
    """One instruction-like pattern found in untrusted page text."""

    rule: str
    severity: str
    snippet: str
    urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "snippet": self.snippet,
            "urls": list(self.urls),
        }


@dataclass
class CheckerResult:
    """Outcome of :func:`check_plan`."""

    flagged: bool
    reasons: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def requires_confirmation(self) -> bool:
        return self.flagged


def _redacted_snippet(text: str, start: int, end: int, *, context: int = 40) -> str:
    lo = max(0, start - context)
    hi = min(len(text), end + context)
    snippet = text[lo:hi].replace("\n", " ").strip()
    # Findings are shown to the operator in the confirmation gate, so
    # never let a URL in the snippet carry a live token.
    return redact_text_urls(snippet)[:160]


def scan_page_text(text: str | None, source_url: str = "") -> list[Finding]:
    """Scan untrusted page text for instruction-like language.

    Returns findings in rule order (not text order), one per rule at
    most — a page that repeats the same lure 50 times is one finding.
    An empty/None text yields no findings. The scan never raises on
    odd input; a failure to scan yields no findings and is the caller's
    problem to notice (the loop logs the page URL, not the text).
    """
    if not text or not isinstance(text, str):
        return []
    findings: list[Finding] = []
    for rule, severity, pattern in INJECTION_RULES:
        try:
            match = pattern.search(text)
        except Exception:
            continue
        if not match:
            continue
        urls = _URL_RE.findall(match.group(0))
        findings.append(
            Finding(
                rule=rule,
                severity=severity,
                snippet=_redacted_snippet(text, match.start(), match.end()),
                urls=urls,
            )
        )
    return findings


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _plan_nav_hosts(steps: list[dict[str, Any]]) -> list[str]:
    hosts: list[str] = []
    for step in steps or []:
        action = (step or {}).get("action") or {}
        if action.get("type") not in {"navigate", "new_tab"}:
            continue
        url = ((action.get("params") or {}).get("url")) or ""
        host = _host_of(url)
        if host:
            hosts.append(host)
    return hosts


def _plan_has_write(steps: list[dict[str, Any]]) -> bool:
    # Task 14: a WebMCP tool call can change site state and a UCP
    # checkout moves money — both count as writes for the
    # exfiltration-lure rule, exactly like type/click.
    write_types = {"click", "type", "press", "select_option", "set_date", "upload_file", "webmcp_tool", "ucp_checkout"}
    return any(
        ((step or {}).get("action") or {}).get("type") in write_types
        for step in steps or []
    )


def check_plan(
    instruction: str,
    steps: list[dict[str, Any]],
    findings: list[Finding],
    current_url: str = "",
    allowed_domains: list[str] | tuple[str, ...] | None = None,
) -> CheckerResult:
    """Web-blind plan check.

    The checker sees the plan (actions/targets/domains), the user's
    directive, and the scan *findings* — never the page prose itself.
    It flags when the plan does something the page text told it to do,
    or something the allow-list forbids. A benign plan on a benign
    page is never flagged: findings are required for every
    injection-shaped reason except the allow-list violation, which is
    enforced regardless of page content.
    """
    reasons: list[str] = []
    steps = steps or []

    # 1. Allow-list: enforced in code, not in the prompt. Any navigation
    #    to a disallowed host is flagged even on a clean page.
    if allowed_domains:
        for host in _plan_nav_hosts(steps):
            if not host_matches_allowlist(host, allowed_domains):
                reasons.append(
                    f"plan navigates to '{host}', which is not on the domain allow-list"
                )

    if findings:
        finding_rules = {f.rule for f in findings}
        nav_hosts = _plan_nav_hosts(steps)

        # 2. The plan follows a navigation instruction found in the page.
        instructed_hosts = set()
        for f in findings:
            if f.rule == "navigate_instruction":
                for u in f.urls:
                    h = _host_of(u)
                    if h:
                        instructed_hosts.add(h)
        for host in nav_hosts:
            if host in instructed_hosts:
                reasons.append(
                    f"plan navigates to '{host}', a destination named by an "
                    "instruction found in page content (possible prompt injection)"
                )

        # 3. Credential/secret exfiltration lure + a write action.
        if finding_rules & {"exfiltrate_secret", "otp_exfiltration", "credential_harvest"}:
            if _plan_has_write(steps):
                rules = sorted(finding_rules & {"exfiltrate_secret", "otp_exfiltration", "credential_harvest"})
                reasons.append(
                    "page contains a credential/secret exfiltration instruction "
                    f"({', '.join(rules)}) and the plan performs a write action "
                    "(type/click) — confirm before proceeding"
                )

        # 4. Instruction-override lure + navigation to a domain the user
        #    never named. (A research task legitimately navigates to new
        #    domains, so this only fires when the page also tried to
        #    override instructions.)
        if finding_rules & {"ignore_instructions", "reveal_system_prompt"}:
            named_hosts = set()
            for host in nav_hosts:
                if host and host in (instruction or "").lower():
                    named_hosts.add(host)
            for host in nav_hosts:
                if host not in named_hosts and host != _host_of(current_url):
                    reasons.append(
                        f"page contains an instruction-override attempt and the plan "
                        f"navigates to '{host}', a domain not named in your instruction"
                    )
                    break

        # 5. Download-and-run / disable-security lures are flagged on
        #    their own whenever the plan does anything but observe/wait:
        #    the page is actively trying to get the agent to do harm.
        if finding_rules & {"download_and_run", "disable_security"}:
            if _plan_has_write(steps) or nav_hosts:
                reasons.append(
                    "page contains a download-and-run / disable-security instruction "
                    "and the plan would act on this page — confirm before proceeding"
                )

    return CheckerResult(flagged=bool(reasons), reasons=reasons, findings=list(findings))


def tag_provenance(text: str, source_url: str = "", captured_at: str | None = None) -> str:
    """Wrap untrusted text in a provenance tag.

    The tag carries the source URL and capture time so a later reader
    (human or model) can tell *where* the text came from and that it is
    untrusted data, never an instruction. Content is HTML-escaped and
    passed through the existing untrusted-text cleanser so embedded
    instruction patterns are quarantined at the boundary.
    """
    from .adversarial_guard import cleanse_untrusted

    captured = captured_at or datetime.now(timezone.utc).isoformat()
    safe_url = html.escape(source_url or "", quote=True)
    try:
        cleaned = cleanse_untrusted(text or "")
    except Exception:
        cleaned = ""
    body = html.escape(cleaned)
    return (
        f'<untrusted_page_content source="page" url="{safe_url}" '
        f'captured="{html.escape(captured, quote=True)}" '
        f'trust="untrusted">{body}</untrusted_page_content>'
    )
