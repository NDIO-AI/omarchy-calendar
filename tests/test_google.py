# SPDX-License-Identifier: GPL-3.0-or-later
import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from omarchy_calendar.models import Account
from omarchy_calendar.providers.google import GoogleProvider, normalize_google_calendar, normalize_google_event


FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


ACCOUNT = Account("google", "google-subject", "person@example.com")
CALENDAR = {
    "id": "primary@example.com",
    "summary": "Work",
    "backgroundColor": "#7aa2f7",
    "timeZone": "America/Chicago",
}


class FakeHttp:
    def __init__(self):
        self.urls = []

    def get_json(self, url, headers=None):
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.netloc == "openidconnect.googleapis.com":
            return {"sub": "google-subject", "email": "person@example.com"}
        if parsed.path.endswith("/users/me/calendarList"):
            if query.get("pageToken") == ["cal-page-2"]:
                return {"items": [{
                    "id": "personal@example.com", "summary": "Personal",
                    "backgroundColor": "#bb9af7", "timeZone": "America/Chicago",
                    "selected": False, "accessRole": "owner", "dataOwner": "person@example.com"
                }]}
            return load_fixture("google-calendar-list.json")
        if parsed.path.endswith("/events/series-1"):
            return {
                "id": "series-1", "etag": '"series-etag-1"',
                "start": {"dateTime": "2026-08-05T10:00:00-05:00"},
                "end": {"dateTime": "2026-08-05T11:00:00-05:00"},
            }
        if "/events" in parsed.path:
            if query.get("pageToken") == ["event-page-2"]:
                return {"items": []}
            return load_fixture("google-events.json")
        raise AssertionError(f"unexpected URL: {url}")


class GoogleProviderTests(unittest.TestCase):
    def test_calendar_ownership_uses_primary_or_data_owner_not_acl_role(self):
        primary = normalize_google_calendar({"id": "primary", "primary": True, "accessRole": "owner"}, ACCOUNT)
        secondary = normalize_google_calendar({
            "id": "secondary", "accessRole": "owner", "dataOwner": ACCOUNT.label,
        }, ACCOUNT)
        shared = normalize_google_calendar({"id": "shared", "accessRole": "owner"}, ACCOUNT)

        self.assertTrue(primary.owned)
        self.assertTrue(secondary.owned)
        self.assertFalse(shared.owned)

    def test_provider_unselected_calendar_is_not_synced_or_writable_in_flight_deck(self):
        calendar = normalize_google_calendar({
            "id": "secondary", "selected": False, "accessRole": "owner",
            "dataOwner": ACCOUNT.label,
        }, ACCOUNT)

        self.assertTrue(calendar.owned)
        self.assertTrue(calendar.writable)
        self.assertFalse(calendar.sync_enabled)

    def test_event_keeps_meeting_provider_link_and_plain_description(self):
        raw = load_fixture("google-events.json")["items"][0]
        result = normalize_google_event(raw, ACCOUNT, CALENDAR)

        self.assertEqual(result.meeting_url, "https://meet.google.com/abc-defg-hij")
        self.assertEqual(result.provider_url, raw["htmlLink"])
        self.assertEqual(result.description, "Review the selected direction.")
        self.assertEqual(result.uid, "google:google-subject:primary@example.com:event-1")
        self.assertEqual(result.provider_event_id, "event-1")
        self.assertEqual(result.revision, '"google-etag-1"')
        self.assertEqual(result.timezone, "America/Chicago")
        self.assertEqual(result.recurrence_id, "series-1")
        self.assertEqual(result.event_type, "occurrence")
        self.assertTrue(result.organizer_owned)
        self.assertEqual(result.start_day, "2026-08-25")
        self.assertEqual(result.end_day, "2026-08-25")

    def test_all_day_event_uses_calendar_timezone_and_cancelled_is_skipped(self):
        items = load_fixture("google-events.json")["items"]
        all_day = normalize_google_event(items[1], ACCOUNT, CALENDAR)
        cancelled = normalize_google_event(items[2], ACCOUNT, CALENDAR)

        self.assertTrue(all_day.all_day)
        self.assertEqual(all_day.start, "2026-08-25T00:00:00-05:00")
        self.assertEqual(all_day.end, "2026-08-26T00:00:00-05:00")
        self.assertEqual(all_day.start_day, "2026-08-25")
        self.assertEqual(all_day.end_day, "2026-08-26")
        self.assertIsNone(cancelled)

    def test_event_records_attendees_and_draft_time_series_revision(self):
        raw = {
            **load_fixture("google-events.json")["items"][0],
            "attendees": [{"email": "guest@example.com"}],
            "_seriesRevision": '"series-etag-1"',
        }

        result = normalize_google_event(raw, ACCOUNT, CALENDAR)

        self.assertTrue(result.has_attendees)
        self.assertEqual(result.series_revision, '"series-etag-1"')

    def test_series_master_metadata_keeps_draft_time_schedule(self):
        http = FakeHttp()
        _, _, events = GoogleProvider(http).fetch_window(
            "access-token", "2026-08-25T00:00:00Z", "2026-08-27T00:00:00Z"
        )

        occurrence = next(item for item in events if item.recurrence_id)
        self.assertEqual(occurrence.series_start, "2026-08-05T10:00:00-05:00")
        self.assertEqual(occurrence.series_end, "2026-08-05T11:00:00-05:00")

    def test_provider_follows_calendar_and_event_pagination(self):
        http = FakeHttp()
        provider = GoogleProvider(http)

        account, calendars, events = provider.fetch_window(
            "access-token", "2026-08-25T00:00:00Z", "2026-08-27T00:00:00Z"
        )

        self.assertEqual(account, ACCOUNT)
        self.assertEqual(len(calendars), 2)
        self.assertTrue(calendars[0].writable)
        self.assertTrue(calendars[0].owned)
        self.assertEqual(calendars[0].meeting_providers, ("googleMeet",))
        self.assertEqual(calendars[0].timezone, "America/Chicago")
        self.assertEqual(len(events), 2)
        self.assertTrue(calendars[1].owned)
        self.assertFalse(calendars[1].sync_enabled)
        self.assertTrue(all(event.series_revision == '"series-etag-1"' for event in events if event.recurrence_id))
        self.assertTrue(any("pageToken=cal-page-2" in url for url in http.urls))
        self.assertEqual(sum("pageToken=event-page-2" in url for url in http.urls), 1)
        event_urls = [url for url in http.urls if "/events?" in url]
        self.assertTrue(all("singleEvents=true" in url for url in event_urls))
        self.assertTrue(all("showDeleted=false" in url for url in event_urls))


if __name__ == "__main__":
    unittest.main()
