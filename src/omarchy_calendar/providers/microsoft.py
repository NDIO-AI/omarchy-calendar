# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from ..http import HttpError
from ..models import Account, Calendar, Event
from ..normalize import extract_meeting_url, is_safe_https_url, plain_text


GRAPH = "https://graph.microsoft.com/v1.0"

_COLORS = {
    "auto": "#7aa2f7",
    "lightBlue": "#7dcfff",
    "lightGreen": "#9ece6a",
    "lightOrange": "#ff9e64",
    "lightGray": "#7982a9",
    "lightYellow": "#e0af68",
    "lightTeal": "#73daca",
    "lightPink": "#f7768e",
    "lightBrown": "#c0a36e",
    "lightRed": "#f7768e",
    "maxColor": "#bb9af7",
    "lightPurple": "#bb9af7",
    "darkBlue": "#2ac3de",
    "darkGreen": "#73daca",
    "darkOrange": "#ff9e64",
    "darkGray": "#565f89",
    "darkYellow": "#e0af68",
    "darkTeal": "#41a6b5",
    "darkPink": "#db4b4b",
    "darkBrown": "#9d7c61",
    "darkRed": "#db4b4b",
    "darkPurple": "#9d7cd8",
}

_WINDOWS_ZONES = {
    "UTC": timezone.utc,
    "Central Standard Time": ZoneInfo("America/Chicago"),
    "Eastern Standard Time": ZoneInfo("America/New_York"),
    "Mountain Standard Time": ZoneInfo("America/Denver"),
    "Pacific Standard Time": ZoneInfo("America/Los_Angeles"),
}


def _zone(name: str):
    mapped = _WINDOWS_ZONES.get(name)
    if mapped is not None:
        return mapped
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return timezone.utc


def local_timezone_name() -> str:
    configured = os.environ.get("TZ", "").lstrip(":")
    if configured:
        try:
            ZoneInfo(configured)
            return configured
        except (KeyError, ValueError):
            pass
    try:
        return str(Path("/etc/localtime").resolve().relative_to("/usr/share/zoneinfo"))
    except (OSError, ValueError):
        return "UTC"


def _graph_time(value: dict[str, Any]) -> str:
    raw = str(value.get("dateTime") or "")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(str(value.get("timeZone") or "UTC")))
    return parsed.isoformat()


def _meeting_url(raw: dict[str, Any]) -> str:
    direct = str((raw.get("onlineMeeting") or {}).get("joinUrl") or "")
    if is_safe_https_url(direct):
        return direct
    legacy = str(raw.get("onlineMeetingUrl") or "")
    if is_safe_https_url(legacy):
        return legacy
    location = str((raw.get("location") or {}).get("displayName") or "")
    body = str((raw.get("body") or {}).get("content") or raw.get("bodyPreview") or "")
    return extract_meeting_url(location, body)


def normalize_microsoft_event(
    raw: dict[str, Any],
    account: Account,
    calendar: dict[str, Any],
) -> Event | None:
    if raw.get("isCancelled") is True:
        return None
    calendar_id = str(calendar.get("id") or "")
    event_id = str(raw.get("id") or "")
    organizer = (raw.get("organizer") or {}).get("emailAddress") or {}
    organizer_text = str(organizer.get("name") or organizer.get("address") or "")
    organizer_owned = (
        bool(raw["isOrganizer"])
        if "isOrganizer" in raw
        else str(organizer.get("address") or "").casefold() == account.label.casefold()
    )
    location = str((raw.get("location") or {}).get("displayName") or "")
    event_type = str(raw.get("type") or "")
    series_master = event_type == "seriesMaster"
    provider_url = str(raw.get("webLink") or "")
    if not is_safe_https_url(provider_url):
        provider_url = ""
    return Event(
        uid=f"microsoft:{account.account_id}:{calendar_id}:{event_id}",
        provider="microsoft",
        account_id=account.account_id,
        account_label=account.label,
        calendar_id=calendar_id,
        calendar_name=str(calendar.get("name") or "Outlook Calendar"),
        calendar_color=_COLORS.get(str(calendar.get("color") or "auto"), "#bb9af7"),
        title=str(raw.get("subject") or "Untitled event"),
        start=_graph_time(raw.get("start", {})),
        end=_graph_time(raw.get("end", {})),
        all_day=bool(raw.get("isAllDay")),
        status="confirmed",
        location=plain_text(location, 500),
        description=plain_text(str((raw.get("body") or {}).get("content") or raw.get("bodyPreview") or ""), 8192),
        organizer=organizer_text,
        meeting_url=_meeting_url(raw),
        provider_url=provider_url,
        updated=str(raw.get("lastModifiedDateTime") or ""),
        provider_event_id=event_id,
        revision=str(raw.get("changeKey") or ""),
        timezone=str((raw.get("start") or {}).get("timeZone") or "UTC"),
        recurrence_id=str(raw.get("seriesMasterId") or ""),
        event_type=(
            "occurrence" if event_type in ("occurrence", "exception")
            else "series" if series_master
            else "single"
        ),
        organizer_owned=organizer_owned,
        start_day=str((raw.get("start") or {}).get("dateTime") or "")[:10],
        end_day=str((raw.get("end") or {}).get("dateTime") or "")[:10],
        series_revision=str(raw.get("_seriesRevision") or (raw.get("changeKey") if series_master else "") or ""),
        series_start=str(raw.get("_seriesStart") or (_graph_time(raw.get("start", {})) if series_master else "")),
        series_end=str(raw.get("_seriesEnd") or (_graph_time(raw.get("end", {})) if series_master else "")),
        has_attendees=bool(raw.get("attendees")),
    )


def normalize_microsoft_calendar(
    raw: dict[str, Any], account: Account, timezone_name: str = "UTC",
) -> Calendar:
    owner = raw.get("owner") or {}
    owner_address = str(owner.get("address") or "")
    allowed = [
        str(item) for item in raw.get("allowedOnlineMeetingProviders", [])
        if item and item != "unknown"
    ]
    preferred = str(raw.get("defaultOnlineMeetingProvider") or "")
    providers = ([preferred] if preferred in allowed else []) + [
        item for item in allowed if item != preferred
    ]
    return Calendar(
        provider="microsoft",
        account_id=account.account_id,
        account_label=account.label,
        calendar_id=str(raw.get("id") or ""),
        name=str(raw.get("name") or "Outlook Calendar"),
        color=_COLORS.get(str(raw.get("color") or "auto"), "#bb9af7"),
        timezone=timezone_name,
        writable=bool(raw.get("canEdit")),
        owned=bool(owner_address) and owner_address.casefold() == account.label.casefold(),
        meeting_providers=tuple(providers),
    )


class MicrosoftProvider:
    def __init__(self, http: Any, *, timezone: str | None = None):
        self.http = http
        self.timezone = timezone or local_timezone_name()

    def fetch_window(self, token: str, start: str, end: str) -> tuple[Account, list[Calendar], list[Event]]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Prefer": f'outlook.timezone="{self.timezone}"',
        }
        identity = self.http.get_json(
            f"{GRAPH}/me?{urlencode({'$select': 'id,displayName,mail,userPrincipalName'})}",
            headers=headers,
        )
        account = Account(
            provider="microsoft",
            account_id=str(identity["id"]),
            label=str(identity.get("mail") or identity.get("userPrincipalName") or identity.get("displayName") or "Outlook"),
        )
        raw_calendars = self._calendars(headers)
        calendars = [normalize_microsoft_calendar(item, account, self.timezone) for item in raw_calendars]
        events: list[Event] = []
        for calendar in raw_calendars:
            events.extend(self._events(calendar, account, headers, start, end))
        return account, calendars, events

    def _calendars(self, headers: dict[str, str]) -> list[dict[str, Any]]:
        fields = "id,name,color,canEdit,owner,allowedOnlineMeetingProviders,defaultOnlineMeetingProvider"
        url = f"{GRAPH}/me/calendars?{urlencode({'$select': fields})}"
        items: list[dict[str, Any]] = []
        while url:
            payload = self.http.get_json(url, headers=headers)
            items.extend(payload.get("value", []))
            url = str(payload.get("@odata.nextLink") or "")
        return items

    def _events(
        self,
        calendar: dict[str, Any],
        account: Account,
        headers: dict[str, str],
        start: str,
        end: str,
    ) -> list[Event]:
        calendar_id = quote(str(calendar["id"]), safe="")
        fields = (
            "id,subject,start,end,isAllDay,isCancelled,body,bodyPreview,location,"
            "organizer,isOrganizer,onlineMeeting,onlineMeetingUrl,webLink,showAs,"
            "lastModifiedDateTime,type,seriesMasterId,changeKey,recurrence,attendees"
        )
        query = urlencode({
            "startDateTime": start,
            "endDateTime": end,
            "$select": fields,
            "$top": "1000",
        })
        url = f"{GRAPH}/me/calendars/{calendar_id}/calendarView?{query}"
        raw_events: list[dict[str, Any]] = []
        while url:
            payload = self.http.get_json(url, headers=headers)
            raw_events.extend(payload.get("value", []))
            url = str(payload.get("@odata.nextLink") or "")
        masters: dict[str, tuple[str, str, str]] = {}
        # ponytail: one read per visible series; batch/cache only if provider quota becomes measurable.
        for series_id in dict.fromkeys(str(item.get("seriesMasterId") or "") for item in raw_events):
            if not series_id:
                continue
            try:
                master = self.http.get_json(
                    f"{GRAPH}/me/calendars/{calendar_id}/events/{quote(series_id, safe='')}?{urlencode({'$select': 'id,changeKey,start,end'})}",
                    headers=headers,
                )
                masters[series_id] = (
                    str(master.get("changeKey") or ""),
                    _graph_time(master.get("start", {})),
                    _graph_time(master.get("end", {})),
                )
            except HttpError:
                pass
        events: list[Event] = []
        for item in raw_events:
            raw = dict(item)
            master = masters.get(str(raw.get("seriesMasterId") or ""), ("", "", ""))
            raw["_seriesRevision"], raw["_seriesStart"], raw["_seriesEnd"] = master
            event = normalize_microsoft_event(raw, account, calendar)
            if event is not None:
                events.append(event)
        return events
