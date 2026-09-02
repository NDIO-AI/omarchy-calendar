# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cache import CalendarStore
from .http import HttpError, ReadOnlyHttp
from .keyring import SecretServiceStore
from .models import Account, Calendar, Event
from .normalize import is_safe_https_url
from .providers.google import GOOGLE_API, normalize_google_event
from .providers.microsoft import GRAPH, local_timezone_name, normalize_microsoft_event
from .settings import ProviderSettings
from .sync import SyncEngine
from .write_http import CalendarWriteHttp


WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MICROSOFT_WEEKDAYS = {
    "MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday",
    "FR": "friday", "SA": "saturday", "SU": "sunday",
}


class DraftError(ValueError):
    pass


class MutationConflict(DraftError):
    pass


@dataclass(frozen=True, slots=True)
class Recurrence:
    frequency: str = "none"
    weekdays: tuple[str, ...] = ()
    end: str = "never"
    count: int = 0
    until: str = ""


@dataclass(frozen=True, slots=True)
class EventDraft:
    request_id: str
    calendar_key: str
    title: str
    day: str
    start: str
    end: str
    all_day: bool
    location: str
    notes: str
    recurrence: Recurrence
    online_meeting: str
    source_uid: str
    scope: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EventDraft":
        if not isinstance(raw, Mapping):
            raise DraftError("Event draft must be a JSON object")
        try:
            request_id = str(uuid.UUID(str(raw.get("request_id") or "")))
        except ValueError as error:
            raise DraftError("Event draft request ID is invalid") from error
        calendar_key = str(raw.get("calendar_key") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", calendar_key):
            raise DraftError("Event draft calendar is invalid")
        title = _text(raw.get("title"), "title", 500, required=True)
        day = _date(raw.get("day"), "day")
        all_day = raw.get("all_day") is True
        start = str(raw.get("start") or "")
        end = str(raw.get("end") or "")
        if not all_day:
            start = _time(start)
            end = _time(end)
            if end <= start:
                raise DraftError("Event end must be after its start")
        else:
            start = ""
            end = ""
        recurrence = _recurrence(raw.get("recurrence"), day)
        online_meeting = str(raw.get("online_meeting") or "none")
        if online_meeting not in ("none", "preserve", "new"):
            raise DraftError("Event meeting option is invalid")
        scope = str(raw.get("scope") or "single")
        if scope not in ("single", "series"):
            raise DraftError("Event recurrence scope is invalid")
        return cls(
            request_id=request_id,
            calendar_key=calendar_key,
            title=title,
            day=day,
            start=start,
            end=end,
            all_day=all_day,
            location=_text(raw.get("location"), "location", 500),
            notes=_text(raw.get("notes"), "notes", 8192),
            recurrence=recurrence,
            online_meeting=online_meeting,
            source_uid=_text(raw.get("source_uid"), "source event", 2048),
            scope=scope,
        )


def _text(value: object, name: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if "\0" in text or len(text) > limit or (required and not text):
        raise DraftError(f"Event {name} is invalid")
    return text


def _date(value: object, name: str) -> str:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError as error:
        raise DraftError(f"Event {name} is invalid") from error


def _time(value: object) -> str:
    try:
        return datetime.strptime(str(value or ""), "%H:%M").strftime("%H:%M")
    except ValueError as error:
        raise DraftError("Event time is invalid") from error


def _recurrence(value: object, start_day: str) -> Recurrence:
    raw = value if isinstance(value, Mapping) else {}
    frequency = str(raw.get("frequency") or "none")
    if frequency not in ("preserve", "none", "daily", "weekdays", "weekly", "monthly", "selected_weekdays"):
        raise DraftError("Event recurrence frequency is invalid")
    supplied = raw.get("weekdays") if isinstance(raw.get("weekdays"), list) else []
    weekdays = tuple(dict.fromkeys(str(item).upper() for item in supplied))
    if any(item not in WEEKDAYS for item in weekdays):
        raise DraftError("Event recurrence weekday is invalid")
    if frequency == "weekdays":
        weekdays = WEEKDAYS[:5]
    elif frequency == "weekly":
        weekdays = (WEEKDAYS[date.fromisoformat(start_day).weekday()],)
    elif frequency == "selected_weekdays" and not weekdays:
        raise DraftError("Event recurrence needs at least one weekday")
    elif frequency not in ("weekdays", "weekly", "selected_weekdays"):
        weekdays = ()
    ending = str(raw.get("end") or "never")
    if ending not in ("never", "count", "date"):
        raise DraftError("Event recurrence ending is invalid")
    count = 0
    until = ""
    if ending == "count":
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError) as error:
            raise DraftError("Event recurrence count is invalid") from error
        if not 1 <= count <= 999:
            raise DraftError("Event recurrence count is invalid")
    elif ending == "date":
        until = _date(raw.get("until"), "recurrence end date")
        if until < start_day:
            raise DraftError("Event recurrence end date is before its start")
    if frequency in ("preserve", "none") and ending != "never":
        raise DraftError("A non-recurring event cannot have a recurrence ending")
    return Recurrence(frequency, weekdays, ending, count, until)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _local_datetime(day: str, value: str, zone: str) -> datetime:
    local = datetime.combine(date.fromisoformat(day), time.fromisoformat(value))
    local_zone = _zone(zone)
    candidates = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=local_zone, fold=fold)
        if candidate.astimezone(timezone.utc).astimezone(local_zone).replace(tzinfo=None) == local:
            candidates.append(candidate)
    offsets = {candidate.utcoffset() for candidate in candidates}
    if not candidates or len(offsets) > 1:
        raise DraftError("Event time is ambiguous or unavailable due to daylight saving time")
    return candidates[0]


def _notes_with_meeting(notes: str, meeting_url: str, mode: str) -> str:
    if mode != "preserve" or not is_safe_https_url(meeting_url) or meeting_url in notes:
        return notes
    return (notes + "\n\n" if notes else "") + f"Meeting: {meeting_url}"


def google_event_id(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("ascii")).digest()
    return base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")


def _google_recurrence(draft: EventDraft, timezone_name: str) -> list[str]:
    recurrence = draft.recurrence
    if recurrence.frequency in ("preserve", "none"):
        return []
    frequency = {
        "daily": "DAILY",
        "weekdays": "WEEKLY",
        "weekly": "WEEKLY",
        "monthly": "MONTHLY",
        "selected_weekdays": "WEEKLY",
    }[recurrence.frequency]
    parts = [f"FREQ={frequency}"]
    if recurrence.weekdays:
        parts.append("BYDAY=" + ",".join(recurrence.weekdays))
    if recurrence.end == "count":
        parts.append(f"COUNT={recurrence.count}")
    elif recurrence.end == "date":
        local_end = datetime.combine(
            date.fromisoformat(recurrence.until), time(23, 59, 59), _zone(timezone_name),
        )
        parts.append("UNTIL=" + local_end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return ["RRULE:" + ";".join(parts)]


def google_payload(
    draft: EventDraft,
    calendar: Calendar,
    existing_meeting_url: str = "",
) -> tuple[dict[str, Any], bool]:
    payload: dict[str, Any] = {
        "id": google_event_id(draft.request_id),
        "summary": draft.title,
        "location": draft.location,
        "description": _notes_with_meeting(draft.notes, existing_meeting_url, draft.online_meeting),
    }
    if draft.all_day:
        start_day = date.fromisoformat(draft.day)
        payload.update({"start": {"date": draft.day}, "end": {"date": (start_day + timedelta(days=1)).isoformat()}})
    else:
        payload.update({
            "start": {"dateTime": _local_datetime(draft.day, draft.start, calendar.timezone).isoformat(), "timeZone": calendar.timezone},
            "end": {"dateTime": _local_datetime(draft.day, draft.end, calendar.timezone).isoformat(), "timeZone": calendar.timezone},
        })
    recurrence = _google_recurrence(draft, calendar.timezone)
    if recurrence:
        payload["recurrence"] = recurrence
    conference = draft.online_meeting == "new"
    if conference:
        if "googleMeet" not in calendar.meeting_providers:
            raise DraftError("This Google calendar cannot create a meeting")
        payload["conferenceData"] = {
            "createRequest": {
                "requestId": draft.request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    return payload, conference


def _microsoft_recurrence(draft: EventDraft, timezone: str) -> dict[str, Any] | None:
    recurrence = draft.recurrence
    if recurrence.frequency in ("preserve", "none"):
        return None
    if recurrence.frequency == "daily":
        pattern: dict[str, Any] = {"type": "daily", "interval": 1}
    elif recurrence.frequency == "monthly":
        pattern = {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": date.fromisoformat(draft.day).day}
    else:
        pattern = {
            "type": "weekly", "interval": 1,
            "daysOfWeek": [MICROSOFT_WEEKDAYS[item] for item in recurrence.weekdays],
            "firstDayOfWeek": "monday",
        }
    range_data: dict[str, Any] = {
        "type": "noEnd", "startDate": draft.day, "recurrenceTimeZone": timezone,
    }
    if recurrence.end == "count":
        range_data.update({"type": "numbered", "numberOfOccurrences": recurrence.count})
    elif recurrence.end == "date":
        range_data.update({"type": "endDate", "endDate": recurrence.until})
    return {"pattern": pattern, "range": range_data}


def microsoft_payload(
    draft: EventDraft,
    calendar: Calendar,
    existing_meeting_url: str = "",
) -> dict[str, Any]:
    start = "00:00" if draft.all_day else draft.start
    end_day = (date.fromisoformat(draft.day) + timedelta(days=1)).isoformat() if draft.all_day else draft.day
    end = "00:00" if draft.all_day else draft.end
    _local_datetime(draft.day, start, calendar.timezone)
    _local_datetime(end_day, end, calendar.timezone)
    payload: dict[str, Any] = {
        "transactionId": draft.request_id,
        "subject": draft.title,
        "body": {
            "contentType": "text",
            "content": _notes_with_meeting(draft.notes, existing_meeting_url, draft.online_meeting),
        },
        "location": {"displayName": draft.location},
        "start": {"dateTime": f"{draft.day}T{start}:00", "timeZone": calendar.timezone},
        "end": {"dateTime": f"{end_day}T{end}:00", "timeZone": calendar.timezone},
        "isAllDay": draft.all_day,
    }
    recurrence = _microsoft_recurrence(draft, calendar.timezone)
    if recurrence:
        payload["recurrence"] = recurrence
    if draft.online_meeting == "new":
        if not calendar.meeting_providers:
            raise DraftError("This Outlook calendar cannot create a meeting")
        payload.update({
            "isOnlineMeeting": True,
            "onlineMeetingProvider": calendar.meeting_providers[0],
        })
    return payload


class MutationService:
    def __init__(
        self,
        store: CalendarStore,
        *,
        keyring: Any | None = None,
        settings: ProviderSettings | None = None,
        read_http: Any | None = None,
        write_http: Any | None = None,
        now: Callable[[], datetime] | None = None,
        timezone: str | None = None,
    ):
        self.store = store
        self.keyring = keyring or SecretServiceStore()
        self.settings = settings or ProviderSettings.load()
        self.read_http = read_http or ReadOnlyHttp()
        self.write_http = write_http or CalendarWriteHttp()
        self.now = now or datetime.now
        self.timezone = timezone or local_timezone_name()

    def create(self, draft: EventDraft) -> dict[str, object]:
        event = self._create(draft)
        return {"mode": "create", "event": event, "refresh": True}

    def copy(self, draft: EventDraft) -> dict[str, object]:
        source = self._source(draft.source_uid)
        event = self._create(draft, str(source.get("meeting_url") or ""))
        source_calendar = self.store.get_calendar(str(source.get("calendar_key") or ""))
        return {
            "mode": "copy",
            "event": event,
            "delete_original_available": bool(
                source.get("organizer_owned")
                and source_calendar
                and source_calendar.writable
                and source_calendar.owned
            ),
            "original_uid": source["uid"],
            "refresh": True,
        }

    def update(self, draft: EventDraft) -> dict[str, object]:
        source = self._source(draft.source_uid)
        calendar = self._calendar(draft.calendar_key)
        if self._calendar_key(source) != draft.calendar_key:
            raise DraftError("Changing calendars requires copy-event")
        self._writable(calendar)
        if not source.get("organizer_owned"):
            raise PermissionError("Only events you organize can be edited")
        if (
            source.get("recurrence_id")
            and draft.scope == "single"
            and draft.recurrence.frequency not in ("preserve", "none")
        ):
            raise DraftError("Choose Entire series to change a recurring schedule")
        token = self._token(calendar)
        event_id = self._event_id(source, draft.scope)
        current: dict[str, Any] | None = None
        if draft.scope == "series" and source.get("recurrence_id"):
            current = self._fetch(calendar, event_id, token)
        if calendar.provider == "microsoft":
            current = current or self._fetch(calendar, event_id, token)
            if draft.scope == "single" and str(current.get("changeKey") or "") != str(source.get("revision") or ""):
                self._store_raw(calendar, current)
                raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.")
            if current.get("isOnlineMeeting") is True or current.get("onlineMeeting"):
                if draft.online_meeting != "preserve":
                    raise DraftError("An existing Outlook online meeting cannot be removed or replaced")
                if draft.notes != str(source.get("description") or ""):
                    raise DraftError("Outlook meeting notes cannot be changed without risking its meeting details")
        payload_draft = (
            self._series_draft(draft, source, calendar, current)
            if draft.scope == "series" and current is not None else draft
        )
        payload, conference = self._payload(payload_draft, calendar, str(source.get("meeting_url") or ""))
        payload.pop("id", None)
        payload.pop("transactionId", None)
        if calendar.provider == "microsoft" and draft.notes == str(source.get("description") or ""):
            payload.pop("body", None)
        if calendar.provider == "google" and source.get("meeting_url") and draft.online_meeting == "none":
            payload["conferenceData"] = None
            conference = True
        if draft.scope == "series" and source.get("recurrence_id") and draft.recurrence.frequency == "none":
            payload["recurrence"] = [] if calendar.provider == "google" else None
        if source.get("event_type") == "occurrence" and draft.scope == "single":
            payload.pop("recurrence", None)
        url = self._event_url(calendar, event_id)
        if conference and calendar.provider == "google":
            url += "?conferenceDataVersion=1"
        headers = self._headers(token)
        if calendar.provider == "google":
            headers["If-Match"] = str(
                current.get("etag") if current is not None else source.get("revision") or ""
            )
        try:
            self.write_http.request_json("PATCH", url, payload, headers=headers)
        except HttpError as error:
            if calendar.provider == "google" and error.status == 412:
                self._store_raw(calendar, self._fetch(calendar, event_id, token))
                raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.") from error
            raise
        event = self._store_raw(calendar, self._fetch(calendar, event_id, token))
        return {"mode": "update", "event": event, "refresh": True}

    def delete_event(self, uid: str, *, scope: str) -> dict[str, object]:
        if scope not in ("single", "series"):
            raise DraftError("Event recurrence scope is invalid")
        source = self._source(uid)
        if not source.get("organizer_owned"):
            raise PermissionError("Only events you organize can be deleted")
        calendar = self._calendar(str(source.get("calendar_key") or ""))
        self._writable(calendar)
        token = self._token(calendar)
        event_id = self._event_id(source, scope)
        current: dict[str, Any] | None = None
        if scope == "series" and source.get("recurrence_id"):
            current = self._fetch(calendar, event_id, token)
        if calendar.provider == "microsoft":
            current = current or self._fetch(calendar, event_id, token)
            expected = str(source.get("revision") or "")
            if scope == "single" and str(current.get("changeKey") or "") != expected:
                self._store_raw(calendar, current)
                raise MutationConflict("Event changed remotely. Delete cancelled; review the refreshed event.")
        headers = self._headers(token)
        if calendar.provider == "google":
            headers["If-Match"] = str(
                current.get("etag") if current is not None else source.get("revision") or ""
            )
        try:
            self.write_http.request_json("DELETE", self._event_url(calendar, event_id), headers=headers)
        except HttpError as error:
            if calendar.provider == "google" and error.status == 412:
                self._store_raw(calendar, self._fetch(calendar, event_id, token))
                raise MutationConflict("Event changed remotely. Delete cancelled; review the refreshed event.") from error
            raise
        self.store.remove_event(uid, series_id=event_id if scope == "series" else "")
        return {"deleted": True, "uid": uid, "scope": scope, "refresh": True}

    def _create(self, draft: EventDraft, existing_meeting_url: str = "") -> dict[str, object]:
        calendar = self._calendar(draft.calendar_key)
        self._writable(calendar)
        token = self._token(calendar)
        payload, conference = self._payload(draft, calendar, existing_meeting_url)
        url = self._collection_url(calendar)
        if conference and calendar.provider == "google":
            url += "?conferenceDataVersion=1"
        try:
            created = self.write_http.request_json("POST", url, payload, headers=self._headers(token))
        except HttpError as error:
            if error.status != 409 or calendar.provider != "google":
                raise
            created = {"id": google_event_id(draft.request_id)}
        event_id = str(created.get("id") or "")
        if not event_id:
            raise RuntimeError("Provider did not return the created event ID")
        return self._store_raw(calendar, self._fetch(calendar, event_id, token))

    def _payload(
        self,
        draft: EventDraft,
        calendar: Calendar,
        existing_meeting_url: str,
    ) -> tuple[dict[str, Any], bool]:
        calendar = replace(calendar, timezone=self.timezone)
        if calendar.provider == "google":
            return google_payload(draft, calendar, existing_meeting_url)
        return microsoft_payload(draft, calendar, existing_meeting_url), False

    def _token(self, calendar: Calendar) -> dict[str, Any]:
        token = self.keyring.get(calendar.provider, calendar.account_id)
        if token is None:
            raise PermissionError("Reconnect this account before editing")
        if float(token.get("expires_at") or 0) <= self.now().timestamp() + 60:
            token = SyncEngine(
                self.store,
                keyring=self.keyring,
                settings=self.settings,
                http=self.read_http,
                now=self.now,
            )._refresh_if_needed(calendar.provider, calendar.account_id, token)
        scopes = set(str(token.get("scope") or "").split())
        write_scope = (
            "https://www.googleapis.com/auth/calendar.events.owned"
            if calendar.provider == "google" else "Calendars.ReadWrite"
        )
        if token.get("access_mode") != "edit" and write_scope not in scopes:
            raise PermissionError("Read and edit permission is required")
        return token

    def _calendar(self, key: str) -> Calendar:
        calendar = self.store.get_calendar(key)
        if calendar is None:
            raise DraftError("Destination calendar is not available")
        return calendar

    @staticmethod
    def _writable(calendar: Calendar) -> None:
        if not calendar.writable or not calendar.owned:
            raise PermissionError("Destination calendar is not owned and writable")

    def _source(self, uid: str) -> dict[str, object]:
        source = self.store.get_event(uid)
        if source is None:
            raise DraftError("Source event is not available")
        return source

    @staticmethod
    def _calendar_key(event: Mapping[str, object]) -> str:
        return str(event.get("calendar_key") or "")

    @staticmethod
    def _event_id(source: Mapping[str, object], scope: str) -> str:
        event_id = str(source.get("provider_event_id") or "")
        if scope == "series" and source.get("recurrence_id"):
            event_id = str(source["recurrence_id"])
        if not event_id:
            raise DraftError("Provider event identity is missing; refresh before editing")
        return event_id

    @staticmethod
    def _headers(token: Mapping[str, object]) -> dict[str, str]:
        return {"Authorization": f"Bearer {token['access_token']}"}

    @staticmethod
    def _collection_url(calendar: Calendar) -> str:
        calendar_id = quote(calendar.calendar_id, safe="")
        if calendar.provider == "google":
            return f"{GOOGLE_API}/calendars/{calendar_id}/events"
        return f"{GRAPH}/me/calendars/{calendar_id}/events"

    def _event_url(self, calendar: Calendar, event_id: str) -> str:
        return f"{self._collection_url(calendar)}/{quote(event_id, safe='')}"

    def _fetch(self, calendar: Calendar, event_id: str, token: Mapping[str, object]) -> dict[str, Any]:
        return self.read_http.get_json(self._event_url(calendar, event_id), headers=self._headers(token))

    @staticmethod
    def _normalized_raw(calendar: Calendar, raw: dict[str, Any]) -> Event:
        account = Account(calendar.provider, calendar.account_id, calendar.account_label)
        if calendar.provider == "google":
            metadata = {
                "id": calendar.calendar_id, "summary": calendar.name,
                "backgroundColor": calendar.color, "timeZone": calendar.timezone,
            }
            event = normalize_google_event(raw, account, metadata)
        else:
            metadata = {"id": calendar.calendar_id, "name": calendar.name, "color": "auto"}
            event = normalize_microsoft_event(raw, account, metadata)
        if event is None:
            raise RuntimeError("Provider did not return an active event")
        return Event(**{**event.to_dict(), "calendar_color": calendar.color})

    def _series_draft(
        self,
        draft: EventDraft,
        source: Mapping[str, object],
        calendar: Calendar,
        current: dict[str, Any],
    ) -> EventDraft:
        zone = _zone(self.timezone)
        master = self._normalized_raw(calendar, current)
        source_start = datetime.fromisoformat(str(source["start"])).astimezone(zone)
        master_start = datetime.fromisoformat(master.start).astimezone(zone)
        if draft.all_day:
            moved = master_start.date() + (date.fromisoformat(draft.day) - source_start.date())
            return replace(draft, day=moved.isoformat(), start="", end="")
        draft_start = _local_datetime(draft.day, draft.start, self.timezone)
        draft_end = _local_datetime(draft.day, draft.end, self.timezone)
        moved = master_start + (draft_start - source_start)
        duration = draft_end - draft_start
        return replace(
            draft,
            day=moved.date().isoformat(),
            start=moved.strftime("%H:%M"),
            end=(moved + duration).strftime("%H:%M"),
        )

    def _store_raw(self, calendar: Calendar, raw: dict[str, Any]) -> dict[str, object]:
        event = self._normalized_raw(calendar, raw)
        self.store.upsert_event(event)
        stored = self.store.get_event(event.uid)
        if stored is None:
            raise RuntimeError("Created event could not be cached")
        return stored
