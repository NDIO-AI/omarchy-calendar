# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Calendar, Event, ProviderHealth


EVENT_COLUMNS = (
    "uid",
    "provider",
    "account_id",
    "account_label",
    "calendar_id",
    "calendar_name",
    "calendar_color",
    "title",
    "start",
    "end",
    "all_day",
    "status",
    "location",
    "description",
    "organizer",
    "meeting_url",
    "provider_url",
    "updated",
    "provider_event_id",
    "revision",
    "timezone",
    "recurrence_id",
    "recurrence",
    "event_type",
    "organizer_owned",
    "start_day",
    "end_day",
    "series_revision",
    "series_start",
    "series_end",
    "has_attendees",
)

CALENDAR_COLUMNS = (
    "provider",
    "account_id",
    "account_label",
    "calendar_id",
    "name",
    "color",
    "timezone",
    "writable",
    "owned",
    "sync_enabled",
    "meeting_providers",
)


class CalendarStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.connection = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              uid TEXT PRIMARY KEY,
              provider TEXT NOT NULL,
              account_id TEXT NOT NULL,
              account_label TEXT NOT NULL,
              calendar_id TEXT NOT NULL,
              calendar_name TEXT NOT NULL,
              calendar_color TEXT NOT NULL,
              title TEXT NOT NULL,
              start TEXT NOT NULL,
              end TEXT NOT NULL,
              all_day INTEGER NOT NULL CHECK (all_day IN (0, 1)),
              status TEXT NOT NULL,
              location TEXT NOT NULL,
              description TEXT NOT NULL,
              organizer TEXT NOT NULL,
              meeting_url TEXT NOT NULL,
              provider_url TEXT NOT NULL,
              updated TEXT NOT NULL,
              provider_event_id TEXT NOT NULL DEFAULT '',
              revision TEXT NOT NULL DEFAULT '',
              timezone TEXT NOT NULL DEFAULT '',
              recurrence_id TEXT NOT NULL DEFAULT '',
              recurrence TEXT NOT NULL DEFAULT '[]',
              event_type TEXT NOT NULL DEFAULT 'single',
              organizer_owned INTEGER NOT NULL DEFAULT 0 CHECK (organizer_owned IN (0, 1)),
              start_day TEXT NOT NULL DEFAULT '',
              end_day TEXT NOT NULL DEFAULT '',
              series_revision TEXT NOT NULL DEFAULT '',
              series_start TEXT NOT NULL DEFAULT '',
              series_end TEXT NOT NULL DEFAULT '',
              has_attendees INTEGER NOT NULL DEFAULT 0 CHECK (has_attendees IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS events_window
              ON events (start, end);
            CREATE INDEX IF NOT EXISTS events_account
              ON events (provider, account_id);
            CREATE TABLE IF NOT EXISTS provider_health (
              provider TEXT NOT NULL,
              account_id TEXT NOT NULL,
              connected INTEGER NOT NULL CHECK (connected IN (0, 1)),
              last_sync TEXT NOT NULL,
              last_error TEXT NOT NULL,
              retry_after TEXT NOT NULL,
              stale INTEGER NOT NULL CHECK (stale IN (0, 1)),
              demo INTEGER NOT NULL CHECK (demo IN (0, 1)),
              skipped INTEGER NOT NULL,
              PRIMARY KEY (provider, account_id)
            );
            CREATE TABLE IF NOT EXISTS calendars (
              provider TEXT NOT NULL,
              account_id TEXT NOT NULL,
              account_label TEXT NOT NULL,
              calendar_id TEXT NOT NULL,
              name TEXT NOT NULL,
              color TEXT NOT NULL,
              timezone TEXT NOT NULL,
              writable INTEGER NOT NULL CHECK (writable IN (0, 1)),
              owned INTEGER NOT NULL CHECK (owned IN (0, 1)),
              sync_enabled INTEGER NOT NULL DEFAULT 1 CHECK (sync_enabled IN (0, 1)),
              meeting_providers TEXT NOT NULL,
              PRIMARY KEY (provider, account_id, calendar_id)
            );
            """
        )
        self._migrate_events()
        self._migrate_calendars()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO calendars
              (provider, account_id, account_label, calendar_id, name, color,
               timezone, writable, owned, sync_enabled, meeting_providers)
            SELECT provider, account_id, account_label, calendar_id,
                   calendar_name, calendar_color, '', 0, 0, 1, '[]'
              FROM events
             GROUP BY provider, account_id, calendar_id
            """
        )
        self.connection.commit()

    def _migrate_events(self) -> None:
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(events)").fetchall()
        }
        additions = {
            "provider_event_id": "TEXT NOT NULL DEFAULT ''",
            "revision": "TEXT NOT NULL DEFAULT ''",
            "timezone": "TEXT NOT NULL DEFAULT ''",
            "recurrence_id": "TEXT NOT NULL DEFAULT ''",
            "recurrence": "TEXT NOT NULL DEFAULT '[]'",
            "event_type": "TEXT NOT NULL DEFAULT 'single'",
            "organizer_owned": "INTEGER NOT NULL DEFAULT 0 CHECK (organizer_owned IN (0, 1))",
            "start_day": "TEXT NOT NULL DEFAULT ''",
            "end_day": "TEXT NOT NULL DEFAULT ''",
            "series_revision": "TEXT NOT NULL DEFAULT ''",
            "series_start": "TEXT NOT NULL DEFAULT ''",
            "series_end": "TEXT NOT NULL DEFAULT ''",
            "has_attendees": "INTEGER NOT NULL DEFAULT 1 CHECK (has_attendees IN (0, 1))",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")

    def _migrate_calendars(self) -> None:
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(calendars)").fetchall()
        }
        if "sync_enabled" not in existing:
            self.connection.execute(
                "ALTER TABLE calendars ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 1 CHECK (sync_enabled IN (0, 1))"
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CalendarStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def replace_window(
        self,
        provider: str,
        account_id: str,
        start: str,
        end: str,
        events: Iterable[Event],
        health: ProviderHealth,
        *,
        calendars: Iterable[Calendar] | None = None,
    ) -> None:
        if health.provider != provider or health.account_id != account_id:
            raise ValueError("provider health does not match replacement account")
        event_list = list(events)
        rows = [self._event_values(event) for event in event_list]
        if calendars is None:
            derived = {
                event.calendar_id: Calendar(
                    provider=event.provider,
                    account_id=event.account_id,
                    account_label=event.account_label,
                    calendar_id=event.calendar_id,
                    name=event.calendar_name,
                    color=event.calendar_color,
                    timezone=event.timezone,
                    writable=False,
                    owned=False,
                )
                for event in event_list
            }
            calendar_rows = [self._calendar_values(item) for item in derived.values()]
            replace_calendars = False
        else:
            calendar_rows = [self._calendar_values(item) for item in calendars]
            replace_calendars = True
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM events
                 WHERE provider = ? AND account_id = ?
                   AND julianday(end) > julianday(?)
                   AND julianday(start) < julianday(?)
                """,
                (provider, account_id, start, end),
            )
            self.connection.executemany(
                f"INSERT OR REPLACE INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({', '.join('?' for _ in EVENT_COLUMNS)})",
                rows,
            )
            if replace_calendars:
                self.connection.execute(
                    "DELETE FROM calendars WHERE provider = ? AND account_id = ?",
                    (provider, account_id),
                )
                self.connection.executemany(
                    f"INSERT INTO calendars ({', '.join(CALENDAR_COLUMNS)}) VALUES ({', '.join('?' for _ in CALENDAR_COLUMNS)})",
                    calendar_rows,
                )
            else:
                self.connection.executemany(
                    f"""
                    INSERT INTO calendars ({', '.join(CALENDAR_COLUMNS)})
                    VALUES ({', '.join('?' for _ in CALENDAR_COLUMNS)})
                    ON CONFLICT(provider, account_id, calendar_id) DO UPDATE SET
                      account_label=excluded.account_label,
                      name=excluded.name,
                      color=excluded.color,
                      timezone=excluded.timezone
                    """,
                    calendar_rows,
                )
            self.connection.execute(
                """
                INSERT INTO provider_health
                  (provider, account_id, connected, last_sync, last_error,
                   retry_after, stale, demo, skipped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id) DO UPDATE SET
                  connected=excluded.connected,
                  last_sync=excluded.last_sync,
                  last_error=excluded.last_error,
                  retry_after=excluded.retry_after,
                  stale=excluded.stale,
                  demo=excluded.demo,
                  skipped=excluded.skipped
                """,
                self._health_values(health),
            )

    def view(self, start: str, end: str) -> dict[str, object]:
        event_rows = self.connection.execute(
            """
            SELECT * FROM events
             WHERE julianday(end) > julianday(?)
               AND julianday(start) < julianday(?)
             ORDER BY julianday(start), all_day DESC, title COLLATE NOCASE
            """,
            (start, end),
        ).fetchall()
        health_rows = self.connection.execute(
            "SELECT * FROM provider_health ORDER BY provider, account_id"
        ).fetchall()
        calendar_rows = self.connection.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM events e
                     WHERE e.provider = c.provider
                       AND e.account_id = c.account_id
                       AND e.calendar_id = c.calendar_id) AS event_count
              FROM calendars c
             ORDER BY c.provider, c.name COLLATE NOCASE, c.account_label COLLATE NOCASE
            """
        ).fetchall()
        providers = [self._public_health(row) for row in health_rows]
        return {
            "events": [self._public_event(row) for row in event_rows],
            "calendars": [self._public_calendar(row) for row in calendar_rows],
            "providers": providers,
            "demo": any(bool(row["demo"]) for row in health_rows),
        }

    def clear_demo(self) -> int:
        accounts = self.connection.execute(
            "SELECT provider, account_id FROM provider_health WHERE demo = 1"
        ).fetchall()
        with self.connection:
            for row in accounts:
                self.connection.execute(
                    "DELETE FROM events WHERE provider = ? AND account_id = ?",
                    (row["provider"], row["account_id"]),
                )
                self.connection.execute(
                    "DELETE FROM calendars WHERE provider = ? AND account_id = ?",
                    (row["provider"], row["account_id"]),
                )
            self.connection.execute("DELETE FROM provider_health WHERE demo = 1")
        return len(accounts)

    def clear_all(self) -> dict[str, int]:
        events = int(self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        providers = int(self.connection.execute("SELECT COUNT(*) FROM provider_health").fetchone()[0])
        with self.connection:
            self.connection.execute("DELETE FROM events")
            self.connection.execute("DELETE FROM calendars")
            self.connection.execute("DELETE FROM provider_health")
        return {"events": events, "providers": providers}

    def accounts(self, provider: str | None = None, *, include_demo: bool = False) -> list[dict[str, object]]:
        conditions: list[str] = []
        values: list[object] = []
        if provider is not None:
            conditions.append("provider = ?")
            values.append(provider)
        if not include_demo:
            conditions.append("demo = 0")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.connection.execute(
            "SELECT provider, account_id, connected FROM provider_health" + where + " ORDER BY provider, account_id",
            values,
        ).fetchall()
        return [
            {
                "provider": row["provider"],
                "account_id": row["account_id"],
                "connected": bool(row["connected"]),
            }
            for row in rows
        ]

    def health_records(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT * FROM provider_health ORDER BY provider, account_id"
        ).fetchall()
        return [self._public_health(row) for row in rows]

    def health(self, provider: str, account_id: str) -> ProviderHealth | None:
        row = self.connection.execute(
            "SELECT * FROM provider_health WHERE provider = ? AND account_id = ?",
            (provider, account_id),
        ).fetchone()
        if row is None:
            return None
        return ProviderHealth(
            provider=row["provider"],
            account_id=row["account_id"],
            connected=bool(row["connected"]),
            last_sync=row["last_sync"],
            last_error=row["last_error"],
            retry_after=row["retry_after"],
            stale=bool(row["stale"]),
            demo=bool(row["demo"]),
            skipped=row["skipped"],
        )

    def set_health(self, health: ProviderHealth) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO provider_health
                  (provider, account_id, connected, last_sync, last_error,
                   retry_after, stale, demo, skipped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id) DO UPDATE SET
                  connected=excluded.connected,
                  last_sync=excluded.last_sync,
                  last_error=excluded.last_error,
                  retry_after=excluded.retry_after,
                  stale=excluded.stale,
                  demo=excluded.demo,
                  skipped=excluded.skipped
                """,
                self._health_values(health),
            )

    def get_event(self, uid: str) -> dict[str, object] | None:
        row = self.connection.execute("SELECT * FROM events WHERE uid = ?", (uid,)).fetchone()
        return self._public_event(row) if row is not None else None

    def get_calendar(self, key: str) -> Calendar | None:
        for row in self.connection.execute("SELECT * FROM calendars").fetchall():
            if self._calendar_key(row) == key:
                return Calendar(
                    provider=row["provider"],
                    account_id=row["account_id"],
                    account_label=row["account_label"],
                    calendar_id=row["calendar_id"],
                    name=row["name"],
                    color=row["color"],
                    timezone=row["timezone"],
                    writable=bool(row["writable"]),
                    owned=bool(row["owned"]),
                    meeting_providers=tuple(self._json_list(row["meeting_providers"])),
                    sync_enabled=bool(row["sync_enabled"]),
                )
        return None

    def upsert_event(self, event: Event) -> None:
        with self.connection:
            self.connection.execute(
                f"INSERT OR REPLACE INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({', '.join('?' for _ in EVENT_COLUMNS)})",
                self._event_values(event),
            )

    def remove_event(self, uid: str, *, series_id: str = "") -> int:
        with self.connection:
            source = self.connection.execute(
                "SELECT provider, account_id, calendar_id FROM events WHERE uid = ?",
                (uid,),
            ).fetchone()
            if series_id and source is not None:
                cursor = self.connection.execute(
                    """
                    DELETE FROM events
                     WHERE provider = ? AND account_id = ? AND calendar_id = ?
                       AND (uid = ? OR recurrence_id = ? OR provider_event_id = ?)
                    """,
                    (
                        source["provider"], source["account_id"], source["calendar_id"],
                        uid, series_id, series_id,
                    ),
                )
            else:
                cursor = self.connection.execute("DELETE FROM events WHERE uid = ?", (uid,))
        return cursor.rowcount

    def remove_account(self, provider: str, account_id: str) -> int:
        with self.connection:
            self.connection.execute(
                "DELETE FROM events WHERE provider = ? AND account_id = ?",
                (provider, account_id),
            )
            self.connection.execute(
                "DELETE FROM calendars WHERE provider = ? AND account_id = ?",
                (provider, account_id),
            )
            cursor = self.connection.execute(
                "DELETE FROM provider_health WHERE provider = ? AND account_id = ?",
                (provider, account_id),
            )
        return cursor.rowcount

    @staticmethod
    def _event_values(event: Event) -> tuple[object, ...]:
        data = event.to_dict()
        data["all_day"] = int(event.all_day)
        data["organizer_owned"] = int(event.organizer_owned)
        data["has_attendees"] = int(event.has_attendees)
        data["recurrence"] = json.dumps(event.recurrence, separators=(",", ":"))
        return tuple(data[column] for column in EVENT_COLUMNS)

    @staticmethod
    def _calendar_values(calendar: Calendar) -> tuple[object, ...]:
        data = calendar.to_dict()
        data["writable"] = int(calendar.writable)
        data["owned"] = int(calendar.owned)
        data["sync_enabled"] = int(calendar.sync_enabled)
        data["meeting_providers"] = json.dumps(calendar.meeting_providers, separators=(",", ":"))
        return tuple(data[column] for column in CALENDAR_COLUMNS)

    @staticmethod
    def _health_values(health: ProviderHealth) -> tuple[object, ...]:
        return (
            health.provider,
            health.account_id,
            int(health.connected),
            health.last_sync,
            health.last_error,
            health.retry_after,
            int(health.stale),
            int(health.demo),
            health.skipped,
        )

    @staticmethod
    def _public_event(row: sqlite3.Row) -> dict[str, object]:
        result = {
            column: bool(row[column]) if column in ("all_day", "organizer_owned", "has_attendees") else row[column]
            for column in EVENT_COLUMNS
        }
        result["recurrence"] = CalendarStore._json_list(result["recurrence"])
        result["calendar_key"] = CalendarStore._calendar_key(row)
        return result

    @staticmethod
    def _public_calendar(row: sqlite3.Row) -> dict[str, object]:
        return {
            "key": CalendarStore._calendar_key(row),
            "provider": row["provider"],
            "account_id": row["account_id"],
            "account_label": row["account_label"],
            "name": row["name"],
            "color": row["color"],
            "event_count": int(row["event_count"]),
            "timezone": row["timezone"],
            "writable": bool(row["writable"]),
            "owned": bool(row["owned"]),
            "sync_enabled": bool(row["sync_enabled"]),
            "meeting_providers": CalendarStore._json_list(row["meeting_providers"]),
        }

    @staticmethod
    def _json_list(value: object) -> list[str]:
        try:
            parsed = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    @staticmethod
    def _calendar_key(row: sqlite3.Row) -> str:
        identity = "\0".join((row["provider"], row["account_id"], row["calendar_id"]))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_health(row: sqlite3.Row) -> dict[str, object]:
        return {
            "provider": row["provider"],
            "account_id": row["account_id"],
            "connected": bool(row["connected"]),
            "last_sync": row["last_sync"],
            "last_error": row["last_error"],
            "retry_after": row["retry_after"],
            "stale": bool(row["stale"]),
            "demo": bool(row["demo"]),
            "skipped": row["skipped"],
        }
