#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Approval-gated provider verification using disposable calendars only."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


HELPER = Path(os.environ.get("FLIGHT_DECK_LIVE_HELPER", "calendarctl"))
CONTRACT_ONLY = os.environ.get("FLIGHT_DECK_LIVE_CONTRACT_ONLY") == "1"
CALENDAR_NAMES = {
    ("google", "source"): os.environ["FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR"],
    ("google", "destination"): os.environ["FLIGHT_DECK_GOOGLE_DESTINATION_CALENDAR"],
    ("microsoft", "source"): os.environ["FLIGHT_DECK_OUTLOOK_SOURCE_CALENDAR"],
    ("microsoft", "destination"): os.environ["FLIGHT_DECK_OUTLOOK_DESTINATION_CALENDAR"],
}
RUN_ID = f"FD-E2E-{uuid.uuid4().hex[:12]}"


def call(
    *arguments: str,
    payload: dict[str, object] | None = None,
    ok: bool = True,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    runtime = os.environ.copy()
    runtime.update(environment or {})
    result = subprocess.run(
        [str(HELPER), *arguments],
        input=json.dumps(payload) + "\n" if payload is not None else None,
        text=True,
        capture_output=True,
        env=runtime,
        check=False,
    )
    if not ok:
        return {
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
            "stdout": result.stdout.strip(),
        }
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{' '.join(arguments)} failed")
    return json.loads(result.stdout)


def window() -> tuple[str, str]:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=45)).isoformat()


def view() -> dict[str, object]:
    start, end = window()
    return call("view", "--from", start, "--to", end)


def sync(provider: str) -> None:
    for attempt in range(3):
        try:
            result = call("sync", "--provider", provider)
            break
        except RuntimeError as error:
            if attempt == 2 or not any(
                marker in str(error) for marker in ("TimeoutError", "timed out")
            ):
                raise
    if int(result.get("failed") or 0):
        raise RuntimeError(f"{provider} sync failed")
    if "synced" in result and int(result["synced"]) != 1:
        raise RuntimeError(f"{provider} sync did not refresh exactly one account")


def calendar(provider: str, role: str) -> dict[str, object]:
    name = CALENDAR_NAMES[(provider, role)]
    matches = [
        item for item in view()["calendars"]
        if item["provider"] == provider and item["name"] == name
        and item.get("owned") is True and item.get("writable") is True
        and item.get("sync_enabled") is not False
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one enabled owned writable {provider} calendar named {name!r}")
    return matches[0]


def expect_connected(*providers: str) -> None:
    records = call("setup-status")["providers"]
    actual = {str(item["provider"]) for item in records if item.get("connected")}
    if actual != set(providers):
        raise RuntimeError(f"Expected connected providers {sorted(providers)}, got {sorted(actual)}")


def expect_editing(provider: str, account_id: str, expected: bool) -> None:
    record = next(
        item for item in call("setup-status")["providers"]
        if item["provider"] == provider
    )
    editing = account_id in record.get("editing_account_ids", [])
    if editing is not expected:
        raise RuntimeError(f"Unexpected {provider} editing state")


def recurrence(frequency: str = "none", **extra: object) -> dict[str, object]:
    return {"frequency": frequency, "weekdays": [], "end": "never", **extra}


def draft(destination: dict[str, object], title: str, *, day: date, hour: int = 10) -> dict[str, object]:
    return {
        "request_id": str(uuid.uuid4()),
        "calendar_key": destination["key"],
        "title": title,
        "day": day.isoformat(),
        "end_day": day.isoformat(),
        "start": f"{hour:02}:00",
        "end": f"{hour + 1:02}:00",
        "all_day": False,
        "location": "Flight Deck test",
        "notes": RUN_ID,
        "recurrence": recurrence(),
        "online_meeting": "none",
        "source_uid": "",
        "source_revision": "",
        "series_revision": "",
        "series_start": "",
        "series_end": "",
        "scope": "single",
    }


def calendar_day_time(value: object, timezone: object) -> tuple[str, str]:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        ZoneInfo(str(timezone))
    )
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def from_event(item: dict[str, object], **changes: object) -> dict[str, object]:
    recurring = bool(
        item.get("recurrence_id") or item.get("recurrence")
        or item.get("event_type") == "series"
    )
    if item["all_day"]:
        day, start = str(item["start_day"]), ""
        end_day, end = str(item["end_day"]), ""
    else:
        day, start = calendar_day_time(item["start"], item["timezone"])
        end_day, end = calendar_day_time(item["end"], item["timezone"])
    value = {
        "request_id": str(uuid.uuid4()),
        "calendar_key": item["calendar_key"],
        "title": item["title"],
        "day": day,
        "end_day": end_day,
        "start": start,
        "end": end,
        "all_day": item["all_day"],
        "location": item.get("location") or "",
        "notes": item.get("description") or "",
        "recurrence": recurrence("preserve" if recurring else "none"),
        "online_meeting": "preserve" if item.get("meeting_url") else "none",
        "source_uid": item["uid"],
        "source_revision": item.get("revision") or "",
        "series_revision": item.get("series_revision") or "",
        "series_start": item.get("series_start") or "",
        "series_end": item.get("series_end") or "",
        "scope": "series" if item.get("event_type") == "series" else "single",
    }
    value.update(changes)
    return value


def verify_event(
    item: dict[str, object],
    expected: dict[str, object],
    destination: dict[str, object],
    *,
    preserved_meeting: str = "",
) -> None:
    for field, value in (
        ("calendar_key", destination["key"]),
        ("provider", destination["provider"]),
        ("account_id", destination["account_id"]),
        ("title", expected["title"]),
        ("all_day", expected["all_day"]),
        ("location", expected["location"]),
    ):
        if item.get(field) != value:
            raise RuntimeError(f"Provider result did not preserve {field}")
    if expected["all_day"]:
        actual_times = (item.get("start_day"), item.get("end_day"))
        expected_times = (expected["day"], expected["end_day"])
    else:
        actual_start = calendar_day_time(item["start"], destination["timezone"])
        actual_end = calendar_day_time(item["end"], destination["timezone"])
        actual_times = (*actual_start, *actual_end)
        expected_times = (
            expected["day"], expected["start"],
            expected["end_day"], expected["end"],
        )
    if actual_times != expected_times:
        raise RuntimeError("Provider result did not preserve event timing")
    if RUN_ID not in str(item.get("description") or ""):
        raise RuntimeError("Provider result did not preserve the test-run identifier")
    frequency = str((expected.get("recurrence") or {}).get("frequency") or "none")
    if frequency not in ("none", "preserve") and not (
        item.get("recurrence_id") or item.get("recurrence") or item.get("event_type") == "series"
    ):
        raise RuntimeError("Provider result did not preserve recurrence")
    if expected.get("online_meeting") == "new" and not item.get("meeting_url"):
        raise RuntimeError("Provider did not return the requested online meeting")
    if preserved_meeting and item.get("meeting_url") != preserved_meeting:
        raise RuntimeError("Provider copy did not preserve the source meeting")


def find_synced(
    expected: dict[str, object],
    destination: dict[str, object],
    preferred_uid: str,
) -> dict[str, object]:
    events = view()["events"]
    exact = [item for item in events if item.get("uid") == preferred_uid]
    if exact:
        return exact[0]
    matches = [
        item for item in events
        if item.get("calendar_key") == destination["key"]
        and item.get("title") == expected["title"]
        and item.get("start_day") == expected["day"]
    ]
    if not matches:
        raise RuntimeError("Provider event did not reappear after sync")
    return matches[0]


def write(
    command: str,
    expected: dict[str, object],
    destination: dict[str, object],
    *,
    preserved_meeting: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    result = call(command, payload=expected)
    immediate = result.get("event")
    if not isinstance(immediate, dict):
        raise RuntimeError(f"{command} returned no event")
    verify_event(immediate, expected, destination, preserved_meeting=preserved_meeting)
    sync(str(destination["provider"]))
    current = find_synced(expected, destination, str(immediate["uid"]))
    verify_event(current, expected, destination, preserved_meeting=preserved_meeting)
    return current, result


def event_named(title: str) -> dict[str, object]:
    matches = [item for item in view()["events"] if item.get("title") == title]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one provider event named {title!r}")
    return matches[0]


def delete(item: dict[str, object], *, series_transfer_guard: bool = False) -> None:
    scope = "series" if item.get("recurrence_id") or item.get("event_type") == "series" else "single"
    call("delete-event", payload={
        "uid": item["uid"],
        "scope": scope,
        "confirmed": True,
        "expected_revision": item.get("revision") or "",
        "series_revision": item.get("series_revision") or "",
        "series_transfer_guard": series_transfer_guard,
    })
    sync(str(item["provider"]))


def delete_original(result: dict[str, object]) -> None:
    if result.get("delete_original_available") is not True:
        raise RuntimeError(str(result.get("delete_original_reason") or "Source deletion was not offered"))
    payload = {
        "uid": result["original_uid"],
        "scope": result.get("original_scope") or "single",
        "confirmed": True,
        "expected_revision": result.get("original_revision") or "",
        "series_revision": result.get("original_series_revision") or "",
        "series_transfer_guard": result.get("original_scope") == "series",
    }
    provider = str(result["original_provider"])
    deletion = call("delete-event", payload=payload, ok=False)
    if int(deletion["returncode"]):
        if "Event changed remotely" not in str(deletion["stderr"]):
            raise RuntimeError(str(deletion["stderr"]) or "Source deletion failed")
        sync(provider)
        current = next(
            (
                item for item in view()["events"]
                if item.get("uid") == result["original_uid"]
            ),
            None,
        )
        if current is None:
            raise RuntimeError("A source conflict removed the original unexpectedly")
        call("delete-event", payload={
            **payload,
            "expected_revision": current.get("revision") or "",
            "series_revision": current.get("series_revision") or "",
        })
    sync(provider)
    if any(item.get("uid") == result["original_uid"] for item in view()["events"]):
        raise RuntimeError("Confirmed source deletion did not remove the original")


def exercise_provider(
    provider: str,
    source: dict[str, object],
    destination: dict[str, object],
    test_day: date,
) -> dict[str, dict[str, object]]:
    prefix = "Google" if provider == "google" else "Outlook"
    timed, _ = write("create-event", draft(
        source, f"{RUN_ID} {prefix} timed", day=test_day,
    ), source)
    quick_draft = from_event(
        timed, request_id=str(uuid.uuid4()), start="10:15", end="11:15",
        title=f"{RUN_ID} {prefix} quick move",
    )
    quick, _ = write("update-event", quick_draft, source)
    next_day = test_day + timedelta(days=1)
    dragged, _ = write("update-event", from_event(
        quick, request_id=str(uuid.uuid4()), day=next_day.isoformat(),
        end_day=next_day.isoformat(), title=f"{RUN_ID} {prefix} drag",
    ), source)
    resized, _ = write("update-event", from_event(
        dragged, request_id=str(uuid.uuid4()), end="11:30",
        title=f"{RUN_ID} {prefix} resize",
    ), source)
    conflict = call("update-event", payload={
        **quick_draft,
        "request_id": str(uuid.uuid4()),
        "title": f"{RUN_ID} conflict",
    }, ok=False)
    if conflict["returncode"] == 0:
        raise RuntimeError("A stale provider update was not rejected")
    sync(provider)
    if event_named(str(resized["title"]))["uid"] != resized["uid"]:
        raise RuntimeError("A conflict replaced the current event")

    all_day_draft = draft(source, f"{RUN_ID} {prefix} all day", day=next_day)
    all_day_draft.update({
        "all_day": True,
        "start": "",
        "end": "",
        "end_day": (next_day + timedelta(days=1)).isoformat(),
    })
    all_day, _ = write("create-event", all_day_draft, source)
    moved_day = next_day + timedelta(days=1)
    all_day, _ = write("update-event", from_event(
        all_day,
        request_id=str(uuid.uuid4()),
        day=moved_day.isoformat(),
        end_day=(moved_day + timedelta(days=1)).isoformat(),
        title=f"{RUN_ID} {prefix} all-day move",
    ), source)

    recurrence_day = test_day + timedelta(days=2)
    weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[recurrence_day.weekday()]
    recurring_draft = draft(source, f"{RUN_ID} {prefix} recurring", day=recurrence_day)
    recurring_draft["recurrence"] = recurrence(
        "selected_weekdays", weekdays=[weekday], end="count", count=3,
    )
    recurring, _ = write("create-event", recurring_draft, source)
    recurring, _ = write("update-event", from_event(
        recurring,
        request_id=str(uuid.uuid4()),
        scope="single",
        title=f"{RUN_ID} {prefix} occurrence",
    ), source)
    series_id = str(recurring.get("recurrence_id") or "")
    series_source = next(
        (
            item for item in view()["events"]
            if item.get("calendar_key") == source["key"]
            and item.get("recurrence_id") == series_id
            and item.get("uid") != recurring.get("uid")
        ),
        None,
    )
    if series_source is None:
        raise RuntimeError("Recurring series did not expose an unmodified occurrence")
    recurring, _ = write("update-event", from_event(
        series_source,
        request_id=str(uuid.uuid4()),
        scope="series",
        title=f"{RUN_ID} {prefix} series",
    ), source)

    meeting_mode = "new" if source.get("meeting_providers") else "none"
    meeting, _ = write("create-event", {
        **draft(source, f"{RUN_ID} {prefix} meeting", day=test_day, hour=12),
        "online_meeting": meeting_mode,
    }, source)
    if meeting_mode == "none" and meeting.get("meeting_url"):
        raise RuntimeError("Provider returned an unrequested online meeting")
    duplicate, _ = write("copy-event", from_event(
        resized,
        request_id=str(uuid.uuid4()),
        title=f"{RUN_ID} {prefix} duplicate",
    ), source)
    same_provider, copy_result = write("copy-event", from_event(
        resized,
        request_id=str(uuid.uuid4()),
        calendar_key=destination["key"],
        title=f"{RUN_ID} {prefix} same-provider copy",
    ), destination)
    if copy_result.get("delete_original_available") is not True:
        raise RuntimeError("Verified same-provider transfer did not offer source deletion")

    retry_draft = draft(source, f"{RUN_ID} {prefix} retry", day=test_day + timedelta(days=3))
    retry_draft["request_id"] = str(uuid.uuid4())
    first, _ = write("create-event", retry_draft, source)
    second, _ = write("create-event", retry_draft, source)
    if first["uid"] != second["uid"]:
        raise RuntimeError("Provider retry created a duplicate event")
    if sum(item.get("uid") == first["uid"] for item in view()["events"]) != 1:
        raise RuntimeError("Provider retry left duplicate cached events")
    return {
        "event": resized,
        "all_day": all_day,
        "recurring": recurring,
        "meeting": meeting,
        "duplicate": duplicate,
        "same_provider": same_provider,
    }


def secret_token(provider: str, account_id: str) -> dict[str, object]:
    result = subprocess.run(
        [
            "secret-tool", "lookup",
            "application", "omarchy-calendar",
            "provider", provider,
            "account", account_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read the isolated {provider} token")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Secret Service returned invalid token data")
    return value


def store_token(provider: str, account_id: str, token: dict[str, object]) -> None:
    result = subprocess.run(
        [
            "secret-tool", "store", f"--label=Flight Deck E2E {provider}",
            "application", "omarchy-calendar",
            "provider", provider,
            "account", account_id,
        ],
        input=json.dumps(token, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not update the isolated {provider} token")


def prove_v1_upgrade(account_id: str) -> None:
    if CONTRACT_ONLY:
        return
    before_token = secret_token("google", account_id)
    item = next(event for event in view()["events"] if event["provider"] == "google")
    database = Path(os.environ["XDG_STATE_HOME"]) / "omarchy-calendar" / "calendar.db"
    legacy = database.with_suffix(".v1")
    if legacy.exists():
        legacy.unlink()
    connection = sqlite3.connect(legacy)
    connection.executescript(
        """
        CREATE TABLE events (
          uid TEXT PRIMARY KEY, provider TEXT NOT NULL, account_id TEXT NOT NULL,
          account_label TEXT NOT NULL, calendar_id TEXT NOT NULL,
          calendar_name TEXT NOT NULL, calendar_color TEXT NOT NULL,
          title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          all_day INTEGER NOT NULL CHECK (all_day IN (0, 1)), status TEXT NOT NULL,
          location TEXT NOT NULL, description TEXT NOT NULL, organizer TEXT NOT NULL,
          meeting_url TEXT NOT NULL, provider_url TEXT NOT NULL, updated TEXT NOT NULL
        );
        CREATE INDEX events_window ON events (start, end);
        CREATE INDEX events_account ON events (provider, account_id);
        CREATE TABLE provider_health (
          provider TEXT NOT NULL, account_id TEXT NOT NULL,
          connected INTEGER NOT NULL CHECK (connected IN (0, 1)),
          last_sync TEXT NOT NULL, last_error TEXT NOT NULL, retry_after TEXT NOT NULL,
          stale INTEGER NOT NULL CHECK (stale IN (0, 1)),
          demo INTEGER NOT NULL CHECK (demo IN (0, 1)), skipped INTEGER NOT NULL,
          PRIMARY KEY (provider, account_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(item.get(field) or "" for field in (
            "uid", "provider", "account_id", "account_label", "calendar_id",
            "calendar_name", "calendar_color", "title", "start", "end",
        )) + (
            int(bool(item["all_day"])), item.get("status") or "confirmed",
            item.get("location") or "", item.get("description") or "",
            item.get("organizer") or "", item.get("meeting_url") or "",
            item.get("provider_url") or "", item.get("updated") or "",
        ),
    )
    connection.execute(
        "INSERT INTO provider_health VALUES (?,?,?,?,?,?,?,?,?)",
        ("google", account_id, 1, datetime.now().astimezone().isoformat(), "", "", 0, 0, 0),
    )
    connection.commit()
    connection.close()
    os.chmod(legacy, 0o600)
    os.replace(legacy, database)
    sync("google")
    after_token = secret_token("google", account_id)
    for key in ("access_token", "refresh_token"):
        if after_token.get(key) != before_token.get(key):
            raise RuntimeError("v1 upgrade replaced a valid read-only token")
    expect_editing("google", account_id, False)


def force_refresh(provider: str, account_id: str) -> None:
    if CONTRACT_ONLY:
        return
    token = secret_token(provider, account_id)
    token["expires_at"] = 0
    store_token(provider, account_id, token)
    sync(provider)
    refreshed = secret_token(provider, account_id)
    if float(refreshed.get("expires_at") or 0) <= time.time():
        raise RuntimeError(f"{provider} token did not refresh")
    write_scope = (
        "https://www.googleapis.com/auth/calendar.events.owned"
        if provider == "google" else "Calendars.ReadWrite"
    )
    if write_scope not in set(str(refreshed.get("scope") or "").split()):
        raise RuntimeError(f"{provider} refresh lost editing permission")


def prove_offline(item: dict[str, object]) -> None:
    if CONTRACT_ONLY:
        return
    before = event_named(str(item["title"]))
    offline = {
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "NO_PROXY": "",
        "no_proxy": "",
    }
    result = call("update-event", payload=from_event(
        before,
        request_id=str(uuid.uuid4()),
        title=f"{RUN_ID} offline must not save",
    ), ok=False, environment=offline)
    if result["returncode"] == 0:
        raise RuntimeError("Offline provider write unexpectedly succeeded")
    after = next(event for event in view()["events"] if event["uid"] == before["uid"])
    if after["title"] != before["title"]:
        raise RuntimeError("Offline write changed the cached event")


def tagged_events(provider: str = "") -> list[dict[str, object]]:
    return [
        item for item in view()["events"]
        if RUN_ID in f"{item.get('title', '')} {item.get('description', '')}"
        and (not provider or item.get("provider") == provider)
        and not item.get("has_attendees") and item.get("organizer_owned") is True
    ]


def cleanup(*, strict: bool = False, provider: str = "") -> None:
    providers = (provider,) if provider else ("google", "microsoft")
    for current_provider in providers:
        try:
            sync(current_provider)
        except Exception:
            pass
    handled_series: set[tuple[str, str, str]] = set()
    for item in tagged_events(provider):
        series = str(item.get("recurrence_id") or "")
        series_key = (str(item["provider"]), str(item["account_id"]), series)
        if series and series_key in handled_series:
            continue
        if series:
            handled_series.add(series_key)
        try:
            delete(item)
        except Exception as error:
            if "Event changed remotely" in str(error):
                try:
                    sync(str(item["provider"]))
                    refreshed = next(
                        (
                            current for current in tagged_events(provider)
                            if current.get("uid") == item.get("uid")
                        ),
                        None,
                    )
                    if refreshed is not None:
                        delete(refreshed)
                        continue
                except Exception as retry_error:
                    error = retry_error
            if strict:
                raise
            print(f"CLEANUP FAILED {item.get('uid')}: {error}", file=sys.stderr)
    if strict:
        for current_provider in providers:
            sync(current_provider)
        if tagged_events(provider):
            raise RuntimeError("Tagged provider events remain after cleanup")


def prove_revoked(provider: str, account_id: str) -> None:
    if CONTRACT_ONLY:
        return
    before = [
        (item["uid"], item["title"])
        for item in view()["events"] if item["provider"] == provider
    ]
    token = secret_token(provider, account_id)
    token.update({
        "access_token": f"{RUN_ID}-invalid",
        "refresh_token": f"{RUN_ID}-invalid",
        "expires_at": 0,
    })
    store_token(provider, account_id, token)
    result = call("sync", "--provider", provider)
    if int(result.get("failed") or 0) != 1:
        raise RuntimeError(f"{provider} revoked token was not rejected")
    after = [
        (item["uid"], item["title"])
        for item in view()["events"] if item["provider"] == provider
    ]
    if after != before:
        raise RuntimeError(f"{provider} revoked access changed cached events")


def main() -> int:
    if len(set(CALENDAR_NAMES.values())) != 4:
        raise RuntimeError("Live E2E requires four distinct disposable calendar names")
    call("reset-local-data")
    test_day = date.today() + timedelta(days=14)
    print("Complete each Google and Outlook browser consent window when it opens.")

    call("auth", "google", "--access", "read")
    sync("google")
    expect_connected("google")
    google_source = calendar("google", "source")
    google_destination = calendar("google", "destination")
    if google_source["account_id"] != google_destination["account_id"]:
        raise RuntimeError("Google source and destination must belong to one test account")
    expect_editing("google", str(google_source["account_id"]), False)
    prove_v1_upgrade(str(google_source["account_id"]))
    call("enable-editing", "google", "--account", str(google_source["account_id"]))
    expect_editing("google", str(google_source["account_id"]), True)
    exercise_provider("google", google_source, google_destination, test_day)

    call("auth", "microsoft", "--access", "read")
    sync("microsoft")
    expect_connected("google", "microsoft")
    outlook_source = calendar("microsoft", "source")
    outlook_destination = calendar("microsoft", "destination")
    if outlook_source["account_id"] != outlook_destination["account_id"]:
        raise RuntimeError("Outlook source and destination must belong to one test account")
    outlook_status = next(
        item for item in call("setup-status")["providers"]
        if item["provider"] == "microsoft"
    )
    if str(outlook_source["account_id"]) not in outlook_status.get("editing_account_ids", []):
        call("enable-editing", "microsoft", "--account", str(outlook_source["account_id"]))
    expect_editing("microsoft", str(outlook_source["account_id"]), True)
    outlook_results = exercise_provider(
        "microsoft", outlook_source, outlook_destination, test_day + timedelta(days=7),
    )

    expect_connected("google", "microsoft")
    google_meeting, _ = write("create-event", {
        **draft(google_source, f"{RUN_ID} combined Google meeting", day=test_day, hour=16),
        "online_meeting": "new",
    }, google_source)
    outlook_meeting = event_named(str(outlook_results["meeting"]["title"]))
    _, google_to_outlook = write("copy-event", from_event(
        google_meeting,
        request_id=str(uuid.uuid4()),
        calendar_key=outlook_destination["key"],
        title=f"{RUN_ID} Google to Outlook",
    ), outlook_destination, preserved_meeting=str(google_meeting["meeting_url"]))
    if google_to_outlook.get("delete_original_available") is not True:
        raise RuntimeError("Google to Outlook transfer did not offer source deletion")
    _, outlook_to_google = write("copy-event", from_event(
        outlook_meeting,
        request_id=str(uuid.uuid4()),
        calendar_key=google_destination["key"],
        title=f"{RUN_ID} Outlook to Google",
    ), google_destination, preserved_meeting=str(outlook_meeting["meeting_url"]))
    delete_original(outlook_to_google)

    force_refresh("google", str(google_source["account_id"]))
    force_refresh("microsoft", str(outlook_source["account_id"]))
    prove_offline(event_named(str(google_meeting["title"])))
    prove_offline(event_named(str(outlook_results["event"]["title"])))
    cleanup(strict=True)
    prove_revoked("google", str(google_source["account_id"]))
    prove_revoked("microsoft", str(outlook_source["account_id"]))
    call("disconnect", "google", "--account", str(google_source["account_id"]))
    call("disconnect", "microsoft", "--account", str(outlook_source["account_id"]))
    call("reset-local-data")
    if view()["events"] or view()["calendars"]:
        raise RuntimeError("Reset left provider data in the isolated profile")
    expect_connected()
    print(f"PASS live disposable-calendar provider matrix {RUN_ID}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        cleanup()
