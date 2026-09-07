# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..http import HttpError
from ..models import Account, Calendar, Event
from ..normalize import extract_meeting_url, is_recognized_meeting_url, is_safe_https_url, plain_text


GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _google_time(value: dict[str, Any], calendar: dict[str, Any]) -> tuple[str, bool]:
    if value.get("dateTime"):
        parsed = datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
        return parsed.isoformat(), False
    day = date.fromisoformat(str(value["date"]))
    zone = _zone(str(value.get("timeZone") or calendar.get("timeZone") or "UTC"))
    return datetime.combine(day, time.min, zone).isoformat(), True


def _conference_url(raw: dict[str, Any]) -> str:
    hangout = str(raw.get("hangoutLink") or "")
    if is_recognized_meeting_url(hangout):
        return hangout
    for entry in raw.get("conferenceData", {}).get("entryPoints", []):
        uri = str(entry.get("uri") or "")
        if entry.get("entryPointType") == "video" and is_safe_https_url(uri):
            return uri
    return extract_meeting_url(str(raw.get("location") or ""), str(raw.get("description") or ""))


def normalize_google_event(
    raw: dict[str, Any],
    account: Account,
    calendar: dict[str, Any],
) -> Event | None:
    if raw.get("status") == "cancelled":
        return None
    start, all_day = _google_time(raw.get("start", {}), calendar)
    end, _ = _google_time(raw.get("end", {}), calendar)
    event_id = str(raw.get("id") or "")
    calendar_id = str(calendar.get("id") or "")
    organizer = raw.get("organizer", {})
    organizer_text = str(organizer.get("displayName") or organizer.get("email") or "")
    recurring_id = str(raw.get("recurringEventId") or "")
    recurrence = tuple(str(item) for item in raw.get("recurrence", []) if item)
    series_master = bool(recurrence) and not recurring_id
    provider_url = str(raw.get("htmlLink") or "")
    if not is_safe_https_url(provider_url):
        provider_url = ""
    return Event(
        uid=f"google:{account.account_id}:{calendar_id}:{event_id}",
        provider="google",
        account_id=account.account_id,
        account_label=account.label,
        calendar_id=calendar_id,
        calendar_name=str(calendar.get("summary") or "Google Calendar"),
        calendar_color=str(calendar.get("backgroundColor") or "#7aa2f7"),
        title=str(raw.get("summary") or "Untitled event"),
        start=start,
        end=end,
        all_day=all_day,
        status=str(raw.get("status") or "confirmed"),
        location=plain_text(str(raw.get("location") or ""), 500),
        description=plain_text(str(raw.get("description") or ""), 8192),
        organizer=organizer_text,
        meeting_url=_conference_url(raw),
        provider_url=provider_url,
        updated=str(raw.get("updated") or ""),
        provider_event_id=event_id,
        revision=str(raw.get("etag") or ""),
        timezone=str((raw.get("start") or {}).get("timeZone") or calendar.get("timeZone") or "UTC"),
        recurrence_id=recurring_id,
        recurrence=recurrence,
        event_type=(
            "occurrence" if raw.get("recurringEventId")
            else "series" if raw.get("recurrence")
            else "single"
        ),
        organizer_owned=bool(organizer.get("self")) or str(organizer.get("email") or "").casefold() == account.label.casefold(),
        start_day=str((raw.get("start") or {}).get("date") or (raw.get("start") or {}).get("dateTime") or "")[:10],
        end_day=str((raw.get("end") or {}).get("date") or (raw.get("end") or {}).get("dateTime") or "")[:10],
        series_revision=str(raw.get("_seriesRevision") or (raw.get("etag") if series_master else "") or ""),
        series_start=str(raw.get("_seriesStart") or (start if series_master else "")),
        series_end=str(raw.get("_seriesEnd") or (end if series_master else "")),
        has_attendees=bool(raw.get("attendees")),
    )


def normalize_google_calendar(raw: dict[str, Any], account: Account) -> Calendar:
    access_role = str(raw.get("accessRole") or "reader")
    data_owner = str(raw.get("dataOwner") or "")
    providers = tuple(
        "googleMeet" if item == "hangoutsMeet" else str(item)
        for item in (raw.get("conferenceProperties") or {}).get("allowedConferenceSolutionTypes", [])
    )
    return Calendar(
        provider="google",
        account_id=account.account_id,
        account_label=account.label,
        calendar_id=str(raw.get("id") or ""),
        name=str(raw.get("summary") or "Google Calendar"),
        color=str(raw.get("backgroundColor") or "#7aa2f7"),
        timezone=str(raw.get("timeZone") or "UTC"),
        writable=access_role in ("writer", "owner"),
        owned=bool(raw.get("primary")) or bool(data_owner) and data_owner.casefold() == account.label.casefold(),
        meeting_providers=providers,
        sync_enabled=raw.get("selected") is not False,
    )


class GoogleProvider:
    def __init__(self, http: Any):
        self.http = http

    def fetch_window(self, token: str, start: str, end: str) -> tuple[Account, list[Calendar], list[Event]]:
        headers = {"Authorization": f"Bearer {token}"}
        identity = self.http.get_json(GOOGLE_USERINFO, headers=headers)
        account = Account(
            provider="google",
            account_id=str(identity["sub"]),
            label=str(identity.get("email") or "Google"),
        )
        raw_calendars = self._calendar_list(headers)
        calendars = [normalize_google_calendar(item, account) for item in raw_calendars]
        events: list[Event] = []
        for calendar, normalized in zip(raw_calendars, calendars):
            if calendar.get("deleted") is True:
                continue
            if not normalized.sync_enabled:
                continue
            events.extend(self._events(calendar, account, headers, start, end))
        return account, calendars, events

    def _calendar_list(self, headers: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query = {"maxResults": "250"}
            if page_token:
                query["pageToken"] = page_token
            payload = self.http.get_json(
                f"{GOOGLE_API}/users/me/calendarList?{urlencode(query)}",
                headers=headers,
            )
            items.extend(payload.get("items", []))
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return items

    def _events(
        self,
        calendar: dict[str, Any],
        account: Account,
        headers: dict[str, str],
        start: str,
        end: str,
    ) -> list[Event]:
        raw_events: list[dict[str, Any]] = []
        page_token = ""
        calendar_id = quote(str(calendar["id"]), safe="")
        while True:
            query = {
                "timeMin": start,
                "timeMax": end,
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": "2500",
            }
            if page_token:
                query["pageToken"] = page_token
            payload = self.http.get_json(
                f"{GOOGLE_API}/calendars/{calendar_id}/events?{urlencode(query)}",
                headers=headers,
            )
            raw_events.extend(payload.get("items", []))
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                break
        masters: dict[str, tuple[str, str, str]] = {}
        # ponytail: one read per visible series; batch/cache only if provider quota becomes measurable.
        for series_id in dict.fromkeys(str(item.get("recurringEventId") or "") for item in raw_events):
            if not series_id:
                continue
            try:
                master = self.http.get_json(
                    f"{GOOGLE_API}/calendars/{calendar_id}/events/{quote(series_id, safe='')}",
                    headers=headers,
                )
                master_start, _ = _google_time(master.get("start", {}), calendar)
                master_end, _ = _google_time(master.get("end", {}), calendar)
                masters[series_id] = (str(master.get("etag") or ""), master_start, master_end)
            except HttpError:
                pass
        events: list[Event] = []
        for item in raw_events:
            raw = dict(item)
            master = masters.get(str(raw.get("recurringEventId") or ""), ("", "", ""))
            raw["_seriesRevision"], raw["_seriesStart"], raw["_seriesEnd"] = master
            event = normalize_google_event(raw, account, calendar)
            if event is not None:
                events.append(event)
        return events
