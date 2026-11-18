"""Google Calendar list, free/busy, and invite creation."""
from __future__ import annotations
import re
from urllib.parse import quote
from .base import NativeConnector, ConnectorError

_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")


def _require_rfc3339(value: str, field: str) -> str:
    # Google 400s a non-RFC3339 timeMin/timeMax/dateTime with a generic
    # error; fail here instead, naming the field, before any API call.
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
        raise ConnectorError(f"Google Calendar requires RFC3339 {field} (e.g. 2026-11-19T09:00:00+05:30), got {value!r}")
    return value


class GoogleCalendarConnector(NativeConnector):
    origin = "https://www.googleapis.com"
    service = "Google Calendar"

    def list_events(self, calendar_id: str = "primary", *, start: str, end: str, limit: int = 50) -> list[dict]:
        _require_rfc3339(start, "start")
        _require_rfc3339(end, "end")
        data = self._request("GET", f"/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
                             params={"timeMin": start, "timeMax": end, "singleEvents": "true",
                                     "orderBy": "startTime", "maxResults": min(max(limit, 1), 250)})
        return data.get("items", [])

    def check_availability(self, start: str, end: str, calendar_ids: list[str]) -> dict:
        _require_rfc3339(start, "start")
        _require_rfc3339(end, "end")
        return self._request("POST", "/calendar/v3/freeBusy",
                             json={"timeMin": start, "timeMax": end, "items": [{"id": cid} for cid in calendar_ids[:20]]})

    def create_invite(self, calendar_id: str, title: str, start: str, end: str,
                      attendees: list[str] | None = None, timezone: str = "UTC",
                      reminders: list[dict] | None = None) -> dict:
        _require_rfc3339(start, "start")
        _require_rfc3339(end, "end")
        body: dict = {"summary": title, "start": {"dateTime": start, "timeZone": timezone},
                      "end": {"dateTime": end, "timeZone": timezone},
                      "attendees": [{"email": email} for email in (attendees or [])]}
        if reminders is not None:
            overrides = []
            for r in reminders:
                method = (r or {}).get("method")
                minutes = (r or {}).get("minutes")
                if method not in ("popup", "email") or not isinstance(minutes, int) or minutes < 0:
                    raise ConnectorError("reminders must be {method: 'popup'|'email', minutes: non-negative int}")
                overrides.append({"method": method, "minutes": minutes})
            body["reminders"] = {"useDefault": False, "overrides": overrides}
        return self._request("POST", f"/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
                             params={"sendUpdates": "all" if attendees else "none"}, json=body)
