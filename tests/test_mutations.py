# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from omarchy_calendar.models import Calendar
from omarchy_calendar.mutations import (
    DraftError,
    EventDraft,
    google_event_id,
    google_payload,
    microsoft_payload,
)


CALENDAR_KEY = "a" * 64
GOOGLE_CALENDAR = Calendar(
    provider="google", account_id="google-account", account_label="person@example.com",
    calendar_id="work@example.com", name="Work", color="#7aa2f7",
    timezone="America/Chicago", writable=True, owned=True,
    meeting_providers=("googleMeet",),
)
MICROSOFT_CALENDAR = Calendar(
    provider="microsoft", account_id="microsoft-account", account_label="person@outlook.example",
    calendar_id="work", name="Work", color="#bb9af7", timezone="UTC",
    writable=True, owned=True, meeting_providers=("teamsForBusiness",),
)


def draft_payload(**overrides):
    payload = {
        "request_id": "2a08dcba-61ef-48af-af54-525832d29d3d",
        "calendar_key": CALENDAR_KEY,
        "title": "Calendar design review",
        "day": "2026-09-02",
        "start": "10:00",
        "end": "11:00",
        "all_day": False,
        "location": "Conference Room B",
        "notes": "Review the latest calendar layouts.",
        "recurrence": {"frequency": "none", "weekdays": [], "end": "never"},
        "online_meeting": "none",
        "source_uid": "",
        "scope": "single",
    }
    payload.update(overrides)
    return payload


class EventDraftTests(unittest.TestCase):
    def test_validates_stdin_shape_and_normalizes_text(self):
        draft = EventDraft.from_dict(draft_payload(title="  Design review  "))

        self.assertEqual(draft.title, "Design review")
        self.assertEqual(draft.start, "10:00")
        self.assertEqual(draft.recurrence.frequency, "none")

    def test_rejects_invalid_or_unsafe_drafts(self):
        cases = (
            ({"title": ""}, "title"),
            ({"calendar_key": "raw-calendar-id"}, "calendar"),
            ({"request_id": "retry-me"}, "request"),
            ({"day": "tomorrow"}, "day"),
            ({"start": "25:00"}, "time"),
            ({"start": "11:00", "end": "10:00"}, "after"),
            ({"online_meeting": "javascript:alert(1)"}, "meeting"),
            ({"scope": "following"}, "scope"),
            ({"recurrence": "daily"}, "recurrence"),
            ({"notes": "x" * 9000}, "notes"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(DraftError, message):
                EventDraft.from_dict(draft_payload(**changes))

    def test_recurrence_supports_requested_presets_and_endings(self):
        weekdays = EventDraft.from_dict(draft_payload(recurrence={
            "frequency": "weekdays", "weekdays": [], "end": "count", "count": 8,
        }))
        selected = EventDraft.from_dict(draft_payload(recurrence={
            "frequency": "selected_weekdays", "weekdays": ["MO", "WE", "FR"],
            "end": "date", "until": "2026-12-31",
        }))

        self.assertEqual(weekdays.recurrence.weekdays, ("MO", "TU", "WE", "TH", "FR"))
        self.assertEqual(weekdays.recurrence.count, 8)
        self.assertEqual(selected.recurrence.weekdays, ("MO", "WE", "FR"))
        self.assertEqual(selected.recurrence.until, "2026-12-31")

        with self.assertRaisesRegex(DraftError, "weekday"):
            EventDraft.from_dict(draft_payload(recurrence={
                "frequency": "selected_weekdays", "weekdays": [], "end": "never",
            }))

    def test_recurrence_start_must_match_its_weekday_pattern(self):
        for recurrence in (
            {"frequency": "weekdays", "weekdays": [], "end": "never"},
            {"frequency": "selected_weekdays", "weekdays": ["MO", "FR"], "end": "never"},
        ):
            with self.subTest(recurrence=recurrence), self.assertRaisesRegex(
                DraftError, "start day must match"
            ):
                EventDraft.from_dict(draft_payload(day="2026-09-05", recurrence=recurrence))

    def test_existing_series_can_preserve_its_remote_schedule(self):
        draft = EventDraft.from_dict(draft_payload(recurrence={
            "frequency": "preserve", "weekdays": [], "end": "never",
        }))

        self.assertEqual(draft.recurrence.frequency, "preserve")
        self.assertNotIn("recurrence", google_payload(draft, GOOGLE_CALENDAR)[0])
        self.assertNotIn("recurrence", microsoft_payload(draft, MICROSOFT_CALENDAR))


class ProviderPayloadTests(unittest.TestCase):
    def test_google_timed_payload_uses_idempotent_id_recurrence_and_meet(self):
        draft = EventDraft.from_dict(draft_payload(
            recurrence={"frequency": "selected_weekdays", "weekdays": ["MO", "WE"], "end": "count", "count": 6},
            online_meeting="new",
        ))

        payload, conference = google_payload(draft, GOOGLE_CALENDAR)

        self.assertEqual(payload["id"], google_event_id(draft.request_id))
        self.assertEqual(payload["start"]["dateTime"], "2026-09-02T10:00:00-05:00")
        self.assertEqual(payload["end"]["dateTime"], "2026-09-02T11:00:00-05:00")
        self.assertEqual(payload["recurrence"], ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6"])
        self.assertEqual(payload["conferenceData"]["createRequest"]["requestId"], draft.request_id)
        self.assertTrue(conference)

    def test_google_all_day_payload_uses_exclusive_end_date(self):
        draft = EventDraft.from_dict(draft_payload(all_day=True, start="", end=""))

        payload, conference = google_payload(draft, GOOGLE_CALENDAR)

        self.assertEqual(payload["start"], {"date": "2026-09-02"})
        self.assertEqual(payload["end"], {"date": "2026-09-03"})
        self.assertFalse(conference)

    def test_provider_payloads_preserve_multi_day_and_overnight_ranges(self):
        multi_day = EventDraft.from_dict(draft_payload(
            day="2026-09-02", end_day="2026-09-05", all_day=True, start="", end="",
        ))
        overnight = EventDraft.from_dict(draft_payload(
            day="2026-09-02", end_day="2026-09-03", start="23:30", end="01:00",
        ))

        google_all_day, _ = google_payload(multi_day, GOOGLE_CALENDAR)
        google_overnight, _ = google_payload(overnight, GOOGLE_CALENDAR)
        microsoft_all_day = microsoft_payload(multi_day, MICROSOFT_CALENDAR)
        microsoft_overnight = microsoft_payload(overnight, MICROSOFT_CALENDAR)

        self.assertEqual(google_all_day["end"], {"date": "2026-09-05"})
        self.assertIn("2026-09-03T01:00:00", google_overnight["end"]["dateTime"])
        self.assertEqual(microsoft_all_day["end"]["dateTime"], "2026-09-05T00:00:00")
        self.assertEqual(microsoft_overnight["end"]["dateTime"], "2026-09-03T01:00:00")

    def test_google_recurrence_end_date_uses_the_calendar_timezone(self):
        draft = EventDraft.from_dict(draft_payload(recurrence={
            "frequency": "daily", "weekdays": [], "end": "date", "until": "2026-12-31",
        }))

        payload, _ = google_payload(draft, GOOGLE_CALENDAR)

        self.assertEqual(payload["recurrence"], ["RRULE:FREQ=DAILY;UNTIL=20270101T055959Z"])

    def test_google_all_day_recurrence_end_date_matches_date_only_start(self):
        draft = EventDraft.from_dict(draft_payload(
            all_day=True, start="", end="",
            recurrence={"frequency": "daily", "weekdays": [], "end": "date", "until": "2026-12-31"},
        ))

        payload, _ = google_payload(draft, GOOGLE_CALENDAR)

        self.assertEqual(payload["recurrence"], ["RRULE:FREQ=DAILY;UNTIL=20261231"])

    def test_dst_gap_and_ambiguous_local_times_are_rejected_before_network_use(self):
        cases = (("2026-03-08", "02:30"), ("2026-11-01", "01:30"))
        for provider, calendar, build in (
            ("google", GOOGLE_CALENDAR, lambda draft, cal: google_payload(draft, cal)),
            ("microsoft", Calendar(**{**MICROSOFT_CALENDAR.to_dict(), "timezone": "America/Chicago"}), microsoft_payload),
        ):
            for day, value in cases:
                with self.subTest(provider=provider, day=day, value=value), self.assertRaisesRegex(DraftError, "daylight saving"):
                    build(EventDraft.from_dict(draft_payload(day=day, start=value, end="03:30")), calendar)

    def test_microsoft_payload_uses_transaction_id_recurrence_and_reported_meeting_provider(self):
        draft = EventDraft.from_dict(draft_payload(
            recurrence={"frequency": "monthly", "weekdays": [], "end": "date", "until": "2026-12-31"},
            online_meeting="new",
        ))

        payload = microsoft_payload(draft, MICROSOFT_CALENDAR)

        self.assertEqual(payload["transactionId"], draft.request_id)
        self.assertEqual(payload["start"], {"dateTime": "2026-09-02T10:00:00", "timeZone": "UTC"})
        self.assertEqual(payload["recurrence"]["pattern"]["type"], "absoluteMonthly")
        self.assertEqual(payload["recurrence"]["range"]["type"], "endDate")
        self.assertTrue(payload["isOnlineMeeting"])
        self.assertEqual(payload["onlineMeetingProvider"], "teamsForBusiness")

    def test_microsoft_all_day_payload_uses_the_next_day_as_its_exclusive_end(self):
        payload = microsoft_payload(
            EventDraft.from_dict(draft_payload(all_day=True, start="", end="")),
            MICROSOFT_CALENDAR,
        )

        self.assertEqual(payload["start"], {"dateTime": "2026-09-02T00:00:00", "timeZone": "UTC"})
        self.assertEqual(payload["end"], {"dateTime": "2026-09-03T00:00:00", "timeZone": "UTC"})
        self.assertTrue(payload["isAllDay"])

    def test_copy_preserves_existing_meeting_link_in_notes(self):
        draft = EventDraft.from_dict(draft_payload(
            online_meeting="preserve", notes="N" * 8192,
        ))

        google, _ = google_payload(draft, GOOGLE_CALENDAR, "https://teams.microsoft.com/l/meetup-join/demo")
        microsoft = microsoft_payload(draft, MICROSOFT_CALENDAR, "https://meet.google.com/abc-defg-hij")

        self.assertTrue(google["description"].startswith(
            "Meeting: https://teams.microsoft.com/l/meetup-join/demo"
        ))
        self.assertTrue(microsoft["body"]["content"].startswith(
            "Meeting: https://meet.google.com/abc-defg-hij"
        ))

    def test_copy_removes_the_source_meeting_link_when_it_is_not_preserved(self):
        old_link = "https://meet.google.com/abc-defg-hij"
        for mode in ("new", "none"):
            with self.subTest(mode=mode):
                copied = EventDraft.from_dict(draft_payload(
                    online_meeting=mode,
                    notes=f"Agenda\n\nMeeting: {old_link}\n\nBring notes",
                    location=f"Room B {old_link}",
                ))

                google, _ = google_payload(copied, GOOGLE_CALENDAR, old_link)
                microsoft = microsoft_payload(copied, MICROSOFT_CALENDAR, old_link)

                self.assertNotIn(old_link, google["description"])
                self.assertNotIn(old_link, microsoft["body"]["content"])
                self.assertNotIn(old_link, google["location"])
                self.assertNotIn(old_link, microsoft["location"]["displayName"])
                self.assertNotIn("Meeting:", google["description"])
                self.assertNotIn("Meeting:", microsoft["body"]["content"])

    def test_new_meeting_requires_the_destination_to_report_support(self):
        requested = EventDraft.from_dict(draft_payload(online_meeting="new"))
        no_google_meeting = Calendar(
            **{**GOOGLE_CALENDAR.to_dict(), "meeting_providers": ()}
        )
        no_outlook_meeting = Calendar(
            **{**MICROSOFT_CALENDAR.to_dict(), "meeting_providers": ()}
        )

        with self.assertRaisesRegex(DraftError, "cannot create a meeting"):
            google_payload(requested, no_google_meeting)
        with self.assertRaisesRegex(DraftError, "cannot create a meeting"):
            microsoft_payload(requested, no_outlook_meeting)


if __name__ == "__main__":
    unittest.main()
