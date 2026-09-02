# SPDX-License-Identifier: GPL-3.0-or-later
import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from omarchy_calendar.models import Account
from omarchy_calendar.providers.microsoft import (
    MicrosoftProvider,
    normalize_microsoft_calendar,
    normalize_microsoft_event,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


ACCOUNT = Account("microsoft", "personal-id", "person@outlook.example")
CALENDAR = {"id": "calendar-primary", "name": "Personal", "color": "lightBlue"}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get_json(self, url, headers=None):
        self.calls.append((url, headers or {}))
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path == "/v1.0/me":
            return {"id": "personal-id", "displayName": "Person", "mail": "person@outlook.example"}
        if parsed.path == "/v1.0/me/calendars":
            if "$skiptoken=calendar-page-2" in url:
                return {"value": [{"id": "calendar-two", "name": "Family", "color": "lightPurple"}]}
            return load_fixture("microsoft-calendars.json")
        if parsed.path.endswith("/calendarView"):
            if "$skiptoken=event-page-2" in url:
                return {"value": []}
            return load_fixture("microsoft-events.json")
        raise AssertionError(f"unexpected URL: {url}")


class MicrosoftProviderTests(unittest.TestCase):
    def test_personal_event_keeps_teams_and_outlook_links(self):
        raw = load_fixture("microsoft-events.json")["value"][0]
        result = normalize_microsoft_event(raw, ACCOUNT, CALENDAR)

        self.assertEqual(result.meeting_url, "https://teams.microsoft.com/l/meetup-join/demo")
        self.assertEqual(result.provider_url, raw["webLink"])
        self.assertEqual(result.provider, "microsoft")
        self.assertEqual(result.description, "Talk through the next milestone.")
        self.assertEqual(result.uid, "microsoft:personal-id:calendar-primary:outlook-event-1")
        self.assertEqual(result.provider_event_id, "outlook-event-1")
        self.assertEqual(result.revision, "outlook-change-1")
        self.assertEqual(result.timezone, "UTC")
        self.assertEqual(result.recurrence_id, "mentor-series")
        self.assertEqual(result.event_type, "occurrence")
        self.assertTrue(result.organizer_owned)

    def test_all_day_is_utc_and_cancelled_is_skipped(self):
        items = load_fixture("microsoft-events.json")["value"]
        all_day = normalize_microsoft_event(items[1], ACCOUNT, CALENDAR)
        cancelled = normalize_microsoft_event(items[2], ACCOUNT, CALENDAR)

        self.assertTrue(all_day.all_day)
        self.assertEqual(all_day.start, "2026-08-25T00:00:00+00:00")
        self.assertIsNone(cancelled)

    def test_event_normalization_accepts_graphs_iana_timezones(self):
        raw = dict(
            load_fixture("microsoft-events.json")["value"][0],
            start={"dateTime": "2026-08-25T15:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-25T15:45:00", "timeZone": "America/Chicago"},
        )

        result = normalize_microsoft_event(raw, ACCOUNT, CALENDAR)

        self.assertEqual(result.start, "2026-08-25T15:00:00-05:00")

    def test_calendar_prefers_the_reported_default_meeting_provider(self):
        calendar = normalize_microsoft_calendar({
            "id": "calendar-primary",
            "name": "Personal",
            "canEdit": True,
            "owner": {"address": ACCOUNT.label},
            "allowedOnlineMeetingProviders": ["unknown", "skypeForConsumer", "teamsForBusiness"],
            "defaultOnlineMeetingProvider": "teamsForBusiness",
        }, ACCOUNT)

        self.assertEqual(calendar.meeting_providers, ("teamsForBusiness", "skypeForConsumer"))

    def test_provider_follows_calendar_and_event_next_links(self):
        http = FakeHttp()
        provider = MicrosoftProvider(http, timezone="America/Chicago")

        account, calendars, events = provider.fetch_window(
            "access-token", "2026-08-25T00:00:00Z", "2026-08-27T00:00:00Z"
        )

        self.assertEqual(account, ACCOUNT)
        self.assertEqual(len(calendars), 2)
        self.assertTrue(calendars[0].writable)
        self.assertTrue(calendars[0].owned)
        self.assertEqual(calendars[0].timezone, "America/Chicago")
        self.assertEqual(calendars[0].meeting_providers, ("teamsForBusiness",))
        self.assertEqual(len(events), 4)
        self.assertTrue(any("$skiptoken=calendar-page-2" in call[0] for call in http.calls))
        self.assertGreaterEqual(sum("$skiptoken=event-page-2" in call[0] for call in http.calls), 2)
        view_calls = [call for call in http.calls if "/calendarView" in call[0]]
        self.assertTrue(all(call[1]["Prefer"] == 'outlook.timezone="America/Chicago"' for call in view_calls))
        self.assertTrue(all("startDateTime=" in call[0] and "endDateTime=" in call[0] for call in view_calls if "$skiptoken" not in call[0]))
        calendar_call = next(call[0] for call in http.calls if "/me/calendars" in call[0])
        self.assertNotIn("timeZone", parse_qs(urlparse(calendar_call).query)["$select"][0])


if __name__ == "__main__":
    unittest.main()
