#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic calendarctl double for installed UI tests. Never contacts a provider."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(os.environ.get("FLIGHT_DECK_E2E_ROOT", Path(__file__).resolve().parent / ".e2e"))
STATE = ROOT / "state.json"
LOG = ROOT / "commands.jsonl"


def calendar(key: str, provider: str, account: str, name: str) -> dict[str, object]:
    meeting_providers = [] if (
        provider == "microsoft"
        and os.environ.get("FLIGHT_DECK_E2E_MICROSOFT_NO_MEETINGS") == "1"
    ) else ["googleMeet" if provider == "google" else "teamsForBusiness"]
    return {
        "key": key, "provider": provider, "account_id": account,
        "account_label": account, "name": name, "color": "#7aa2f7",
        "writable": True, "owned": True, "sync_enabled": True,
        "timezone": "America/Chicago",
        "meeting_providers": meeting_providers,
    }


def event(uid: str, calendar_key: str, provider: str, account: str, title: str, hour: int, day: str) -> dict[str, object]:
    start = datetime.fromisoformat(day).replace(hour=hour)
    return {
        "uid": uid, "provider": provider, "provider_event_id": uid,
        "account_id": account, "calendar_id": calendar_key, "calendar_key": calendar_key,
        "calendar_name": "Verification", "calendar_color": "#7aa2f7", "title": title,
        "start": start.isoformat() + "-05:00", "end": (start + timedelta(hours=1)).isoformat() + "-05:00",
        "start_day": day, "end_day": day, "all_day": False,
        "location": "Test room", "description": "Deterministic local test event.",
        "meeting_url": "", "provider_url": f"https://calendar.example.test/{uid}",
        "organizer_owned": True, "has_attendees": False, "revision": "1",
        "series_revision": "", "series_start": "", "series_end": "",
        "recurrence_id": "", "recurrence": [], "event_type": "default",
        "timezone": "America/Chicago",
    }


def initial_state(day: str = "2026-09-03") -> dict[str, object]:
    accounts = [
        ("google", "google-read", "google-main", "Google Verification", "Read-only design review", 9),
        ("google", "google-denial", "google-denial-calendar", "Denied account", "Denied account event", 11),
        ("google", "google-wrong", "google-wrong-calendar", "Wrong account", "Wrong account event", 13),
        ("microsoft", "outlook-read", "outlook-main", "Outlook Verification", "Outlook planning", 15),
    ]
    calendars = [calendar(key, provider, account, name) for provider, account, key, name, _, _ in accounts]
    calendars.extend((
        calendar("google-transfer", "google", "google-read", "Google Transfer"),
        calendar("outlook-transfer", "microsoft", "outlook-read", "Outlook Transfer"),
    ))
    events = [event(f"{provider}:{account}:event", key, provider, account, title, hour, day)
              for provider, account, key, _, title, hour in accounts]
    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    series_day = (datetime.fromisoformat(day) - timedelta(days=28)).date().isoformat()
    all_day = event("google:google-read:all-day", "google-main", "google", "google-read", "All-day release", 8, day)
    all_day.update({"start": day, "end": next_day, "end_day": next_day, "all_day": True})
    recurring = event("google:google-read:recurring", "google-main", "google", "google-read", "Weekly review", 10, day)
    recurring.update({
        "recurrence_id": f"{day}T10:00:00-05:00", "recurrence": ["RRULE:FREQ=WEEKLY"],
        "series_revision": "series-1", "series_start": f"{series_day}T10:00:00-05:00",
        "series_end": f"{series_day}T11:00:00-05:00",
    })
    events.extend((all_day, recurring))
    health = [{
        "provider": provider, "account_id": account, "connected": True, "stale": False,
        "last_sync": "2026-09-03T08:00:00-05:00", "last_error": "", "demo": False,
    } for provider, account, *_ in accounts]
    microsoft_editing = (
        ["outlook-read"]
        if os.environ.get("FLIGHT_DECK_E2E_PREGRANTED_MICROSOFT_EDIT") == "1"
        else []
    )
    return {
        "calendars": calendars,
        "events": events,
        "health": health,
        "editing": {"google": [], "microsoft": microsoft_editing},
    }


def save(state: dict[str, object]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(STATE)
    STATE.chmod(0o600)


def load() -> dict[str, object]:
    if not STATE.is_file():
        save(initial_state())
    return json.loads(STATE.read_text(encoding="utf-8"))


def emit(value: object) -> None:
    print(json.dumps(value, separators=(",", ":")))


def record(arguments: list[str], payload: dict[str, object] | None = None) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"arguments": arguments, "stdin": payload}, separators=(",", ":")) + "\n")


def read_payload() -> dict[str, object]:
    raw = sys.stdin.readline(65537)
    if len(raw.encode()) > 65536:
        raise ValueError("Event JSON is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Event input must be a JSON object")
    return value


def setup(state: dict[str, object]) -> dict[str, object]:
    providers = []
    for provider, label in (("google", "Google"), ("microsoft", "Outlook")):
        health = [row for row in state["health"] if row["provider"] == provider]
        editing = list(state["editing"][provider])
        providers.append({
            "provider": provider, "label": label, "client_configured": True,
            "registration_source": "bundled", "connected": bool(health), "accounts": len(health),
            "editing": bool(editing), "edit_accounts": len(editing), "editing_account_ids": editing,
            "stale": any(row["stale"] for row in health), "last_sync": health[0]["last_sync"] if health else "",
            "last_error": next((row["last_error"] for row in health if row["last_error"]), ""),
        })
    return {"providers": providers, "demo": False}


def provider_for(state: dict[str, object], calendar_key: str) -> dict[str, object]:
    return next(row for row in state["calendars"] if row["key"] == calendar_key)


def iso(day: str, value: str) -> str:
    return f"{day}T{value}:00-05:00"


def from_draft(state: dict[str, object], draft: dict[str, object], uid: str) -> dict[str, object]:
    destination = provider_for(state, str(draft["calendar_key"]))
    day, end_day = str(draft["day"]), str(draft.get("end_day") or draft["day"])
    all_day = draft.get("all_day") is True
    recurrence = draft.get("recurrence") if isinstance(draft.get("recurrence"), dict) else {}
    frequency = str(recurrence.get("frequency") or "none")
    recurring = frequency not in ("", "none")
    start = day if all_day else iso(day, str(draft["start"]))
    end = end_day if all_day else iso(end_day, str(draft["end"]))
    return {
        "uid": uid, "provider": destination["provider"], "provider_event_id": uid,
        "account_id": destination["account_id"], "calendar_id": destination["key"],
        "calendar_key": destination["key"], "calendar_name": destination["name"],
        "calendar_color": destination["color"], "title": str(draft.get("title") or "Untitled event"),
        "start": start, "end": end,
        "start_day": day, "end_day": end_day, "all_day": all_day,
        "location": str(draft.get("location") or ""), "description": str(draft.get("notes") or ""),
        "meeting_url": (
            "https://meet.google.com/test-link"
            if draft.get("online_meeting") == "new" and destination["provider"] == "google"
            else "https://teams.microsoft.com/l/meetup-join/test-link"
            if draft.get("online_meeting") == "new"
            else ""
        ),
        "provider_url": f"https://calendar.example.test/{uid}", "organizer_owned": True,
        "has_attendees": False, "revision": "2",
        "series_revision": "series-2" if recurring else "",
        "series_start": start if recurring else "", "series_end": end if recurring else "",
        "recurrence_id": f"series:{uid}" if recurring else "",
        "recurrence": [f"RRULE:FREQ={frequency.upper()}"] if recurring else [],
        "event_type": "occurrence" if recurring else "single",
        "timezone": destination["timezone"],
    }


def reconnect(state: dict[str, object], provider: str) -> None:
    account = "google-read" if provider == "google" else "outlook-read"
    fixture = initial_state()
    for key in ("calendars", "events", "health"):
        present = {
            (row["provider"], row["account_id"], row.get("key") or row.get("uid") or "")
            for row in state[key]
        }
        for row in fixture[key]:
            identity = (row["provider"], row["account_id"], row.get("key") or row.get("uid") or "")
            if row["account_id"] == account and identity not in present:
                state[key].append(row)
    if (
        provider == "microsoft"
        and os.environ.get("FLIGHT_DECK_E2E_PREGRANTED_MICROSOFT_EDIT") == "1"
        and account not in state["editing"][provider]
    ):
        state["editing"][provider].append(account)
    save(state)


def mutate(command: str, state: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
    outcome = ROOT / "mutation-outcome"
    if outcome.is_file():
        message = outcome.read_text(encoding="utf-8").strip()
        outcome.unlink()
        raise RuntimeError(message)
    if command == "delete-event":
        if payload.get("confirmed") is not True:
            raise ValueError("Event deletion requires explicit confirmation")
        uid = str(payload.get("uid") or "")
        source = next(row for row in state["events"] if row["uid"] == uid)
        drift = ROOT / "cleanup-drift-used"
        if (
            os.environ.get("FLIGHT_DECK_E2E_CLEANUP_REVISION_DRIFT") == "1"
            and str(source.get("title") or "").endswith("Outlook resize")
            and not drift.exists()
        ):
            source["revision"] = str(int(source["revision"]) + 1)
            save(state)
            drift.touch()
            raise RuntimeError("Event changed remotely. Delete cancelled; review the refreshed event.")
        transfer_drift = ROOT / "transfer-delete-drift-used"
        if (
            os.environ.get("FLIGHT_DECK_E2E_TRANSFER_DELETE_REVISION_DRIFT") == "1"
            and str(source.get("title") or "").endswith("Outlook meeting")
            and not transfer_drift.exists()
        ):
            source["revision"] = str(int(source["revision"]) + 1)
            save(state)
            transfer_drift.touch()
            raise RuntimeError("Event changed remotely. Delete cancelled; review the refreshed event.")
        if source["account_id"] not in state["editing"][source["provider"]]:
            raise PermissionError("Editing is not enabled for this account")
        series = str(source.get("recurrence_id") or "")
        state["events"] = [
            row for row in state["events"]
            if row["uid"] != uid and not (
                payload.get("scope") == "series" and series
                and row.get("recurrence_id") == series
            )
        ]
        save(state)
        return {"deleted": uid}
    source_uid = str(payload.get("source_uid") or "")
    destination = provider_for(state, str(payload["calendar_key"]))
    if destination["account_id"] not in state["editing"][destination["provider"]]:
        raise PermissionError("Editing is not enabled for this account")
    if "conflict" in str(payload.get("title") or "").casefold():
        raise RuntimeError("Event changed remotely. The local draft was preserved.")
    if command == "update-event":
        current = next(row for row in state["events"] if row["uid"] == source_uid)
        updated = from_draft(state, payload, source_uid)
        if payload.get("online_meeting") == "preserve":
            updated["meeting_url"] = current.get("meeting_url") or ""
        updated["revision"] = str(int(current["revision"]) + 1)
        if payload.get("scope") == "series" and current.get("recurrence_id"):
            series_id = current["recurrence_id"]
            state["events"] = [
                {
                    **row,
                    "title": updated["title"],
                    "location": updated["location"],
                    "description": updated["description"],
                    "revision": updated["revision"],
                }
                if row.get("recurrence_id") == series_id else row
                for row in state["events"]
            ]
        else:
            state["events"] = [updated if row["uid"] == source_uid else row for row in state["events"]]
        save(state)
        return {"event": updated}
    uid = f"e2e:{payload.get('request_id') or len(state['events'])}"
    existing = next((row for row in state["events"] if row["uid"] == uid), None)
    if existing is not None:
        return {"event": existing}
    created = from_draft(state, payload, uid)
    source = next((row for row in state["events"] if row["uid"] == source_uid), None)
    if payload.get("online_meeting") == "preserve" and source is not None:
        created["meeting_url"] = source.get("meeting_url") or ""
    state["events"].append(created)
    if created.get("recurrence_id"):
        for number in (2, 3):
            peer = dict(created)
            peer["uid"] = peer["provider_event_id"] = f"{uid}:{number}"
            for field in ("start", "end"):
                peer[field] = (
                    datetime.fromisoformat(str(peer[field])) + timedelta(days=7 * (number - 1))
                ).isoformat()
            peer["start_day"] = peer["start"][:10]
            peer["end_day"] = peer["end"][:10]
            state["events"].append(peer)
    save(state)
    if command == "copy-event":
        source = next(row for row in state["events"] if row["uid"] == source_uid)
        return {
            "event": created, "original_uid": source_uid, "original_scope": str(payload.get("scope") or "single"),
            "original_revision": source["revision"], "original_series_revision": source["series_revision"],
            "original_provider": source["provider"], "original_account_id": source["account_id"],
            "delete_original_available": created["calendar_key"] != source["calendar_key"], "delete_original_needs_permission": False,
            "delete_original_reason": "Copy saved and verified. The original remains unchanged.",
        }
    return {"event": created}


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if not arguments:
        print("calendarctl command is required", file=sys.stderr)
        return 2
    try:
        state = load()
        command = arguments[0]
        payload = read_payload() if command in {"create-event", "update-event", "copy-event", "delete-event"} else None
        record(arguments, payload)
        if arguments[:2] == ["demo", "seed"]:
            day = arguments[arguments.index("--date") + 1] if "--date" in arguments else "2026-09-03"
            datetime.fromisoformat(day)
            state = initial_state(day); save(state); emit({"seeded": len(state["events"]), "accounts": 4, "date": day, "demo": True}); return 0
        if arguments[:2] == ["demo", "clear"]:
            save(initial_state()); emit({"cleared_accounts": 4, "demo": False}); return 0
        if command == "view":
            emit({"events": state["events"], "calendars": state["calendars"], "providers": state["health"], "demo": False}); return 0
        if command == "status":
            emit({"providers": state["health"], "database": str(STATE)}); return 0
        if command == "setup-status":
            emit(setup(state)); return 0
        if command == "sync":
            provider = arguments[arguments.index("--provider") + 1] if "--provider" in arguments else ""
            connected = [
                row for row in state["health"]
                if not provider or row["provider"] == provider
            ]
            emit({"synced": len(connected), "failed": 0, "skipped": 0, "accounts": connected}); return 0
        if command == "enable-editing":
            provider = arguments[1]
            account = arguments[arguments.index("--account") + 1]
            if not os.environ.get("FLIGHT_DECK_E2E_NATIVE") and "denial" in account:
                raise RuntimeError("Access was denied. Read-only access is unchanged.")
            if not os.environ.get("FLIGHT_DECK_E2E_NATIVE") and "wrong" in account:
                raise RuntimeError("The browser returned a different account. Read-only access is unchanged.")
            if account not in state["editing"][provider]:
                state["editing"][provider].append(account)
            save(state); emit({"provider": provider, "account_id": account, "access": "edit"}); return 0
        if command == "disconnect":
            provider = arguments[1]
            account = arguments[arguments.index("--account") + 1] if "--account" in arguments else ""
            state["health"] = [row for row in state["health"] if not (row["provider"] == provider and (not account or row["account_id"] == account))]
            state["events"] = [row for row in state["events"] if not (row["provider"] == provider and (not account or row["account_id"] == account))]
            state["calendars"] = [row for row in state["calendars"] if not (row["provider"] == provider and (not account or row["account_id"] == account))]
            if account in state["editing"][provider]: state["editing"][provider].remove(account)
            save(state); emit({"provider": provider, "disconnected": 1}); return 0
        if command == "reset-local-data":
            save({"calendars": [], "events": [], "health": [], "editing": {"google": [], "microsoft": []}}); emit({"events": 0, "accounts": 0, "provider_overrides": 0}); return 0
        if command in {"create-event", "update-event", "copy-event", "delete-event"}:
            emit(mutate(command, state, payload or {})); return 0
        if command in {"open-meeting", "open-source", "copy-meeting"}:
            emit({"ok": True}); return 0
        if command == "auth":
            reconnect(state, arguments[1]); emit({"configured": True}); return 0
        if command in {"configure-client", "import-google-desktop-app"}:
            emit({"configured": True}); return 0
        raise ValueError("Unsupported deterministic helper command")
    except (ValueError, KeyError, StopIteration, json.JSONDecodeError, RuntimeError, PermissionError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
