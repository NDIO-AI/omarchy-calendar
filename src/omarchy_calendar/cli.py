# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .auth_service import Authenticator
from .cache import CalendarStore
from .demo import ZONE, demo_events
from .keyring import KeyringError, SecretServiceStore, redact
from .mutations import EventDraft, MutationService
from .normalize import is_safe_https_url
from .settings import ProviderSettings
from .sync import SyncEngine


def state_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "omarchy-calendar" / "calendar.db"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="calendarctl", description="Omarchy calendar helper")
    commands = root.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth")
    auth.add_argument("provider", choices=("google", "microsoft"))
    auth.add_argument("--access", choices=("read", "edit"), default="read")
    enable = commands.add_parser("enable-editing")
    enable.add_argument("provider", choices=("google", "microsoft"))
    enable.add_argument("--account", default="")
    sync = commands.add_parser("sync")
    sync.add_argument("--provider", choices=("google", "microsoft"))
    view = commands.add_parser("view")
    view.add_argument("--from", dest="start", required=True)
    view.add_argument("--to", dest="end", required=True)
    commands.add_parser("status")
    commands.add_parser("setup-status")
    configure = commands.add_parser("configure-client")
    configure.add_argument("provider", choices=("microsoft",))
    configure.add_argument("client_id")
    import_google = commands.add_parser("import-google-desktop-app")
    import_google.add_argument("credentials_json")
    disconnect = commands.add_parser("disconnect")
    disconnect.add_argument("provider", choices=("google", "microsoft"))
    disconnect.add_argument("--account")
    commands.add_parser("reset-local-data")
    for name in ("create-event", "update-event", "copy-event", "delete-event"):
        commands.add_parser(name)
    for name in ("open-meeting", "open-source"):
        action = commands.add_parser(name)
        action.add_argument("uid")
    copy_meeting = commands.add_parser("copy-meeting")
    copy_meeting.add_argument("uid")
    demo = commands.add_parser("demo")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    seed = demo_commands.add_parser("seed")
    seed.add_argument("--date")
    demo_commands.add_parser("clear")
    return root


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def read_stdin_json(stream=sys.stdin, *, limit: int = 65536) -> dict[str, object]:
    raw = stream.readline(limit + 1)
    if len(raw.encode("utf-8")) > limit:
        raise ValueError("Event JSON is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("Event input must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Event input must be a JSON object")
    return payload


def seed_demo(store: CalendarStore, selected_day: date) -> dict[str, object]:
    events, health = demo_events(selected_day)
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for event in events:
        grouped[(event.provider, event.account_id)].append(event)
    start = datetime.combine(selected_day - timedelta(days=1), time.min, ZONE).isoformat()
    end = datetime.combine(selected_day + timedelta(days=3), time.min, ZONE).isoformat()
    health_by_account = {(item.provider, item.account_id): item for item in health}
    for key, account_events in grouped.items():
        store.replace_window(*key, start, end, account_events, health_by_account[key])
    return {"seeded": len(events), "accounts": len(grouped), "date": selected_day.isoformat(), "demo": True}


def open_event_url(
    store: CalendarStore,
    uid: str,
    field: str,
    *,
    command: str | None = None,
    runner=subprocess.run,
) -> int:
    event = store.get_event(uid)
    if event is None:
        print("Event is not available in the local cache", file=sys.stderr)
        return 4
    url = str(event.get(field) or "")
    if not is_safe_https_url(url):
        print("This event does not provide that action", file=sys.stderr)
        return 4
    executable = command or os.environ.get("OMARCHY_CALENDAR_OPEN_COMMAND", "xdg-open")
    runner([executable, url], check=False)
    return 0


def copy_meeting_url(
    store: CalendarStore,
    uid: str,
    *,
    command: str | None = None,
    runner=subprocess.run,
) -> int:
    event = store.get_event(uid)
    url = str(event.get("meeting_url") or "") if event else ""
    if not is_safe_https_url(url):
        print("This event does not provide a meeting link", file=sys.stderr)
        return 4
    executable = command or os.environ.get("OMARCHY_CALENDAR_COPY_COMMAND", "wl-copy")
    try:
        result = runner([executable], input=url, text=True, check=False)
    except OSError:
        print("Could not copy the meeting link", file=sys.stderr)
        return 5
    if getattr(result, "returncode", 0) != 0:
        print("Could not copy the meeting link", file=sys.stderr)
        return 5
    return 0


def setup_status(
    store: CalendarStore,
    settings: ProviderSettings,
    keyring: SecretServiceStore | None = None,
) -> dict[str, object]:
    health = store.health_records()
    token_store = keyring or SecretServiceStore()
    providers = []
    for provider, label in (("google", "Google"), ("microsoft", "Outlook")):
        real = [item for item in health if item["provider"] == provider and not item["demo"]]
        connected = [item for item in real if item["connected"]]
        configured = bool(settings.client_id(provider))
        if provider == "google" and configured:
            configured = bool(
                settings.google_app_credential(token_store)
            )
        editing_accounts = []
        token_get = getattr(token_store, "get", None)
        if token_get:
            for account in connected:
                try:
                    token = token_get(provider, str(account["account_id"])) or {}
                except KeyringError:
                    token = {}
                scopes = set(str(token.get("scope") or "").split())
                write_scope = (
                    "https://www.googleapis.com/auth/calendar.events.owned"
                    if provider == "google" else "Calendars.ReadWrite"
                )
                if write_scope in scopes:
                    editing_accounts.append(str(account["account_id"]))
        providers.append({
            "provider": provider,
            "label": label,
            "client_configured": configured,
            "registration_source": settings.registration_source(provider),
            "connected": bool(connected),
            "accounts": len(real),
            "editing": bool(editing_accounts),
            "edit_accounts": len(editing_accounts),
            "editing_account_ids": editing_accounts,
            "stale": any(bool(item["stale"]) for item in real),
            "last_sync": str(real[0]["last_sync"]) if real else "",
            "last_error": next((str(item["last_error"]) for item in real if item["last_error"]), ""),
        })
    return {
        "providers": providers,
        "demo": any(bool(item["demo"]) for item in health),
    }


def edit_account_id(store: CalendarStore, provider: str, requested: str) -> str:
    accounts = [
        str(item["account_id"])
        for item in store.accounts(provider)
        if item["connected"]
    ]
    if requested:
        if requested not in accounts:
            raise ValueError("The connected account is not available")
        return requested
    if len(accounts) != 1:
        raise ValueError("Use --account when the provider does not have exactly one connected account")
    return accounts[0]


def disconnect_provider(
    store: CalendarStore,
    keyring: SecretServiceStore,
    provider: str,
    account_id: str | None = None,
) -> dict[str, object]:
    accounts = store.accounts(provider)
    if account_id:
        accounts = [item for item in accounts if item["account_id"] == account_id]
    removed = 0
    for account in accounts:
        account = str(account["account_id"])
        keyring.clear(provider, account)
        removed += store.remove_account(provider, account)
    return {"provider": provider, "disconnected": removed}


def reset_local_data(
    store: CalendarStore,
    keyring: SecretServiceStore,
    settings_path: str | Path | None = None,
) -> dict[str, object]:
    keyring.clear_all()
    removed = store.clear_all()
    removed["provider_overrides"] = int(ProviderSettings.clear(settings_path))
    return removed


def import_google_desktop_app(
    source: str | Path,
    keyring: SecretServiceStore,
    settings_path: str | Path | None = None,
) -> dict[str, object]:
    source_text = os.fspath(source)
    parsed = urlparse(source_text)
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise ValueError("Google Desktop credentials must be a local file")
        if parsed.query or parsed.fragment:
            raise ValueError("Google Desktop credentials must be a plain local file")
        source = Path(unquote(parsed.path))
    source = Path(source)
    if not source.is_absolute():
        raise ValueError("Google Desktop credentials must be an absolute local file")
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Google Desktop credentials must be a regular file")
            if metadata.st_size > 65536:
                raise ValueError("Google Desktop credentials file is too large")
            raw = os.read(descriptor, 65537)
        finally:
            os.close(descriptor)
        if len(raw) > 65536:
            raise ValueError("Google Desktop credentials file is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Google Desktop credentials file is not valid JSON") from error
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        raise ValueError("Google credentials must be for a Desktop app")
    client_id = str(installed.get("client_id") or "").strip()
    credential = str(installed.get("client_secret") or "").strip()
    settings = ProviderSettings.load(settings_path).with_client_id("google", client_id)
    keyring.put_app_credential("google", credential)
    settings.save(settings_path)
    return {"provider": "google", "configured": True}


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        with CalendarStore(state_path()) as store:
            if arguments.command == "demo":
                if arguments.demo_command == "seed":
                    selected = date.fromisoformat(arguments.date) if arguments.date else datetime.now(ZONE).date()
                    emit(seed_demo(store, selected))
                else:
                    emit({"cleared_accounts": store.clear_demo(), "demo": False})
                return 0
            if arguments.command == "view":
                emit(store.view(arguments.start, arguments.end))
                return 0
            if arguments.command == "status":
                emit({"providers": store.health_records(), "database": str(store.path)})
                return 0
            if arguments.command == "setup-status":
                emit(setup_status(store, ProviderSettings.load(), SecretServiceStore()))
                return 0
            if arguments.command == "configure-client":
                ProviderSettings.load().with_client_id(arguments.provider, arguments.client_id).save()
                emit({"provider": arguments.provider, "configured": True})
                return 0
            if arguments.command == "import-google-desktop-app":
                emit(import_google_desktop_app(arguments.credentials_json, SecretServiceStore()))
                return 0
            if arguments.command == "auth":
                settings = ProviderSettings.load()
                if not settings.client_id(arguments.provider):
                    print(f"{arguments.provider} public client ID is not configured", file=sys.stderr)
                    return 3
                emit(Authenticator(store, settings=settings).authenticate(
                    arguments.provider, access=arguments.access,
                ))
                return 0
            if arguments.command == "enable-editing":
                settings = ProviderSettings.load()
                emit(Authenticator(store, settings=settings).authenticate(
                    arguments.provider,
                    access="edit",
                    expected_account_id=edit_account_id(store, arguments.provider, arguments.account),
                ))
                return 0
            if arguments.command == "sync":
                emit(SyncEngine(store).sync(arguments.provider))
                return 0
            if arguments.command == "disconnect":
                emit(disconnect_provider(
                    store,
                    SecretServiceStore(),
                    arguments.provider,
                    arguments.account,
                ))
                return 0
            if arguments.command == "reset-local-data":
                emit(reset_local_data(store, SecretServiceStore()))
                return 0
            if arguments.command in ("create-event", "update-event", "copy-event"):
                draft = EventDraft.from_dict(read_stdin_json())
                service = MutationService(store)
                operation = {
                    "create-event": service.create,
                    "update-event": service.update,
                    "copy-event": service.copy,
                }[arguments.command]
                emit(operation(draft))
                return 0
            if arguments.command == "delete-event":
                payload = read_stdin_json()
                if payload.get("confirmed") is not True:
                    raise ValueError("Event deletion requires explicit confirmation")
                uid = str(payload.get("uid") or "")
                if not uid or len(uid) > 2048:
                    raise ValueError("Event identity is invalid")
                emit(MutationService(store).delete_event(
                    uid,
                    scope=str(payload.get("scope") or "single"),
                    expected_revision=str(payload.get("expected_revision") or ""),
                    series_revision=str(payload.get("series_revision") or ""),
                    series_transfer_guard=payload.get("series_transfer_guard") is True,
                ))
                return 0
            if arguments.command == "open-meeting":
                return open_event_url(store, arguments.uid, "meeting_url")
            if arguments.command == "open-source":
                return open_event_url(store, arguments.uid, "provider_url")
            if arguments.command == "copy-meeting":
                result = copy_meeting_url(store, arguments.uid)
                if result == 0:
                    emit({"copied": True})
                return result
    except (ValueError, RuntimeError, PermissionError, KeyringError) as error:
        print(redact(str(error)), file=sys.stderr)
        return 2
    return 2
