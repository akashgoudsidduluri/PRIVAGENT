"""Google Calendar list, free/busy, and invite creation."""
from __future__ import annotations
from urllib.parse import quote
from .base import NativeConnector


class GoogleCalendarConnector(NativeConnector):
    origin = "https://www.googleapis.com"
    service = "Google Calendar"

    def list_events(self, calendar_id: str = "primary", *, start: str, end: str, limit: int = 50) -> list[dict]:
        data = self._request("GET", f"/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
                             params={"timeMin": start, "timeMax": end, "singleEvents": "true",
                                     "orderBy": "startTime", "maxResults": min(max(limit, 1), 250)})
        return data.get("items", [])

    def check_availability(self, start: str, end: str, calendar_ids: list[str]) -> dict:
        return self._request("POST", "/calendar/v3/freeBusy",
                             json={"timeMin": start, "timeMax": end, "items": [{"id": cid} for cid in calendar_ids[:20]]})

    def create_invite(self, calendar_id: str, title: str, start: str, end: str,
                      attendees: list[str] | None = None, timezone: str = "UTC") -> dict:
        return self._request("POST", f"/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
                             params={"sendUpdates": "all" if attendees else "none"},
                             json={"summary": title, "start": {"dateTime": start, "timeZone": timezone},
                                   "end": {"dateTime": end, "timeZone": timezone},
                                   "attendees": [{"email": email} for email in (attendees or [])]})
