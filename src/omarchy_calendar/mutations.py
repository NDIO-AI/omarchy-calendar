# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
import hashlib
import re
import time as time_module
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cache import CalendarStore
from .http import HttpError, ReadOnlyHttp
from .keyring import SecretServiceStore
from .models import Account, Calendar, Event
from .normalize import extract_meeting_url, is_safe_https_url, plain_text
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
GOOGLE_WEEKDAYS = {value: key for key, value in MICROSOFT_WEEKDAYS.items()}


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
    end_day: str
    start: str
    end: str
    all_day: bool
    location: str
    notes: str
    recurrence: Recurrence
    online_meeting: str
    source_uid: str
    source_revision: str
    series_revision: str
    series_start: str
    series_end: str
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
        default_end_day = (
            (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            if all_day else day
        )
        end_day = _date(raw.get("end_day") or default_end_day, "end day")
        start = str(raw.get("start") or "")
        end = str(raw.get("end") or "")
        if not all_day:
            start = _time(start)
            end = _time(end)
            if datetime.combine(date.fromisoformat(end_day), time.fromisoformat(end)) <= datetime.combine(
                date.fromisoformat(day), time.fromisoformat(start)
            ):
                raise DraftError("Event end must be after its start")
        else:
            start = ""
            end = ""
            if end_day <= day:
                raise DraftError("All-day event end must be after its start")
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
            end_day=end_day,
            start=start,
            end=end,
            all_day=all_day,
            location=_text(raw.get("location"), "location", 500),
            notes=_text(raw.get("notes"), "notes", 8192),
            recurrence=recurrence,
            online_meeting=online_meeting,
            source_uid=_text(raw.get("source_uid"), "source event", 2048),
            source_revision=_text(raw.get("source_revision"), "source revision", 2048),
            series_revision=_text(raw.get("series_revision"), "series revision", 2048),
            series_start=_timestamp(raw.get("series_start"), "series start"),
            series_end=_timestamp(raw.get("series_end"), "series end"),
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


def _timestamp(value: object, name: str) -> str:
    raw = _text(value, name, 128)
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise DraftError(f"Event {name} is invalid") from error
    if parsed.utcoffset() is None:
        raise DraftError(f"Event {name} is invalid")
    return parsed.isoformat()


def _time(value: object) -> str:
    try:
        return datetime.strptime(str(value or ""), "%H:%M").strftime("%H:%M")
    except ValueError as error:
        raise DraftError("Event time is invalid") from error


def _recurrence(value: object, start_day: str) -> Recurrence:
    if value is not None and not isinstance(value, Mapping):
        raise DraftError("Event recurrence is invalid")
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
    if (
        frequency in ("weekdays", "weekly", "selected_weekdays")
        and WEEKDAYS[date.fromisoformat(start_day).weekday()] not in weekdays
    ):
        raise DraftError("Event recurrence start day must match its weekday pattern")
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


def _without_meeting_link(text: str, meeting_url: str) -> str:
    without_link = text.replace(meeting_url, "")
    return re.sub(r"(?m)^[ \t]*Meeting:[ \t]*(?:\n|$)", "", without_link).strip()


def _notes_with_meeting(notes: str, meeting_url: str, mode: str) -> str:
    if not is_safe_https_url(meeting_url):
        return notes
    if mode == "preserve":
        return notes if meeting_url in notes else f"Meeting: {meeting_url}" + ("\n\n" + notes if notes else "")
    return _without_meeting_link(notes, meeting_url)


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
        if draft.all_day:
            parts.append("UNTIL=" + date.fromisoformat(recurrence.until).strftime("%Y%m%d"))
        else:
            local_end = datetime.combine(
                date.fromisoformat(recurrence.until), time(23, 59, 59), _zone(timezone_name),
            )
            parts.append("UNTIL=" + local_end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return ["RRULE:" + ";".join(parts)]


def _google_source_recurrence(raw: Mapping[str, object], timezone_name: str) -> Recurrence:
    rules = raw.get("recurrence")
    if not isinstance(rules, list) or len(rules) != 1 or not str(rules[0]).startswith("RRULE:"):
        raise DraftError("This series recurrence is not supported for transfer")
    parts: dict[str, str] = {}
    for part in str(rules[0])[6:].split(";"):
        key, separator, value = part.partition("=")
        if not separator or key in parts:
            raise DraftError("This series recurrence is not supported for transfer")
        parts[key] = value
    if set(parts) - {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"}:
        raise DraftError("This series recurrence is not supported for transfer")
    if parts.get("INTERVAL", "1") != "1" or ("COUNT" in parts and "UNTIL" in parts):
        raise DraftError("This series recurrence is not supported for transfer")
    start_day = str((raw.get("start") or {}).get("date") or (raw.get("start") or {}).get("dateTime") or "")[:10]
    frequency = parts.get("FREQ")
    weekdays: tuple[str, ...] = ()
    if frequency == "DAILY":
        if "BYDAY" in parts or "BYMONTHDAY" in parts:
            raise DraftError("This series recurrence is not supported for transfer")
        normalized_frequency = "daily"
    elif frequency == "WEEKLY":
        if "BYMONTHDAY" in parts:
            raise DraftError("This series recurrence is not supported for transfer")
        supplied = parts.get("BYDAY") or WEEKDAYS[date.fromisoformat(start_day).weekday()]
        weekdays = tuple(dict.fromkeys(supplied.split(",")))
        if not weekdays or any(item not in WEEKDAYS for item in weekdays):
            raise DraftError("This series recurrence is not supported for transfer")
        normalized_frequency = (
            "weekdays" if weekdays == WEEKDAYS[:5]
            else "weekly" if len(weekdays) == 1
            else "selected_weekdays"
        )
    elif frequency == "MONTHLY":
        if "BYDAY" in parts:
            raise DraftError("This series recurrence is not supported for transfer")
        if parts.get("BYMONTHDAY", str(date.fromisoformat(start_day).day)) != str(date.fromisoformat(start_day).day):
            raise DraftError("This series recurrence is not supported for transfer")
        normalized_frequency = "monthly"
    else:
        raise DraftError("This series recurrence is not supported for transfer")
    ending = "never"
    count = 0
    until = ""
    if "COUNT" in parts:
        try:
            count = int(parts["COUNT"])
        except ValueError as error:
            raise DraftError("This series recurrence is not supported for transfer") from error
        if not 1 <= count <= 999:
            raise DraftError("This series recurrence is not supported for transfer")
        ending = "count"
    elif "UNTIL" in parts:
        value = parts["UNTIL"]
        try:
            until = (
                datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(_zone(timezone_name)).date()
                if "T" in value else datetime.strptime(value, "%Y%m%d").date()
            ).isoformat()
        except ValueError as error:
            raise DraftError("This series recurrence is not supported for transfer") from error
        ending = "date"
    return Recurrence(normalized_frequency, weekdays, ending, count, until)


def _microsoft_source_recurrence(raw: Mapping[str, object]) -> Recurrence:
    recurrence = raw.get("recurrence")
    if not isinstance(recurrence, Mapping):
        raise DraftError("This series recurrence is not supported for transfer")
    pattern = recurrence.get("pattern")
    range_data = recurrence.get("range")
    if not isinstance(pattern, Mapping) or not isinstance(range_data, Mapping) or int(pattern.get("interval") or 1) != 1:
        raise DraftError("This series recurrence is not supported for transfer")
    pattern_type = str(pattern.get("type") or "")
    weekdays: tuple[str, ...] = ()
    if pattern_type == "daily":
        frequency = "daily"
    elif pattern_type == "weekly":
        supplied = pattern.get("daysOfWeek")
        if not isinstance(supplied, list):
            raise DraftError("This series recurrence is not supported for transfer")
        try:
            weekdays = tuple(dict.fromkeys(GOOGLE_WEEKDAYS[str(item)] for item in supplied))
        except KeyError as error:
            raise DraftError("This series recurrence is not supported for transfer") from error
        if not weekdays:
            raise DraftError("This series recurrence is not supported for transfer")
        frequency = (
            "weekdays" if weekdays == WEEKDAYS[:5]
            else "weekly" if len(weekdays) == 1
            else "selected_weekdays"
        )
    elif pattern_type == "absoluteMonthly":
        start_day = str((raw.get("start") or {}).get("dateTime") or "")[:10]
        if int(pattern.get("dayOfMonth") or 0) != date.fromisoformat(start_day).day:
            raise DraftError("This series recurrence is not supported for transfer")
        frequency = "monthly"
    else:
        raise DraftError("This series recurrence is not supported for transfer")
    range_type = str(range_data.get("type") or "")
    if range_type == "noEnd":
        return Recurrence(frequency, weekdays)
    if range_type == "numbered":
        count = int(range_data.get("numberOfOccurrences") or 0)
        if not 1 <= count <= 999:
            raise DraftError("This series recurrence is not supported for transfer")
        return Recurrence(frequency, weekdays, "count", count)
    if range_type == "endDate":
        until = _date(range_data.get("endDate"), "recurrence end date")
        return Recurrence(frequency, weekdays, "date", 0, until)
    raise DraftError("This series recurrence is not supported for transfer")


def _shifted_recurrence(recurrence: Recurrence, days: int) -> Recurrence:
    weekdays = recurrence.weekdays
    frequency = recurrence.frequency
    if weekdays and days % 7:
        weekdays = tuple(WEEKDAYS[(WEEKDAYS.index(item) + days) % 7] for item in weekdays)
        frequency = (
            "weekdays" if weekdays == WEEKDAYS[:5]
            else "weekly" if len(weekdays) == 1
            else "selected_weekdays"
        )
    until = (
        (date.fromisoformat(recurrence.until) + timedelta(days=days)).isoformat()
        if recurrence.end == "date" and days else recurrence.until
    )
    return replace(recurrence, frequency=frequency, weekdays=weekdays, until=until)


def google_payload(
    draft: EventDraft,
    calendar: Calendar,
    existing_meeting_url: str = "",
    *,
    include_temporal: bool = True,
) -> tuple[dict[str, Any], bool]:
    payload: dict[str, Any] = {
        "id": google_event_id(draft.request_id),
        "summary": draft.title,
        "location": _without_meeting_link(draft.location, existing_meeting_url)
        if existing_meeting_url and draft.online_meeting != "preserve" else draft.location,
        "description": _notes_with_meeting(draft.notes, existing_meeting_url, draft.online_meeting),
    }
    if include_temporal:
        if draft.all_day:
            payload.update({"start": {"date": draft.day}, "end": {"date": draft.end_day}})
        else:
            payload.update({
                "start": {"dateTime": _local_datetime(draft.day, draft.start, calendar.timezone).isoformat(), "timeZone": calendar.timezone},
                "end": {"dateTime": _local_datetime(draft.end_day, draft.end, calendar.timezone).isoformat(), "timeZone": calendar.timezone},
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
    *,
    include_temporal: bool = True,
) -> dict[str, Any]:
    start = "00:00" if draft.all_day else draft.start
    end_day = draft.end_day
    end = "00:00" if draft.all_day else draft.end
    payload: dict[str, Any] = {
        "transactionId": draft.request_id,
        "subject": draft.title,
        "body": {
            "contentType": "text",
            "content": _notes_with_meeting(draft.notes, existing_meeting_url, draft.online_meeting),
        },
        "location": {"displayName": (
            _without_meeting_link(draft.location, existing_meeting_url)
            if existing_meeting_url and draft.online_meeting != "preserve" else draft.location
        )},
    }
    if include_temporal:
        _local_datetime(draft.day, start, calendar.timezone)
        _local_datetime(end_day, end, calendar.timezone)
        payload.update({
            "start": {"dateTime": f"{draft.day}T{start}:00", "timeZone": calendar.timezone},
            "end": {"dateTime": f"{end_day}T{end}:00", "timeZone": calendar.timezone},
            "isAllDay": draft.all_day,
        })
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
        sleep: Callable[[float], None] | None = None,
    ):
        self.store = store
        self.keyring = keyring or SecretServiceStore()
        self.settings = settings or ProviderSettings.load()
        self.read_http = read_http or ReadOnlyHttp()
        self.write_http = write_http or CalendarWriteHttp()
        self.now = now or datetime.now
        self.timezone = timezone or local_timezone_name()
        self.sleep = sleep or time_module.sleep

    def create(self, draft: EventDraft) -> dict[str, object]:
        if draft.recurrence.frequency == "preserve":
            raise DraftError("A new event cannot preserve an existing recurrence")
        event, notice = self._create(draft)
        return {
            "mode": "create", "event": event, "refresh": True,
            **({"notice": notice} if notice else {}),
        }

    def copy(self, draft: EventDraft) -> dict[str, object]:
        source = self._source(draft.source_uid)
        source_calendar = self.store.get_calendar(str(source.get("calendar_key") or ""))
        is_transfer = self._calendar_key(source) != draft.calendar_key
        copy_draft = draft
        source_master: Event | None = None
        series_delete_blocker = ""
        if draft.scope == "series":
            if not self._recurring(source):
                raise DraftError("Only a recurring event can be copied as Entire series")
            if source_calendar is None:
                raise DraftError("Source calendar is not available")
            self._unchanged_source(source, draft.source_revision)
            self._series_baseline(draft, source)
            source_token = self._access_token(source_calendar)
            source_series_id = self._event_id(source, "series")
            current = (
                self._fetch_transfer_series(source_calendar, source_series_id, source_token)
                if is_transfer else self._fetch(source_calendar, source_series_id, source_token)
            )
            if self._revision(source_calendar, current) != draft.series_revision:
                self._store_raw(source_calendar, current)
                raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.")
            copy_draft, source_master = self._master_seeded_series_draft(
                draft, source, source_calendar, current,
            )
            if draft.recurrence.frequency == "none":
                raise DraftError("Entire series transfer must keep or replace the recurring schedule")
            if draft.recurrence.frequency == "preserve":
                recurrence = self._source_recurrence(source_calendar, current)
                copy_draft = replace(copy_draft, recurrence=recurrence)
                copy_draft = self._series_draft(copy_draft, source, shift_recurrence=True)
            else:
                copy_draft = self._series_draft(copy_draft, source)
        meeting_url = (
            source_master.meeting_url if source_master is not None
            else str(source.get("meeting_url") or "")
        )
        event, notice = self._create(
            copy_draft,
            meeting_url,
            verify_copy=is_transfer,
        )
        if draft.scope == "series":
            event = self._store_created_series_preview(event, draft, copy_draft)
            if is_transfer:
                series_delete_blocker = self._series_delete_blocker(
                    source_calendar, source_series_id, source_token, current,
                )
        delete_available = False
        delete_needs_permission = False
        delete_reason = ""
        source_health = self.store.health(
            str(source.get("provider") or ""), str(source.get("account_id") or ""),
        )
        if is_transfer:
            if draft.scope == "series" and series_delete_blocker:
                delete_reason = series_delete_blocker
            elif notice:
                delete_reason = notice + " The original is unchanged."
            elif source_health and (not source_health.connected or source_health.stale):
                delete_reason = "The source account is offline, so the original cannot be deleted yet."
            elif source.get("has_attendees") or (
                source_master is not None and source_master.has_attendees
            ):
                delete_reason = "The original has attendees, so Flight Deck will keep both events."
            elif not source.get("organizer_owned") or (
                source_master is not None and not source_master.organizer_owned
            ):
                delete_reason = "You are not the organizer, so the original cannot be deleted."
            elif not source_calendar or not source_calendar.writable or not source_calendar.owned:
                delete_reason = "The source calendar is shared or read-only, so the original cannot be deleted."
            elif self._has_write_access(source_calendar):
                delete_available = True
            else:
                delete_needs_permission = True
                delete_reason = f"Enable editing for {source_calendar.account_label} to delete the original."
        return {
            "mode": "copy",
            "event": event,
            "delete_original_available": delete_available,
            "delete_original_needs_permission": delete_needs_permission,
            "delete_original_reason": delete_reason,
            "original_uid": source["uid"] if is_transfer else "",
            "original_scope": draft.scope,
            "original_revision": source.get("revision") or "",
            "original_series_revision": draft.series_revision if draft.scope == "series" else "",
            "original_provider": source.get("provider") or "",
            "original_account_id": source.get("account_id") or "",
            "refresh": True,
            **({"notice": notice} if notice else {}),
        }

    def update(self, draft: EventDraft) -> dict[str, object]:
        requested_draft = draft
        source = self._source(draft.source_uid)
        if source.get("has_attendees"):
            raise PermissionError("Events with attendees are duplicate-only in this release")
        calendar = self._calendar(draft.calendar_key)
        if self._calendar_key(source) != draft.calendar_key:
            raise DraftError("Changing calendars requires copy-event")
        self._writable(calendar)
        if not source.get("organizer_owned"):
            raise PermissionError("Only events you organize can be edited")
        self._unchanged_source(source, draft.source_revision)
        recurring = self._recurring(source)
        if draft.scope == "series" and not recurring:
            raise DraftError("Only a recurring event can be edited as Entire series")
        if self._series_only(source) and draft.scope != "series":
            raise DraftError("A series master must be edited as Entire series")
        if (
            recurring
            and draft.scope == "single"
            and draft.recurrence.frequency not in ("preserve", "none")
        ):
            raise DraftError("Choose Entire series to change a recurring schedule")
        token = self._token(calendar)
        event_id = self._event_id(source, draft.scope)
        series_update = draft.scope == "series" and recurring
        ends_series = series_update and draft.recurrence.frequency == "none"
        current: dict[str, Any] | None = None
        if series_update:
            self._series_baseline(draft, source)
            current = self._fetch(calendar, event_id, token)
            if self._revision(calendar, current) != draft.series_revision:
                self._store_raw(calendar, current)
                raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.")
            draft, master = self._master_seeded_series_draft(draft, source, calendar, current)
            if master.has_attendees:
                raise PermissionError("Events with attendees are duplicate-only in this release")
            if not master.organizer_owned:
                raise PermissionError("Only events you organize can be edited")
        if calendar.provider == "microsoft":
            current = current or self._fetch(calendar, event_id, token)
            if draft.scope == "single" and str(current.get("changeKey") or "") != str(source.get("revision") or ""):
                self._store_raw(calendar, current)
                raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.")
            if current.get("isOnlineMeeting") is True or current.get("onlineMeeting"):
                if draft.online_meeting != "preserve":
                    raise DraftError("An existing Outlook online meeting cannot be removed or replaced")
                if requested_draft.notes != str(source.get("description") or ""):
                    raise DraftError("Outlook meeting notes cannot be changed without risking its meeting details")
        temporal_changed = self._temporal_changed(draft, source)
        payload_draft = draft
        if series_update and not ends_series:
            payload_draft = (
                self._series_draft(draft, source)
                if temporal_changed
                else replace(draft, day=master.start_day, end_day=master.end_day)
            )
        if series_update and not ends_series and self._series_day_changed(draft, source) and draft.recurrence.frequency == "preserve":
            payload_draft = self._series_draft(
                replace(draft, recurrence=self._source_recurrence(calendar, current or {})),
                source,
                shift_recurrence=True,
            )
        meeting_url = str(
            master.meeting_url if series_update else source.get("meeting_url") or ""
        )
        cleanup_url = meeting_url if draft.online_meeting != "preserve" else ""
        payload, conference = self._payload(
            payload_draft,
            calendar,
            cleanup_url,
            include_temporal=ends_series or temporal_changed,
        )
        payload.pop("id", None)
        payload.pop("transactionId", None)
        if not ends_series and not temporal_changed:
            payload.pop("start", None)
            payload.pop("end", None)
            payload.pop("isAllDay", None)
        if requested_draft.notes == str(source.get("description") or "") and not (
            cleanup_url and cleanup_url in payload_draft.notes
        ):
            payload.pop("description" if calendar.provider == "google" else "body", None)
        if requested_draft.location == str(source.get("location") or "") and not (
            cleanup_url and cleanup_url in payload_draft.location
        ):
            payload.pop("location", None)
        if requested_draft.title == str(source.get("title") or ""):
            payload.pop("summary" if calendar.provider == "google" else "subject", None)
        if calendar.provider == "google" and (
            meeting_url
        ) and draft.online_meeting == "none":
            payload["conferenceData"] = None
            conference = True
        if draft.scope == "series" and recurring and draft.recurrence.frequency == "none":
            payload["recurrence"] = [] if calendar.provider == "google" else None
        if source.get("event_type") == "occurrence" and draft.scope == "single":
            payload.pop("recurrence", None)
        url = self._event_url(calendar, event_id)
        if conference and calendar.provider == "google":
            url += "?conferenceDataVersion=1"
        headers = self._headers(token)
        if calendar.provider == "google":
            headers["If-Match"] = draft.series_revision if current is not None else draft.source_revision
        try:
            updated_raw = self.write_http.request_json("PATCH", url, payload, headers=headers)
        except HttpError as error:
            if calendar.provider == "google" and error.status == 412:
                self._store_raw(calendar, self._fetch(calendar, event_id, token))
                raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.") from error
            raise
        notice = ""
        if draft.online_meeting == "new":
            updated_raw, notice = self._fetch_after_write(calendar, event_id, token, True)
        if series_update:
            self.store.remove_event(str(source["uid"]), series_id=event_id)
            event = self._store_series_result(
                calendar, updated_raw, source, draft,
                temporal_changed=temporal_changed,
            )
        else:
            raw = updated_raw if draft.online_meeting == "new" else self._fetch(calendar, event_id, token)
            event = self._store_raw(calendar, raw)
        return {
            "mode": "update", "event": event, "refresh": True,
            **({"notice": notice} if notice else {}),
        }

    def delete_event(
        self,
        uid: str,
        *,
        scope: str,
        expected_revision: str,
        series_revision: str = "",
        series_transfer_guard: bool = False,
    ) -> dict[str, object]:
        if scope not in ("single", "series"):
            raise DraftError("Event recurrence scope is invalid")
        if series_transfer_guard and scope != "series":
            raise DraftError("Series transfer guard requires Entire series")
        source = self._source(uid)
        if source.get("has_attendees"):
            raise PermissionError("Events with attendees are duplicate-only in this release")
        if self._series_only(source) and scope != "series":
            raise DraftError("A series master must be deleted as Entire series")
        if not source.get("organizer_owned"):
            raise PermissionError("Only events you organize can be deleted")
        calendar = self._calendar(str(source.get("calendar_key") or ""))
        self._writable(calendar)
        self._unchanged_source(source, expected_revision, deleting=True)
        recurring = self._recurring(source)
        if scope == "series" and not recurring:
            raise DraftError("Only a recurring event can be deleted as Entire series")
        token = self._token(calendar)
        event_id = self._event_id(source, scope)
        current: dict[str, Any] | None = None
        try:
            if scope == "series" and recurring:
                if not series_revision:
                    raise DraftError("Series revision is missing; refresh before deleting")
                if str(source.get("series_revision") or "") != series_revision:
                    raise MutationConflict("Event changed remotely. Delete cancelled; review the refreshed event.")
                current = (
                    self._fetch_transfer_series(calendar, event_id, token)
                    if series_transfer_guard else self._fetch(calendar, event_id, token)
                )
                if self._revision(calendar, current) != series_revision:
                    self._store_raw(calendar, current)
                    raise MutationConflict("Event changed remotely. Delete cancelled; review the refreshed event.")
                master = self._normalized_raw(calendar, current)
                if master.has_attendees:
                    raise PermissionError("Events with attendees are duplicate-only in this release")
                if not master.organizer_owned:
                    raise PermissionError("Only events you organize can be deleted")
                if series_transfer_guard:
                    blocker = self._series_delete_blocker(calendar, event_id, token, current)
                    if blocker:
                        raise MutationConflict(blocker)
                    if calendar.provider == "google":
                        current = self._fetch(calendar, event_id, token)
                        if self._revision(calendar, current) != series_revision:
                            self._store_raw(calendar, current)
                            raise MutationConflict(
                                "Event changed remotely. Delete cancelled; review the refreshed event."
                            )
            if calendar.provider == "microsoft":
                current = current or self._fetch(calendar, event_id, token)
                expected = str(source.get("revision") or "")
                if scope == "single" and str(current.get("changeKey") or "") != expected:
                    self._store_raw(calendar, current)
                    raise MutationConflict("Event changed remotely. Delete cancelled; review the refreshed event.")
        except HttpError as error:
            if error.status in (404, 410):
                return self._finish_delete(uid, scope, event_id)
            raise
        headers = self._headers(token)
        if calendar.provider == "google":
            headers["If-Match"] = series_revision if current is not None else expected_revision
        try:
            self.write_http.request_json("DELETE", self._event_url(calendar, event_id), headers=headers)
        except HttpError as error:
            if error.status in (404, 410):
                return self._finish_delete(uid, scope, event_id)
            if calendar.provider == "google" and error.status == 412:
                self._store_raw(calendar, self._fetch(calendar, event_id, token))
                raise MutationConflict("Event changed remotely. Delete cancelled; review the refreshed event.") from error
            raise
        return self._finish_delete(uid, scope, event_id)

    def _finish_delete(self, uid: str, scope: str, event_id: str) -> dict[str, object]:
        self.store.remove_event(uid, series_id=event_id if scope == "series" else "")
        return {"deleted": True, "uid": uid, "scope": scope, "refresh": True}

    def _create(
        self,
        draft: EventDraft,
        existing_meeting_url: str = "",
        *,
        verify_copy: bool = False,
    ) -> tuple[dict[str, object], str]:
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
        raw, notice = self._fetch_after_write(
            calendar, event_id, token, draft.online_meeting == "new",
        )
        if verify_copy:
            self._verify_copy_destination(
                calendar, raw, event_id, draft, existing_meeting_url, notice,
            )
        return self._store_raw(calendar, raw), notice

    def _fetch_after_write(
        self,
        calendar: Calendar,
        event_id: str,
        token: Mapping[str, object],
        meeting_requested: bool,
    ) -> tuple[dict[str, Any], str]:
        raw: dict[str, Any] = {}
        for attempt in range(4):
            raw = self._fetch(calendar, event_id, token)
            if not meeting_requested or self._generated_meeting_ready(calendar.provider, raw):
                return raw, ""
            google_status = str(
                (((raw.get("conferenceData") or {}).get("createRequest") or {}).get("status") or {}).get("statusCode") or ""
            )
            if calendar.provider == "google" and google_status == "failure":
                return raw, "Event saved, but Google Meet could not be created."
            if attempt < 3:
                self.sleep(0.5)
        return raw, (
            "Event saved. Google Meet is still being prepared."
            if calendar.provider == "google"
            else "Event saved. Outlook is still preparing the meeting link."
        )

    @staticmethod
    def _generated_meeting_ready(provider: str, raw: Mapping[str, object]) -> bool:
        if provider == "microsoft":
            return is_safe_https_url(str((raw.get("onlineMeeting") or {}).get("joinUrl") or ""))
        if is_safe_https_url(str(raw.get("hangoutLink") or "")):
            return True
        return any(
            entry.get("entryPointType") == "video"
            and is_safe_https_url(str(entry.get("uri") or ""))
            for entry in (raw.get("conferenceData") or {}).get("entryPoints", [])
        )

    def _payload(
        self,
        draft: EventDraft,
        calendar: Calendar,
        existing_meeting_url: str,
        *,
        include_temporal: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        calendar = replace(calendar, timezone=self.timezone)
        if calendar.provider == "google":
            return google_payload(
                draft, calendar, existing_meeting_url,
                include_temporal=include_temporal,
            )
        return microsoft_payload(
            draft, calendar, existing_meeting_url,
            include_temporal=include_temporal,
        ), False

    def _token(self, calendar: Calendar) -> dict[str, Any]:
        token = self._access_token(calendar)
        scopes = set(str(token.get("scope") or "").split())
        if self._write_scope(calendar.provider) not in scopes:
            raise PermissionError("Read and edit permission is required")
        return token

    def _access_token(self, calendar: Calendar) -> dict[str, Any]:
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
        return token

    def _has_write_access(self, calendar: Calendar) -> bool:
        token = self.keyring.get(calendar.provider, calendar.account_id)
        scopes = set(str((token or {}).get("scope") or "").split())
        return self._write_scope(calendar.provider) in scopes

    @staticmethod
    def _write_scope(provider: str) -> str:
        return (
            "https://www.googleapis.com/auth/calendar.events.owned"
            if provider == "google" else "Calendars.ReadWrite"
        )

    def _calendar(self, key: str) -> Calendar:
        calendar = self.store.get_calendar(key)
        if calendar is None:
            raise DraftError("Destination calendar is not available")
        return calendar

    def _writable(self, calendar: Calendar) -> None:
        if not calendar.writable or not calendar.owned:
            raise PermissionError("Destination calendar is not owned and writable")
        if not calendar.sync_enabled:
            raise PermissionError("Destination calendar is not enabled for synchronization")
        health = self.store.health(calendar.provider, calendar.account_id)
        if health is None or not health.connected or health.stale:
            raise PermissionError("Editing is unavailable while this account is offline")

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
    def _recurring(source: Mapping[str, object]) -> bool:
        return bool(
            source.get("recurrence_id")
            or source.get("event_type") == "series"
            or source.get("recurrence")
        )

    @staticmethod
    def _series_only(source: Mapping[str, object]) -> bool:
        return source.get("event_type") == "series" and not source.get("recurrence_id")

    @staticmethod
    def _revision(calendar: Calendar, raw: Mapping[str, object]) -> str:
        return str(raw.get("etag" if calendar.provider == "google" else "changeKey") or "")

    @staticmethod
    def _unchanged_source(
        source: Mapping[str, object], expected: str, *, deleting: bool = False,
    ) -> None:
        if not expected:
            raise DraftError("Event revision is missing; refresh before editing")
        if str(source.get("revision") or "") != expected:
            action = "Delete cancelled" if deleting else "Draft preserved"
            raise MutationConflict(f"Event changed remotely. {action}; review the refreshed event.")

    @staticmethod
    def _series_baseline(draft: EventDraft, source: Mapping[str, object]) -> None:
        if not draft.series_revision or not draft.series_start or not draft.series_end:
            raise DraftError("Series details are missing; refresh before editing")
        if str(source.get("series_revision") or "") != draft.series_revision:
            raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.")
        source_start = str(source.get("series_start") or "")
        source_end = str(source.get("series_end") or "")
        if not source_start or not source_end:
            raise DraftError("Series details are missing; refresh before editing")
        try:
            if source.get("all_day"):
                unchanged = (
                    draft.series_start[:10] == source_start[:10]
                    and draft.series_end[:10] == source_end[:10]
                )
            else:
                unchanged = (
                    datetime.fromisoformat(draft.series_start).astimezone(timezone.utc)
                    == datetime.fromisoformat(source_start).astimezone(timezone.utc)
                    and datetime.fromisoformat(draft.series_end).astimezone(timezone.utc)
                    == datetime.fromisoformat(source_end).astimezone(timezone.utc)
                )
        except ValueError as error:
            raise DraftError("Series details are missing; refresh before editing") from error
        if not unchanged:
            raise MutationConflict("Event changed remotely. Draft preserved; review the refreshed event.")

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

    def _fetch_transfer_series(
        self,
        calendar: Calendar,
        event_id: str,
        token: Mapping[str, object],
    ) -> dict[str, Any]:
        if calendar.provider != "microsoft":
            return self._fetch(calendar, event_id, token)
        fields = (
            "id,changeKey,type,subject,body,bodyPreview,location,isAllDay,isCancelled,"
            "start,end,organizer,isOrganizer,onlineMeeting,onlineMeetingUrl,webLink,"
            "lastModifiedDateTime,recurrence,attendees,cancelledOccurrences,"
            "exceptionOccurrences"
        )
        query = urlencode({
            "$select": fields,
            "$expand": "exceptionOccurrences($select=id)",
        })
        return self.read_http.get_json(
            f"{self._event_url(calendar, event_id)}?{query}",
            headers=self._headers(token),
        )

    def _series_delete_blocker(
        self,
        calendar: Calendar,
        event_id: str,
        token: Mapping[str, object],
        master: Mapping[str, object],
    ) -> str:
        deviations = (
            "The original series has modified or cancelled occurrences, "
            "so it cannot be deleted after this copy."
        )
        unknown = (
            "Flight Deck could not verify every series occurrence, "
            "so the original cannot be deleted after this copy."
        )
        if calendar.provider == "microsoft":
            cancelled = master.get("cancelledOccurrences")
            exceptions = master.get("exceptionOccurrences")
            if (
                not isinstance(cancelled, list)
                or not isinstance(exceptions, list)
                or bool(master.get("exceptionOccurrences@odata.nextLink"))
            ):
                return unknown
            return deviations if cancelled or exceptions else ""
        query = {"singleEvents": "false", "showDeleted": "true", "maxResults": "2500"}
        seen_tokens: set[str] = set()
        try:
            while True:
                payload = self.read_http.get_json(
                    f"{self._collection_url(calendar)}?{urlencode(query)}",
                    headers=self._headers(token),
                )
                if not isinstance(payload, Mapping):
                    return unknown
                items = payload.get("items")
                if not isinstance(items, list):
                    return unknown
                if any(not isinstance(item, Mapping) for item in items):
                    return unknown
                if any(
                    str(item.get("recurringEventId") or "") == event_id
                    for item in items
                ):
                    return deviations
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token:
                    return ""
                if page_token in seen_tokens:
                    return unknown
                seen_tokens.add(page_token)
                query["pageToken"] = page_token
        except HttpError:
            return unknown

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

    def _master_seeded_series_draft(
        self,
        draft: EventDraft,
        source: Mapping[str, object],
        calendar: Calendar,
        current: Mapping[str, object],
    ) -> tuple[EventDraft, Event]:
        master = self._normalized_raw(calendar, dict(current))
        source_meeting_mode = "preserve" if source.get("meeting_url") else "none"
        return replace(
            draft,
            title=(
                master.title if draft.title == str(source.get("title") or "")
                else draft.title
            ),
            location=(
                master.location if draft.location == str(source.get("location") or "")
                else draft.location
            ),
            notes=(
                master.description if draft.notes == str(source.get("description") or "")
                else draft.notes
            ),
            online_meeting=(
                ("preserve" if master.meeting_url else "none")
                if draft.online_meeting == source_meeting_mode
                else draft.online_meeting
            ),
        ), master

    def _series_draft(
        self,
        draft: EventDraft,
        source: Mapping[str, object],
        *,
        shift_recurrence: bool = False,
    ) -> EventDraft:
        zone = _zone(self.timezone)
        if source.get("all_day"):
            source_start = datetime.combine(
                date.fromisoformat(str(source["start_day"])), time.min, zone,
            )
            source_end = datetime.combine(
                date.fromisoformat(str(source["end_day"])), time.min, zone,
            )
            master_start = datetime.combine(
                date.fromisoformat(draft.series_start[:10]), time.min, zone,
            )
            master_end = datetime.combine(
                date.fromisoformat(draft.series_end[:10]), time.min, zone,
            )
        else:
            source_start = datetime.fromisoformat(str(source["start"])).astimezone(zone)
            source_end = datetime.fromisoformat(str(source["end"])).astimezone(zone)
            master_start = datetime.fromisoformat(draft.series_start).astimezone(zone)
            master_end = datetime.fromisoformat(draft.series_end).astimezone(zone)
        if draft.all_day:
            moved = master_start.date() + (date.fromisoformat(draft.day) - source_start.date())
            draft_duration = date.fromisoformat(draft.end_day) - date.fromisoformat(draft.day)
            if source.get("all_day"):
                duration = (master_end.date() - master_start.date()) + (
                    draft_duration - (source_end.date() - source_start.date())
                )
            else:
                duration = draft_duration
            if duration <= timedelta(0):
                raise DraftError("Event end must be after its start")
            return replace(
                draft,
                day=moved.isoformat(),
                end_day=(moved + duration).isoformat(),
                start="",
                end="",
                recurrence=(
                    _shifted_recurrence(draft.recurrence, (moved - master_start.date()).days)
                    if shift_recurrence else draft.recurrence
                ),
            )
        draft_start = _local_datetime(draft.day, draft.start, self.timezone)
        draft_end = _local_datetime(draft.end_day, draft.end, self.timezone)
        moved = master_start + (draft_start - source_start)
        draft_duration = draft_end - draft_start
        duration = (
            (master_end - master_start) + (draft_duration - (source_end - source_start))
            if not source.get("all_day") else draft_duration
        )
        if duration <= timedelta(0):
            raise DraftError("Event end must be after its start")
        moved_end = moved + duration
        return replace(
            draft,
            day=moved.date().isoformat(),
            end_day=moved_end.date().isoformat(),
            start=moved.strftime("%H:%M"),
            end=moved_end.strftime("%H:%M"),
            recurrence=(
                _shifted_recurrence(draft.recurrence, (moved.date() - master_start.date()).days)
                if shift_recurrence else draft.recurrence
            ),
        )

    @staticmethod
    def _source_recurrence(calendar: Calendar, raw: Mapping[str, object]) -> Recurrence:
        try:
            start = raw.get("start") or {}
            series_timezone = (
                str(start.get("timeZone") or calendar.timezone)
                if isinstance(start, Mapping) else calendar.timezone
            )
            return (
                _google_source_recurrence(raw, series_timezone)
                if calendar.provider == "google"
                else _microsoft_source_recurrence(raw)
            )
        except DraftError:
            raise
        except (TypeError, ValueError) as error:
            raise DraftError("This series recurrence is not supported for transfer") from error

    def _verify_copy_destination(
        self,
        calendar: Calendar,
        raw: dict[str, Any],
        event_id: str,
        draft: EventDraft,
        existing_meeting_url: str,
        notice: str,
    ) -> None:
        event = self._normalized_raw(calendar, raw)
        expected_location = (
            _without_meeting_link(draft.location, existing_meeting_url)
            if existing_meeting_url and draft.online_meeting != "preserve"
            else draft.location
        )
        expected_notes = _notes_with_meeting(
            draft.notes, existing_meeting_url, draft.online_meeting,
        )
        matches = (
            event.provider == calendar.provider
            and event.account_id == calendar.account_id
            and event.calendar_id == calendar.calendar_id
            and event.provider_event_id == event_id
            and event.title == draft.title
            and event.all_day == draft.all_day
            and event.location == plain_text(expected_location, 500)
            and event.description == plain_text(expected_notes, 8192)
        )
        if draft.all_day:
            matches = matches and event.start_day == draft.day and event.end_day == draft.end_day
        else:
            expected_start = _local_datetime(draft.day, draft.start, self.timezone)
            expected_end = _local_datetime(draft.end_day, draft.end, self.timezone)
            try:
                actual_start = datetime.fromisoformat(event.start).astimezone(timezone.utc)
                actual_end = datetime.fromisoformat(event.end).astimezone(timezone.utc)
            except ValueError:
                matches = False
            else:
                matches = matches and (
                    actual_start == expected_start.astimezone(timezone.utc)
                    and actual_end == expected_end.astimezone(timezone.utc)
                )

        expected_recurrence = (
            Recurrence() if draft.recurrence.frequency == "preserve" else draft.recurrence
        )
        if raw.get("recurrence"):
            try:
                actual_recurrence = self._source_recurrence(calendar, raw)
            except DraftError:
                matches = False
            else:
                matches = matches and self._recurrence_semantics(actual_recurrence) == self._recurrence_semantics(
                    expected_recurrence
                )
                if calendar.provider == "microsoft":
                    range_data = (raw.get("recurrence") or {}).get("range") or {}
                    matches = matches and str(range_data.get("startDate") or "") == draft.day
                    recurrence_timezone = str(range_data.get("recurrenceTimeZone") or "")
                else:
                    recurrence_timezone = str((raw.get("start") or {}).get("timeZone") or "")
                if not draft.all_day:
                    matches = matches and recurrence_timezone == self.timezone
        else:
            matches = matches and expected_recurrence.frequency == "none"

        expected_meeting = extract_meeting_url(expected_location, expected_notes)
        if draft.online_meeting == "new":
            if not notice:
                matches = matches and is_safe_https_url(event.meeting_url)
        else:
            matches = matches and event.meeting_url == expected_meeting
        if not matches:
            raise DraftError(
                "Destination copy could not be verified. The original is unchanged; "
                "check the destination before retrying."
            )

    @staticmethod
    def _recurrence_semantics(recurrence: Recurrence) -> tuple[object, ...]:
        pattern = (
            "weekly"
            if recurrence.frequency in ("weekdays", "weekly", "selected_weekdays")
            else recurrence.frequency
        )
        return (
            pattern,
            frozenset(recurrence.weekdays),
            recurrence.end,
            recurrence.count,
            recurrence.until,
        )

    def _temporal_changed(
        self,
        draft: EventDraft,
        source: Mapping[str, object],
    ) -> bool:
        if draft.all_day != bool(source.get("all_day")):
            return True
        if draft.all_day:
            return (
                draft.day != str(source.get("start_day") or "")
                or draft.end_day != str(source.get("end_day") or "")
            )
        zone = _zone(self.timezone)
        source_start = datetime.fromisoformat(str(source["start"])).astimezone(zone)
        source_end = datetime.fromisoformat(str(source["end"])).astimezone(zone)
        return (
            f"{draft.day}T{draft.start}" != source_start.strftime("%Y-%m-%dT%H:%M")
            or f"{draft.end_day}T{draft.end}" != source_end.strftime("%Y-%m-%dT%H:%M")
        )

    def _series_day_changed(
        self,
        draft: EventDraft,
        source: Mapping[str, object],
    ) -> bool:
        if source.get("all_day"):
            return draft.day != str(source.get("start_day") or "")
        source_start = datetime.fromisoformat(str(source["start"])).astimezone(_zone(self.timezone))
        return draft.day != source_start.date().isoformat()

    def _store_series_result(
        self,
        calendar: Calendar,
        raw: dict[str, Any],
        source: Mapping[str, object],
        draft: EventDraft,
        *,
        temporal_changed: bool,
    ) -> dict[str, object]:
        master = self._normalized_raw(calendar, raw)
        if master.event_type != "series":
            self.store.upsert_event(master)
            stored = self.store.get_event(master.uid)
        else:
            zone = _zone(self.timezone)
            start_day = draft.day if temporal_changed else str(source.get("start_day") or draft.day)
            end_day = draft.end_day if temporal_changed else str(source.get("end_day") or draft.end_day)
            if temporal_changed:
                start = datetime.combine(date.fromisoformat(draft.day), time.min, zone)
                end = datetime.combine(date.fromisoformat(draft.end_day), time.min, zone)
                if not draft.all_day:
                    start = _local_datetime(draft.day, draft.start, self.timezone)
                    end = _local_datetime(draft.end_day, draft.end, self.timezone)
                start_value = start.isoformat()
                end_value = end.isoformat()
            else:
                start_value = str(source["start"])
                end_value = str(source["end"])
            preview = replace(
                master,
                start=start_value,
                end=end_value,
                all_day=draft.all_day,
                timezone=self.timezone if temporal_changed else str(source.get("timezone") or self.timezone),
                start_day=start_day,
                end_day=end_day,
            )
            self.store.upsert_event(preview)
            stored = self.store.get_event(preview.uid)
        if stored is None:
            raise RuntimeError("Updated series could not be cached")
        return stored

    def _store_created_series_preview(
        self,
        event: Mapping[str, object],
        selected_draft: EventDraft,
        payload_draft: EventDraft,
    ) -> dict[str, object]:
        if event.get("event_type") != "series":
            return dict(event)
        created = Event(**{
            name: event[name] for name in Event.__dataclass_fields__
        })
        zone = _zone(self.timezone)
        if payload_draft.all_day:
            start_day = date.fromisoformat(selected_draft.day)
            duration = date.fromisoformat(payload_draft.end_day) - date.fromisoformat(payload_draft.day)
            end_day = start_day + duration
            start = datetime.combine(start_day, time.min, zone)
            end = datetime.combine(end_day, time.min, zone)
        else:
            start_day = date.fromisoformat(selected_draft.day)
            payload_start = _local_datetime(payload_draft.day, payload_draft.start, self.timezone)
            payload_end = _local_datetime(payload_draft.end_day, payload_draft.end, self.timezone)
            start = datetime.combine(start_day, payload_start.timetz().replace(tzinfo=None), zone)
            end = start + (payload_end - payload_start)
            end_day = end.date()
        preview = replace(
            created,
            start=start.isoformat(),
            end=end.isoformat(),
            all_day=payload_draft.all_day,
            timezone=self.timezone,
            start_day=start_day.isoformat(),
            end_day=end_day.isoformat(),
        )
        self.store.upsert_event(preview)
        stored = self.store.get_event(preview.uid)
        if stored is None:
            raise RuntimeError("Created series could not be cached")
        return stored

    def _store_raw(self, calendar: Calendar, raw: dict[str, Any]) -> dict[str, object]:
        event = self._normalized_raw(calendar, raw)
        self.store.upsert_event(event)
        stored = self.store.get_event(event.uid)
        if stored is None:
            raise RuntimeError("Created event could not be cached")
        return stored
