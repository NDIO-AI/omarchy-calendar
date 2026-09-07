# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from omarchy_calendar.cache import CalendarStore
from omarchy_calendar.models import Calendar, Event, ProviderHealth


def editable_event() -> Event:
    return Event(
        uid="google:account:calendar:event",
        provider="google",
        account_id="account",
        account_label="person@example.com",
        calendar_id="calendar",
        calendar_name="Work",
        calendar_color="#7aa2f7",
        title="Design review",
        start="2026-09-02T10:00:00-05:00",
        end="2026-09-02T11:00:00-05:00",
        all_day=False,
        status="confirmed",
        location="Room B",
        description="Review the calendar editor.",
        organizer="person@example.com",
        meeting_url="https://meet.google.com/abc-defg-hij",
        provider_url="https://calendar.google.com/calendar/event?eid=event",
        updated="2026-09-02T14:00:00Z",
        provider_event_id="event",
        revision='"etag-1"',
        timezone="America/Chicago",
        recurrence_id="series-1",
        recurrence=("RRULE:FREQ=WEEKLY;BYDAY=WE",),
        event_type="occurrence",
        organizer_owned=True,
        start_day="2026-09-02",
        end_day="2026-09-02",
        series_revision='"series-etag-1"',
        series_start="2026-08-05T10:00:00-05:00",
        series_end="2026-08-05T11:00:00-05:00",
        has_attendees=False,
    )


class WriteModelTests(unittest.TestCase):
    def test_additive_migration_preserves_events_and_adds_write_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calendar.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE events (
                  uid TEXT PRIMARY KEY, provider TEXT NOT NULL, account_id TEXT NOT NULL,
                  account_label TEXT NOT NULL, calendar_id TEXT NOT NULL,
                  calendar_name TEXT NOT NULL, calendar_color TEXT NOT NULL,
                  title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                  all_day INTEGER NOT NULL, status TEXT NOT NULL, location TEXT NOT NULL,
                  description TEXT NOT NULL, organizer TEXT NOT NULL, meeting_url TEXT NOT NULL,
                  provider_url TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE TABLE provider_health (
                  provider TEXT NOT NULL, account_id TEXT NOT NULL, connected INTEGER NOT NULL,
                  last_sync TEXT NOT NULL, last_error TEXT NOT NULL, retry_after TEXT NOT NULL,
                  stale INTEGER NOT NULL, demo INTEGER NOT NULL, skipped INTEGER NOT NULL,
                  PRIMARY KEY (provider, account_id)
                );
                INSERT INTO events VALUES (
                  'google:a:c:e', 'google', 'a', 'a@example.com', 'c', 'Work', '#7aa2f7',
                  'Preserved', '2026-09-02T10:00:00Z', '2026-09-02T11:00:00Z', 0,
                  'confirmed', '', '', '', '', '', '2026-09-02T09:00:00Z'
                );
                """
            )
            connection.commit()
            connection.close()

            with CalendarStore(path) as store:
                migrated = store.get_event("google:a:c:e")
                store.upsert_event(replace(
                    editable_event(),
                    uid="google:a:c:e",
                    account_id="a",
                    account_label="a@example.com",
                    calendar_id="c",
                    provider_event_id="e",
                    has_attendees=False,
                ))
                refreshed = store.get_event("google:a:c:e")

            self.assertEqual(migrated["title"], "Preserved")
            self.assertEqual(migrated["provider_event_id"], "")
            self.assertEqual(migrated["recurrence"], [])
            self.assertFalse(migrated["organizer_owned"])
            self.assertEqual(migrated["start_day"], "")
            self.assertEqual(migrated["series_revision"], "")
            self.assertEqual(migrated["series_start"], "")
            self.assertEqual(migrated["series_end"], "")
            self.assertTrue(migrated["has_attendees"])
            self.assertFalse(refreshed["has_attendees"])

    def test_calendar_catalog_keeps_empty_writable_destinations_and_event_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            with CalendarStore(Path(temporary) / "calendar.db") as store:
                calendar = Calendar(
                    provider="google",
                    account_id="account",
                    account_label="person@example.com",
                    calendar_id="calendar",
                    name="Work",
                    color="#7aa2f7",
                    timezone="America/Chicago",
                    writable=True,
                    owned=True,
                    meeting_providers=("googleMeet",),
                    sync_enabled=False,
                )
                store.replace_window(
                    "google", "account",
                    "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
                    [editable_event()],
                    ProviderHealth.ok("google", "account", "2026-09-02T14:01:00Z"),
                    calendars=[calendar],
                )
                view = store.view("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
                fetched_calendar = store.get_calendar(view["calendars"][0]["key"])

            self.assertEqual(view["events"][0]["revision"], '"etag-1"')
            self.assertEqual(view["events"][0]["recurrence"], ["RRULE:FREQ=WEEKLY;BYDAY=WE"])
            self.assertTrue(view["events"][0]["organizer_owned"])
            self.assertEqual(view["events"][0]["start_day"], "2026-09-02")
            self.assertEqual(view["events"][0]["end_day"], "2026-09-02")
            self.assertEqual(view["events"][0]["series_revision"], '"series-etag-1"')
            self.assertEqual(view["events"][0]["series_start"], "2026-08-05T10:00:00-05:00")
            self.assertEqual(view["events"][0]["series_end"], "2026-08-05T11:00:00-05:00")
            self.assertFalse(view["events"][0]["has_attendees"])
            self.assertEqual(len(view["calendars"]), 1)
            self.assertTrue(view["calendars"][0]["writable"])
            self.assertTrue(view["calendars"][0]["owned"])
            self.assertEqual(view["calendars"][0]["account_id"], "account")
            self.assertEqual(view["calendars"][0]["timezone"], "America/Chicago")
            self.assertEqual(view["calendars"][0]["meeting_providers"], ["googleMeet"])
            self.assertFalse(view["calendars"][0]["sync_enabled"])
            self.assertFalse(fetched_calendar.sync_enabled)

    def test_calendar_migration_marks_existing_catalog_rows_sync_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calendar.db"
            with CalendarStore(path) as store:
                store.replace_window(
                    "google", "account",
                    "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z", [],
                    ProviderHealth.ok("google", "account", "2026-09-02T14:01:00Z"),
                    calendars=[Calendar(
                        "google", "account", "person@example.com", "calendar",
                        "Work", "#7aa2f7", "UTC", True, True,
                    )],
                )
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE calendars RENAME TO calendars_current")
            connection.execute(
                "CREATE TABLE calendars AS SELECT provider, account_id, account_label, calendar_id, name, color, timezone, writable, owned, meeting_providers FROM calendars_current"
            )
            connection.execute("DROP TABLE calendars_current")
            connection.commit()
            connection.close()

            with CalendarStore(path) as store:
                view = store.view("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")

            self.assertTrue(view["calendars"][0]["sync_enabled"])

    def test_calendar_catalog_includes_an_empty_calendar_and_removal_is_account_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            with CalendarStore(Path(temporary) / "calendar.db") as store:
                calendar = Calendar(
                    provider="microsoft", account_id="account", account_label="person@example.com",
                    calendar_id="empty", name="Empty", color="#bb9af7", timezone="UTC",
                    writable=True, owned=True, meeting_providers=("teamsForBusiness",),
                )
                store.replace_window(
                    "microsoft", "account", "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
                    [], ProviderHealth.ok("microsoft", "account", "2026-09-02T14:01:00Z"),
                    calendars=[calendar],
                )
                view = store.view("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
                removed = store.remove_account("microsoft", "account")
                after = store.view("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")

            self.assertEqual(view["calendars"][0]["event_count"], 0)
            self.assertEqual(removed, 1)
            self.assertEqual(after["calendars"], [])


if __name__ == "__main__":
    unittest.main()
