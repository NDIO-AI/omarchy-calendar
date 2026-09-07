# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from omarchy_calendar.cache import CalendarStore
from omarchy_calendar.http import HttpError
from omarchy_calendar.models import Calendar, Event, ProviderHealth
from omarchy_calendar.mutations import (
    DraftError,
    EventDraft,
    MutationConflict,
    MutationService,
    google_event_id,
)
from omarchy_calendar.settings import ProviderSettings


def event(provider="google", account="ga", calendar="gc", event_id="event-1", **changes):
    values = {
        "uid": f"{provider}:{account}:{calendar}:{event_id}",
        "provider": provider,
        "account_id": account,
        "account_label": "person@example.com" if provider == "google" else "person@outlook.example",
        "calendar_id": calendar,
        "calendar_name": "Work",
        "calendar_color": "#7aa2f7",
        "title": "Original",
        "start": "2026-09-02T10:00:00-05:00" if provider == "google" else "2026-09-02T15:00:00+00:00",
        "end": "2026-09-02T11:00:00-05:00" if provider == "google" else "2026-09-02T16:00:00+00:00",
        "all_day": False,
        "status": "confirmed",
        "location": "",
        "description": "",
        "organizer": "person@example.com" if provider == "google" else "person@outlook.example",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "provider_url": "https://calendar.example/event",
        "updated": "2026-09-02T14:00:00Z",
        "provider_event_id": event_id,
        "revision": '"etag-1"' if provider == "google" else "change-1",
        "timezone": "America/Chicago" if provider == "google" else "UTC",
        "recurrence_id": "series-1",
        "event_type": "occurrence",
        "organizer_owned": True,
        "start_day": "2026-09-02",
        "end_day": "2026-09-02",
        "series_revision": '"master-etag"' if provider == "google" else "master-change",
        "series_start": (
            "2026-08-05T10:00:00-05:00" if provider == "google"
            else "2026-08-05T15:00:00+00:00"
        ),
        "series_end": (
            "2026-08-05T11:00:00-05:00" if provider == "google"
            else "2026-08-05T16:00:00+00:00"
        ),
    }
    values.update(changes)
    return Event(**values)


def draft(calendar_key, **changes):
    source_uid = str(changes.get("source_uid") or "")
    microsoft = source_uid.startswith("microsoft:")
    values = {
        "request_id": "2a08dcba-61ef-48af-af54-525832d29d3d",
        "calendar_key": calendar_key,
        "title": "Updated",
        "day": "2026-09-02",
        "start": "10:00",
        "end": "11:00",
        "all_day": False,
        "location": "Room B",
        "notes": "Notes",
        "recurrence": {"frequency": "none", "weekdays": [], "end": "never"},
        "online_meeting": "none",
        "source_uid": "",
        "source_revision": "change-1" if microsoft else '"etag-1"',
        "series_revision": "master-change" if microsoft else '"master-etag"',
        "series_start": (
            "2026-08-05T15:00:00+00:00" if microsoft
            else "2026-08-05T10:00:00-05:00"
        ),
        "series_end": (
            "2026-08-05T16:00:00+00:00" if microsoft
            else "2026-08-05T11:00:00-05:00"
        ),
        "scope": "single",
    }
    values.update(changes)
    return EventDraft.from_dict(values)


class FakeKeyring:
    def __init__(self, tokens):
        self.tokens = tokens

    def get(self, provider, account_id):
        token = self.tokens.get((provider, account_id))
        return dict(token) if token else None

    def put(self, provider, account_id, token):
        self.tokens[(provider, account_id)] = dict(token)


class FakeReadHttp:
    def __init__(self, responses, token_response=None):
        self.responses = list(responses)
        self.token_response = token_response
        self.calls = []
        self.posts = []

    def get_json(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if not self.responses:
            raise AssertionError(f"unexpected read: {url}")
        return self.responses.pop(0)

    def post_token(self, url, form):
        self.posts.append((url, form))
        if self.token_response is None:
            raise AssertionError(f"unexpected token refresh: {url}")
        return dict(self.token_response)


class FakeWriteHttp:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [{}])
        self.error = error
        self.calls = []

    def request_json(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body, headers or {}))
        if self.error:
            raise self.error
        return self.responses.pop(0)


GOOGLE_RAW = {
    "id": "created-google", "etag": '"etag-2"', "status": "confirmed",
    "summary": "Updated", "start": {"dateTime": "2026-09-02T10:00:00-05:00", "timeZone": "America/Chicago"},
    "end": {"dateTime": "2026-09-02T11:00:00-05:00", "timeZone": "America/Chicago"},
    "organizer": {"email": "person@example.com", "self": True},
    "htmlLink": "https://calendar.google.com/calendar/event?eid=created-google",
    "updated": "2026-09-02T15:00:00Z",
}
MICROSOFT_RAW = {
    "id": "created-microsoft", "changeKey": "change-2", "subject": "Updated",
    "isAllDay": False, "isCancelled": False,
    "start": {"dateTime": "2026-09-02T15:00:00", "timeZone": "UTC"},
    "end": {"dateTime": "2026-09-02T16:00:00", "timeZone": "UTC"},
    "bodyPreview": "Notes", "location": {"displayName": "Room B"},
    "organizer": {"emailAddress": {"address": "person@outlook.example"}},
    "webLink": "https://outlook.live.com/calendar/item/created-microsoft",
    "lastModifiedDateTime": "2026-09-02T15:00:00Z", "type": "singleInstance",
}


class MutationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CalendarStore(Path(self.temporary.name) / "calendar.db")
        self.google_calendar = Calendar(
            "google", "ga", "person@example.com", "gc", "Google Work", "#7aa2f7",
            "America/Chicago", True, True, ("googleMeet",),
        )
        self.microsoft_calendar = Calendar(
            "microsoft", "ma", "person@outlook.example", "mc", "Outlook Work", "#bb9af7",
            "America/Chicago", True, True, ("teamsForBusiness",),
        )
        window = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        self.source = event()
        self.store.replace_window(
            "google", "ga", *window, [self.source], ProviderHealth.ok("google", "ga", window[0]),
            calendars=[self.google_calendar],
        )
        self.store.replace_window(
            "microsoft", "ma", *window, [], ProviderHealth.ok("microsoft", "ma", window[0]),
            calendars=[self.microsoft_calendar],
        )
        catalog = self.store.view(*window)["calendars"]
        self.keys = {item["provider"]: item["key"] for item in catalog}
        self.tokens = {
            ("google", "ga"): {
                "access_token": "google-edit", "expires_at": 9999999999,
                "access_mode": "edit",
                "scope": "https://www.googleapis.com/auth/calendar.events.owned",
            },
            ("microsoft", "ma"): {
                "access_token": "microsoft-edit", "expires_at": 9999999999,
                "access_mode": "edit", "scope": "Calendars.ReadWrite",
            },
        }

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def service(self, read_responses, write_responses=None, error=None):
        read = FakeReadHttp(read_responses)
        write = FakeWriteHttp(write_responses, error)
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )
        return service, read, write

    def test_create_requires_edit_permission_and_owned_writable_calendar(self):
        service, read, write = self.service([GOOGLE_RAW], [GOOGLE_RAW])

        result = service.create(draft(self.keys["google"]))

        self.assertEqual(result["event"]["provider_event_id"], "created-google")
        self.assertIn("/calendars/gc/events", write.calls[0][1])
        self.assertEqual(read.calls[0][1]["Authorization"], "Bearer google-edit")
        self.assertIsNotNone(self.store.get_event(result["event"]["uid"]))

        self.tokens[("google", "ga")]["scope"] = (
            "https://www.googleapis.com/auth/calendar.events.readonly"
        )
        blocked, _, blocked_write = self.service([])
        with self.assertRaisesRegex(PermissionError, "Read and edit"):
            blocked.create(draft(self.keys["google"]))
        self.assertEqual(blocked_write.calls, [])

    def test_create_rejects_preserve_recurrence_before_network_use(self):
        service, read, write = self.service([])

        with self.assertRaisesRegex(DraftError, "new event cannot preserve"):
            service.create(draft(
                self.keys["google"],
                recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
            ))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_google_and_outlook_reject_misaligned_recurrence_before_network_use(self):
        for provider in ("google", "microsoft"):
            with self.subTest(provider=provider):
                service, read, write = self.service([])
                with self.assertRaisesRegex(DraftError, "start day must match"):
                    service.create(EventDraft.from_dict({
                        "request_id": "2a08dcba-61ef-48af-af54-525832d29d3d",
                        "calendar_key": self.keys[provider],
                        "title": "Weekend mismatch",
                        "day": "2026-09-05",
                        "start": "10:00",
                        "end": "11:00",
                        "all_day": False,
                        "recurrence": {
                            "frequency": "weekdays", "weekdays": [], "end": "never",
                        },
                    }))
                self.assertEqual(read.calls, [])
                self.assertEqual(write.calls, [])

    def test_google_create_waits_for_generated_meeting_link(self):
        pending = dict(
            GOOGLE_RAW,
            conferenceData={"createRequest": {"status": {"statusCode": "pending"}}},
        )
        ready = dict(
            pending,
            hangoutLink="https://meet.google.com/new-room",
            conferenceData={"createRequest": {"status": {"statusCode": "success"}}},
        )
        read = FakeReadHttp([pending, ready])
        write = FakeWriteHttp([GOOGLE_RAW])
        sleeps = []
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
            sleep=sleeps.append,
        )

        result = service.create(draft(
            self.keys["google"], online_meeting="new",
        ))

        self.assertEqual(result["event"]["meeting_url"], "https://meet.google.com/new-room")
        self.assertNotIn("notice", result)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(read.calls), 2)

    def test_create_reports_provider_meeting_still_pending(self):
        cases = (
            (
                "google",
                dict(
                    GOOGLE_RAW,
                    conferenceData={"createRequest": {"status": {"statusCode": "pending"}}},
                ),
                "Google Meet is still being prepared",
            ),
            (
                "microsoft",
                dict(MICROSOFT_RAW, isOnlineMeeting=True, onlineMeeting=None),
                "Outlook is still preparing the meeting link",
            ),
        )
        for provider, pending, notice in cases:
            with self.subTest(provider=provider):
                read = FakeReadHttp([pending] * 4)
                write = FakeWriteHttp([pending])
                sleeps = []
                service = MutationService(
                    self.store,
                    keyring=FakeKeyring(self.tokens),
                    read_http=read,
                    write_http=write,
                    timezone="America/Chicago",
                    sleep=sleeps.append,
                )

                result = service.create(draft(
                    self.keys[provider],
                    request_id=(
                        "3a08dcba-61ef-48af-af54-525832d29d3d"
                        if provider == "google" else "4a08dcba-61ef-48af-af54-525832d29d3d"
                    ),
                    online_meeting="new",
                ))

                self.assertIn(notice, result["notice"])
                self.assertEqual(result["event"]["meeting_url"], "")
                self.assertEqual(sleeps, [0.5, 0.5, 0.5])
                self.assertEqual(len(read.calls), 4)

    def test_unrelated_link_in_notes_does_not_complete_google_meet_creation(self):
        pending = dict(
            GOOGLE_RAW,
            description="Reference: https://zoom.us/j/123456789",
            conferenceData={"createRequest": {"status": {"statusCode": "pending"}}},
        )
        read = FakeReadHttp([pending] * 4)
        sleeps = []
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=FakeWriteHttp([pending]),
            timezone="America/Chicago",
            sleep=sleeps.append,
        )

        result = service.create(draft(
            self.keys["google"], online_meeting="new",
        ))

        self.assertIn("Google Meet is still being prepared", result["notice"])
        self.assertEqual(len(read.calls), 4)

    def test_google_update_waits_for_generated_meeting_link(self):
        source = event(meeting_url="")
        self.store.upsert_event(source)
        pending = dict(
            GOOGLE_RAW,
            id="event-1",
            etag='"etag-2"',
            conferenceData={"createRequest": {"status": {"statusCode": "pending"}}},
        )
        ready = dict(pending, hangoutLink="https://meet.google.com/new-room")
        read = FakeReadHttp([pending, ready])
        sleeps = []
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=FakeWriteHttp([pending]),
            timezone="America/Chicago",
            sleep=sleeps.append,
        )

        result = service.update(draft(
            self.keys["google"], source_uid=source.uid, online_meeting="new",
        ))

        self.assertEqual(result["event"]["meeting_url"], "https://meet.google.com/new-room")
        self.assertNotIn("notice", result)
        self.assertEqual(sleeps, [0.5])

    def test_copy_keeps_original_when_requested_meeting_is_not_ready(self):
        pending = dict(MICROSOFT_RAW, isOnlineMeeting=True, onlineMeeting=None)
        read = FakeReadHttp([pending] * 4)
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=FakeWriteHttp([pending]),
            timezone="America/Chicago",
            sleep=lambda _: None,
        )

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid,
            online_meeting="new",
        ))

        self.assertFalse(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertIn("meeting link", result["delete_original_reason"])
        self.assertIn("original", result["delete_original_reason"].lower())

    def test_created_series_master_is_series_only_for_update_and_delete(self):
        cases = (
            (
                "google",
                dict(
                    GOOGLE_RAW,
                    id="google-series",
                    recurrence=["RRULE:FREQ=WEEKLY"],
                    etag='"google-series-etag"',
                ),
            ),
            (
                "microsoft",
                dict(
                    MICROSOFT_RAW,
                    id="microsoft-series",
                    type="seriesMaster",
                    recurrence={
                        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                        "range": {"type": "noEnd", "startDate": "2026-09-02"},
                    },
                    changeKey="microsoft-series-change",
                ),
            ),
        )
        for provider, raw in cases:
            with self.subTest(provider=provider):
                updated = dict(raw)
                updated["etag" if provider == "google" else "changeKey"] = (
                    '"google-series-updated"' if provider == "google" else "microsoft-series-updated"
                )
                service, read, write = self.service([raw, raw, updated], [raw, updated, {}])
                created = service.create(draft(
                    self.keys[provider],
                    recurrence={"frequency": "weekly", "weekdays": ["WE"], "end": "never"},
                ))["event"]
                single = draft(
                    self.keys[provider],
                    source_uid=created["uid"],
                    source_revision=created["revision"],
                    series_revision=created["series_revision"],
                    series_start=created["series_start"],
                    series_end=created["series_end"],
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                    scope="single",
                )

                with self.assertRaisesRegex(DraftError, "Entire series"):
                    service.update(single)
                with self.assertRaisesRegex(DraftError, "Entire series"):
                    service.delete_event(
                        created["uid"],
                        scope="single",
                        expected_revision=created["revision"],
                        series_revision=created["series_revision"],
                    )

                series = draft(
                    self.keys[provider],
                    source_uid=created["uid"],
                    source_revision=created["revision"],
                    series_revision=created["series_revision"],
                    series_start=created["series_start"],
                    series_end=created["series_end"],
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                    scope="series",
                )
                edited = service.update(series)["event"]
                service.delete_event(
                    edited["uid"],
                    scope="series",
                    expected_revision=edited["revision"],
                    series_revision=edited["series_revision"],
                )

                self.assertEqual([call[0] for call in write.calls], ["POST", "PATCH", "DELETE"])
                self.assertEqual(len(read.calls), 3)
                self.assertTrue(write.calls[1][1].endswith("/" + created["provider_event_id"]))
                self.assertTrue(write.calls[2][1].endswith("/" + created["provider_event_id"]))
                self.assertIsNone(self.store.get_event(edited["uid"]))

    def test_access_mode_metadata_without_exact_outlook_scope_cannot_write(self):
        self.tokens[("microsoft", "ma")]["scope"] = "Calendars.Read"
        service, read, write = self.service([])

        with self.assertRaisesRegex(PermissionError, "Read and edit"):
            service.create(draft(self.keys["microsoft"]))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_create_rejects_a_shared_writable_calendar_before_network_use(self):
        shared = Calendar(
            "google", "ga", "person@example.com", "shared", "Shared", "#7aa2f7",
            "America/Chicago", True, False, (),
        )
        window = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        self.store.replace_window(
            "google", "ga", *window, [self.source],
            ProviderHealth.ok("google", "ga", window[0]),
            calendars=[self.google_calendar, shared],
        )
        shared_key = next(
            item["key"] for item in self.store.view(*window)["calendars"]
            if item["name"] == "Shared"
        )
        service, read, write = self.service([])

        with self.assertRaisesRegex(PermissionError, "owned and writable"):
            service.create(draft(shared_key))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_draft_times_are_written_in_the_local_interface_timezone(self):
        tokyo = Calendar(
            "google", "ga", "person@example.com", "tokyo", "Tokyo", "#7aa2f7",
            "Asia/Tokyo", True, True, (),
        )
        window = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        self.store.replace_window(
            "google", "ga", *window, [self.source],
            ProviderHealth.ok("google", "ga", window[0]), calendars=[tokyo],
        )
        tokyo_key = self.store.view(*window)["calendars"][0]["key"]
        read = FakeReadHttp([GOOGLE_RAW])
        write = FakeWriteHttp([GOOGLE_RAW])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )

        service.create(draft(tokyo_key))

        self.assertEqual(write.calls[0][2]["start"], {
            "dateTime": "2026-09-02T10:00:00-05:00",
            "timeZone": "America/Chicago",
        })

    def test_google_update_uses_etag_and_refreshes_cache_on_conflict(self):
        current = dict(GOOGLE_RAW, id="event-1", summary="Changed remotely", etag='"etag-remote"')
        service, _, write = self.service([current], error=HttpError(412, "conflict"))
        edit = draft(self.keys["google"], source_uid=self.source.uid)

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(edit)

        self.assertEqual(write.calls[0][3]["If-Match"], '"etag-1"')
        self.assertEqual(self.store.get_event(self.source.uid)["title"], "Changed remotely")

    def test_update_rejects_a_draft_older_than_the_cached_event_before_network_use(self):
        service, read, write = self.service([])

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(draft(
                self.keys["google"], source_uid=self.source.uid,
                source_revision='"older-etag"',
            ))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_occurrence_update_rejects_a_series_schedule_change(self):
        service, read, write = self.service([])
        edit = draft(
            self.keys["google"],
            source_uid=self.source.uid,
            scope="single",
            recurrence={"frequency": "weekly", "end": "never"},
        )

        with self.assertRaisesRegex(DraftError, "Entire series"):
            service.update(edit)

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_google_series_update_uses_draft_time_revision_and_replaces_stale_occurrences(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        updated = dict(
            master,
            summary="Updated series",
            etag='"updated-etag"',
            recurrence=[],
            start={"dateTime": "2026-09-02T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-09-02T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        service, read, write = self.service([master], [updated])

        result = service.update(draft(
            self.keys["google"], source_uid=self.source.uid, scope="series",
        ))

        self.assertEqual(write.calls[0][3]["If-Match"], '"master-etag"')
        self.assertEqual(write.calls[0][2]["start"]["dateTime"], "2026-09-02T10:00:00-05:00")
        self.assertEqual(write.calls[0][2]["end"]["dateTime"], "2026-09-02T11:00:00-05:00")
        self.assertEqual(write.calls[0][2]["recurrence"], [])
        self.assertTrue(read.calls[0][0].endswith("/events/series-1"))
        self.assertEqual(result["event"]["provider_event_id"], "series-1")
        self.assertEqual(result["event"]["title"], "Updated series")
        self.assertEqual(result["event"]["start"], "2026-09-02T10:00:00-05:00")
        self.assertIsNone(self.store.get_event(self.source.uid))
        self.assertIsNotNone(self.store.get_event(result["event"]["uid"]))
        self.assertEqual(
            [item["uid"] for item in self.store.view(
                "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
            )["events"]],
            [result["event"]["uid"]],
        )

    def test_series_update_keeps_the_edited_occurrence_visible_until_refresh(self):
        sibling = event(
            event_id="event-2",
            start="2026-09-03T10:00:00-05:00",
            end="2026-09-03T11:00:00-05:00",
            start_day="2026-09-03",
            end_day="2026-09-03",
        )
        self.store.upsert_event(sibling)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        updated = dict(
            master,
            summary="Moved series",
            etag='"updated-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TH"],
            start={"dateTime": "2026-08-06T10:30:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-06T11:30:00-05:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master], [updated])

        result = service.update(draft(
            self.keys["google"],
            source_uid=self.source.uid,
            scope="series",
            title="Moved series",
            day="2026-09-03",
            start="10:30",
            end="11:30",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        visible = self.store.view(
            "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
        )["events"]
        self.assertEqual([item["uid"] for item in visible], ["google:ga:gc:series-1"])
        self.assertEqual(result["event"]["uid"], "google:ga:gc:series-1")
        self.assertEqual(write.calls[0][2]["recurrence"], ["RRULE:FREQ=WEEKLY;BYDAY=TH"])
        self.assertEqual(result["event"]["title"], "Moved series")
        self.assertEqual(result["event"]["start"], "2026-09-03T10:30:00-05:00")
        self.assertEqual(result["event"]["end"], "2026-09-03T11:30:00-05:00")
        self.assertEqual(result["event"]["start_day"], "2026-09-03")
        self.assertEqual(result["event"]["end_day"], "2026-09-03")
        self.assertEqual(result["event"]["provider_event_id"], "series-1")
        self.assertEqual(result["event"]["recurrence_id"], "")
        self.assertEqual(result["event"]["event_type"], "series")
        self.assertEqual(result["event"]["series_revision"], '"updated-etag"')
        self.assertEqual(result["event"]["revision"], '"updated-etag"')
        self.assertIsNone(self.store.get_event(self.source.uid))
        self.assertIsNone(self.store.get_event(sibling.uid))

    def test_time_only_series_edit_preserves_an_unsupported_rule_unchanged(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=YEARLY;BYMONTH=9"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        updated = dict(
            master,
            etag='"updated-etag"',
            start={"dateTime": "2026-08-05T10:30:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:30:00-05:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master], [updated])

        service.update(draft(
            self.keys["google"], source_uid=self.source.uid, scope="series",
            start="10:30", end="11:30",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertNotIn("recurrence", write.calls[0][2])

    def test_no_op_entire_series_edit_does_not_overwrite_master_with_exception_fields(self):
        exception = event(
            title="Exception title",
            start="2026-09-02T12:00:00-05:00",
            end="2026-09-02T14:00:00-05:00",
            location="Exception room",
            description="Exception notes",
            meeting_url="https://meet.google.com/exception-room",
        )
        self.store.upsert_event(exception)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            summary="Master title",
            location="Master room",
            description="Master notes",
            hangoutLink="https://meet.google.com/master-room",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master], [dict(master, etag='"updated-etag"')])

        service.update(draft(
            self.keys["google"], source_uid=exception.uid, scope="series",
            title="Exception title", day="2026-09-02", start="12:00", end="14:00",
            location="Exception room", notes="Exception notes", online_meeting="preserve",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2], {})

    def test_outlook_no_op_entire_series_edit_does_not_overwrite_master_with_exception_fields(self):
        exception = event(
            "microsoft", "ma", "mc", "occurrence-1",
            title="Exception title",
            start="2026-09-02T17:00:00+00:00",
            end="2026-09-02T19:00:00+00:00",
            location="Exception room",
            description="Exception notes",
            meeting_url="https://teams.microsoft.com/l/meetup-join/exception",
            revision="change-1",
        )
        self.store.upsert_event(exception)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            subject="Master title",
            body={"contentType": "text", "content": "Master notes"},
            bodyPreview="Master notes",
            location={"displayName": "Master room"},
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        service, _, write = self.service([master], [dict(master, changeKey="updated-change")])

        service.update(draft(
            self.keys["microsoft"], source_uid=exception.uid, scope="series",
            title="Exception title", day="2026-09-02", start="12:00", end="14:00",
            location="Exception room", notes="Exception notes", online_meeting="preserve",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2], {})

    def test_monthly_series_day_move_updates_outlook_day_of_month(self):
        source = event(
            "microsoft", "ma", "mc", "occurrence-1", revision="change-1",
            start="2026-09-05T10:00:00-05:00", end="2026-09-05T11:00:00-05:00",
            start_day="2026-09-05", end_day="2026-09-05",
        )
        self.store.upsert_event(source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 5},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )
        updated = dict(master, changeKey="updated-change")
        service, _, write = self.service([master], [updated])

        service.update(draft(
            self.keys["microsoft"], source_uid=source.uid, scope="series",
            day="2026-09-06", end_day="2026-09-06",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2]["start"]["dateTime"], "2026-08-06T10:00:00")
        self.assertEqual(write.calls[0][2]["recurrence"]["pattern"]["dayOfMonth"], 6)

    def test_series_update_result_rejects_immediate_occurrence_update_and_delete(self):
        cases = (
            (
                "google",
                self.source,
                dict(
                    GOOGLE_RAW,
                    id="series-1",
                    etag='"master-etag"',
                    recurrence=["RRULE:FREQ=WEEKLY"],
                    start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
                    end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
                ),
                '"updated-etag"',
            ),
            (
                "microsoft",
                event("microsoft", "ma", "mc", "occurrence-1", revision="change-1"),
                dict(
                    MICROSOFT_RAW,
                    id="series-1",
                    changeKey="master-change",
                    type="seriesMaster",
                    recurrence={
                        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                        "range": {"type": "noEnd", "startDate": "2026-08-05"},
                    },
                    start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
                    end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
                ),
                "updated-change",
            ),
        )
        for provider, source, master, updated_revision in cases:
            with self.subTest(provider=provider):
                self.store.upsert_event(source)
                updated = dict(master)
                updated["etag" if provider == "google" else "changeKey"] = updated_revision
                service, read, write = self.service([master], [updated])
                result = service.update(draft(
                    self.keys[provider],
                    source_uid=source.uid,
                    scope="series",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                ))["event"]
                occurrence = draft(
                    self.keys[provider],
                    source_uid=result["uid"],
                    source_revision=result["revision"],
                    series_revision=result["series_revision"],
                    series_start=result["series_start"],
                    series_end=result["series_end"],
                    scope="single",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                )

                with self.assertRaisesRegex(DraftError, "Entire series"):
                    service.update(occurrence)
                with self.assertRaisesRegex(DraftError, "Entire series"):
                    service.delete_event(
                        result["uid"],
                        scope="single",
                        expected_revision=result["revision"],
                        series_revision=result["series_revision"],
                    )

                self.assertEqual(len(read.calls), 1)
                self.assertEqual([call[0] for call in write.calls], ["PATCH"])

    def test_all_day_series_move_uses_provider_dates_across_timezones(self):
        source = event(
            start="2026-09-02T00:00:00-05:00",
            end="2026-09-03T00:00:00-05:00",
            all_day=True,
            start_day="2026-09-02",
            end_day="2026-09-03",
            series_start="2026-08-05T00:00:00+09:00",
            series_end="2026-08-06T00:00:00+09:00",
        )
        self.store.upsert_event(source)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY"],
            start={"date": "2026-08-05", "timeZone": "Asia/Tokyo"},
            end={"date": "2026-08-06", "timeZone": "Asia/Tokyo"},
        )
        updated = dict(master, etag='"updated-etag"', start={"date": "2026-08-06"}, end={"date": "2026-08-07"})
        service, _, write = self.service([master], [updated])

        result = service.update(draft(
            self.keys["google"],
            source_uid=source.uid,
            scope="series",
            day="2026-09-03",
            end_day="2026-09-04",
            start="",
            end="",
            all_day=True,
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
            series_start=source.series_start,
            series_end=source.series_end,
        ))

        self.assertEqual(write.calls[0][2]["start"], {"date": "2026-08-06"})
        self.assertEqual(write.calls[0][2]["end"], {"date": "2026-08-07"})
        self.assertEqual(result["event"]["start_day"], "2026-09-03")
        self.assertEqual(result["event"]["end_day"], "2026-09-04")

    def test_title_only_foreign_timezone_updates_do_not_rewrite_time(self):
        google_source = event(
            recurrence_id="",
            event_type="single",
            start="2026-09-02T10:00:00+09:00",
            end="2026-09-02T11:00:00+09:00",
            start_day="2026-09-02",
            end_day="2026-09-02",
            location="Room B",
            description="Notes",
            meeting_url="",
        )
        self.store.upsert_event(google_source)
        google_current = dict(
            GOOGLE_RAW,
            id="event-1",
            summary="Renamed",
            location="Room B",
            description="Notes",
            start={"dateTime": "2026-09-02T10:00:00+09:00", "timeZone": "Asia/Tokyo"},
            end={"dateTime": "2026-09-02T11:00:00+09:00", "timeZone": "Asia/Tokyo"},
        )
        service, _, write = self.service([google_current], [google_current])

        service.update(draft(
            self.keys["google"],
            source_uid=google_source.uid,
            title="Renamed",
            day="2026-09-01",
            end_day="2026-09-01",
            start="20:00",
            end="21:00",
        ))

        self.assertEqual(write.calls[0][2], {"summary": "Renamed"})

    def test_title_only_foreign_timezone_series_update_does_not_rewrite_time(self):
        source = event(
            "microsoft",
            "ma",
            "mc",
            "occurrence-1",
            start="2026-09-02T10:00:00+09:00",
            end="2026-09-02T11:00:00+09:00",
            start_day="2026-09-02",
            end_day="2026-09-02",
            location="Room B",
            description="Notes",
            meeting_url="",
        )
        self.store.upsert_event(source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            location={"displayName": "Room B"},
            body={"contentType": "text", "content": "Notes"},
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        service, _, write = self.service(
            [master], [dict(master, subject="Renamed", changeKey="updated-change")],
        )

        service.update(draft(
            self.keys["microsoft"],
            source_uid=source.uid,
            title="Renamed",
            day="2026-09-01",
            end_day="2026-09-01",
            start="20:00",
            end="21:00",
            scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2], {"subject": "Renamed"})

    def test_google_series_update_rejects_a_changed_master_before_writing(self):
        changed = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"changed-master"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([changed])

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(draft(
                self.keys["google"], source_uid=self.source.uid, scope="series",
            ))

        self.assertEqual(write.calls, [])
        self.assertIsNotNone(self.store.get_event(self.source.uid))
        self.assertEqual(
            self.store.get_event("google:ga:gc:series-1")["revision"],
            '"changed-master"',
        )

    def test_series_update_rejects_a_draft_older_than_the_cached_master(self):
        self.store.upsert_event(event(series_revision='"newer-master"'))
        service, read, write = self.service([])

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(draft(
                self.keys["google"], source_uid=self.source.uid, scope="series",
            ))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_outlook_series_move_applies_the_occurrence_delta_to_the_master(self):
        outlook_source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        updated = dict(
            master,
            changeKey="updated-change",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["thursday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-06"},
            },
        )
        service, _, write = self.service([master], [updated])

        service.update(draft(
            self.keys["microsoft"],
            source_uid=outlook_source.uid,
            scope="series",
            day="2026-09-03",
            start="10:30",
            end="11:30",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2]["start"], {
            "dateTime": "2026-08-06T10:30:00", "timeZone": "America/Chicago",
        })
        self.assertEqual(write.calls[0][2]["end"], {
            "dateTime": "2026-08-06T11:30:00", "timeZone": "America/Chicago",
        })
        self.assertEqual(
            write.calls[0][2]["recurrence"]["pattern"]["daysOfWeek"],
            ["thursday"],
        )
        self.assertEqual(write.calls[0][2]["recurrence"]["range"]["startDate"], "2026-08-06")
        preview = self.store.get_event("microsoft:ma:mc:series-1")
        self.assertIsNotNone(preview)
        self.assertEqual(preview["start"], "2026-09-03T10:30:00-05:00")
        self.assertEqual(preview["provider_event_id"], "series-1")
        self.assertEqual(preview["recurrence_id"], "")
        self.assertEqual(preview["event_type"], "series")
        self.assertEqual(preview["series_revision"], "updated-change")
        self.assertIsNone(self.store.get_event(outlook_source.uid))

    def test_outlook_series_update_rejects_a_changed_master_before_writing(self):
        outlook_source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        changed = dict(
            MICROSOFT_RAW,
            id="series-1", changeKey="changed-master", type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        service, _, write = self.service([changed])

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(draft(
                self.keys["microsoft"], source_uid=outlook_source.uid, scope="series",
            ))

        self.assertEqual(write.calls, [])
        self.assertEqual(
            self.store.get_event("microsoft:ma:mc:series-1")["revision"],
            "changed-master",
        )

    def test_google_series_patch_conflict_caches_the_latest_master(self):
        current = dict(
            GOOGLE_RAW, id="series-1", etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        latest = dict(current, etag='"latest-master"', summary="Changed elsewhere")
        service, read, write = self.service(
            [current, latest], error=HttpError(412, "precondition failed"),
        )

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(draft(
                self.keys["google"], source_uid=self.source.uid, scope="series",
            ))

        self.assertEqual(len(read.calls), 2)
        self.assertEqual(write.calls[0][0], "PATCH")
        cached = self.store.get_event("google:ga:gc:series-1")
        self.assertEqual(cached["revision"], '"latest-master"')
        self.assertEqual(cached["title"], "Changed elsewhere")

    def test_entire_series_update_revalidates_master_attendees_and_ownership(self):
        cases = (
            ("google", "attendees", "attendees"),
            ("google", "ownership", "organize"),
            ("microsoft", "attendees", "attendees"),
            ("microsoft", "ownership", "organize"),
        )
        for provider, restriction, message in cases:
            with self.subTest(provider=provider, restriction=restriction):
                if provider == "google":
                    source = self.source
                    master = dict(
                        GOOGLE_RAW, id="series-1", etag='"master-etag"',
                        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                    )
                else:
                    source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
                    self.store.upsert_event(source)
                    master = dict(
                        MICROSOFT_RAW, id="series-1", changeKey="master-change", type="seriesMaster",
                        recurrence={
                            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                            "range": {"type": "noEnd", "startDate": "2026-09-02"},
                        },
                    )
                if restriction == "attendees":
                    master["attendees"] = (
                        [{"email": "guest@example.com"}] if provider == "google" else [{
                            "emailAddress": {"name": "Guest", "address": "guest@example.com"},
                            "status": {"response": "none", "time": "0001-01-01T00:00:00Z"},
                            "type": "required",
                        }]
                    )
                elif provider == "google":
                    master["organizer"] = {"email": "other@example.com", "self": False}
                else:
                    master["organizer"] = {"emailAddress": {"address": "other@example.com"}}
                    master["isOrganizer"] = False
                service, _, write = self.service([master])

                with self.assertRaisesRegex(PermissionError, message):
                    service.update(draft(
                        self.keys[provider], source_uid=source.uid, scope="series",
                        recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                    ))

                self.assertEqual(write.calls, [])

    def test_series_move_uses_the_draft_time_master_schedule_not_a_new_remote_start(self):
        outlook_source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        shifted_remote_master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-07T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-07T16:00:00", "timeZone": "UTC"},
        )
        service, _, write = self.service([
            shifted_remote_master,
        ], [{**shifted_remote_master, "changeKey": "updated-change"}])

        service.update(draft(
            self.keys["microsoft"], source_uid=outlook_source.uid, scope="series",
            day="2026-09-03", start="10:30", end="11:30",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2]["start"]["dateTime"], "2026-08-06T10:30:00")

    def test_outlook_update_compares_change_key_before_writing(self):
        outlook_source = event("microsoft", "ma", "mc", "event-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        remote = dict(MICROSOFT_RAW, id="event-1", changeKey="change-remote")
        service, _, write = self.service([remote])
        edit = draft(self.keys["microsoft"], source_uid=outlook_source.uid)

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.update(edit)

        self.assertEqual(write.calls, [])
        self.assertEqual(self.store.get_event(outlook_source.uid)["revision"], "change-remote")

    def test_outlook_update_preserves_existing_meeting_body(self):
        outlook_source = event(
            "microsoft", "ma", "mc", "event-1", revision="change-1",
            description="Notes", meeting_url="https://teams.example/join",
        )
        self.store.upsert_event(outlook_source)
        current = dict(
            MICROSOFT_RAW,
            id="event-1",
            changeKey="change-1",
            isOnlineMeeting=True,
            onlineMeeting={"joinUrl": "https://teams.example/join"},
            body={"contentType": "html", "content": "<p>Notes</p><div>meeting blob</div>"},
        )
        updated = dict(current, changeKey="change-2", subject="Renamed")
        service, _, write = self.service([current, updated], [updated])

        service.update(draft(
            self.keys["microsoft"], source_uid=outlook_source.uid,
            title="Renamed", notes="Notes", online_meeting="preserve",
        ))

        self.assertNotIn("body", write.calls[0][2])

    def test_outlook_meeting_rejects_note_or_meeting_removal_before_writing(self):
        outlook_source = event(
            "microsoft", "ma", "mc", "event-1", revision="change-1",
            description="Notes", meeting_url="https://teams.example/join",
        )
        self.store.upsert_event(outlook_source)
        current = dict(
            MICROSOFT_RAW,
            id="event-1",
            changeKey="change-1",
            isOnlineMeeting=True,
            onlineMeeting={"joinUrl": "https://teams.example/join"},
        )

        for changes, message in (
            ({"notes": "Changed", "online_meeting": "preserve"}, "notes"),
            ({"notes": "Notes", "online_meeting": "none"}, "cannot be removed"),
        ):
            with self.subTest(changes=changes):
                service, _, write = self.service([current])
                with self.assertRaisesRegex(DraftError, message):
                    service.update(draft(
                        self.keys["microsoft"], source_uid=outlook_source.uid,
                        **changes,
                    ))
                self.assertEqual(write.calls, [])

    def test_google_update_can_remove_an_existing_meeting(self):
        current = dict(GOOGLE_RAW, id="event-1", etag='"etag-2"')
        service, _, write = self.service([current], [current])

        service.update(draft(
            self.keys["google"], source_uid=self.source.uid,
            online_meeting="none",
        ))

        self.assertIsNone(write.calls[0][2]["conferenceData"])
        self.assertTrue(write.calls[0][1].endswith("?conferenceDataVersion=1"))

    def test_update_removes_a_fallback_meeting_link_from_notes_and_location(self):
        link = "https://meet.google.com/abc-defg-hij"
        source = event(
            description=f"Agenda\nMeeting: {link}",
            location=f"Room B {link}",
            meeting_url=link,
        )
        self.store.upsert_event(source)
        current = dict(
            GOOGLE_RAW,
            id="event-1",
            etag='"etag-2"',
            description="Agenda",
            location="Room B",
        )
        service, _, write = self.service([current], [current])

        service.update(draft(
            self.keys["google"], source_uid=source.uid,
            notes=source.description, location=source.location,
            online_meeting="none",
        ))

        self.assertNotIn(link, write.calls[0][2]["description"])
        self.assertNotIn(link, write.calls[0][2]["location"])

    def test_unrelated_updates_do_not_rewrite_normalized_notes_or_location(self):
        google_source = event(description="Visible notes", location="Room B")
        self.store.upsert_event(google_source)
        google_updated = dict(GOOGLE_RAW, id="event-1", summary="Renamed", etag='"etag-2"')
        google, _, google_write = self.service([google_updated], [google_updated])

        google.update(draft(
            self.keys["google"], source_uid=google_source.uid, title="Renamed",
            notes="Visible notes", location="Room B", online_meeting="preserve",
        ))

        self.assertNotIn("description", google_write.calls[0][2])
        self.assertNotIn("location", google_write.calls[0][2])

        outlook_source = event(
            "microsoft", "ma", "mc", "event-1", description="Visible notes",
            location="Room B", meeting_url="",
        )
        self.store.upsert_event(outlook_source)
        current = dict(MICROSOFT_RAW, id="event-1", changeKey="change-1")
        updated = dict(current, changeKey="change-2", subject="Renamed")
        outlook, _, outlook_write = self.service([current, updated], [updated])

        outlook.update(draft(
            self.keys["microsoft"], source_uid=outlook_source.uid, title="Renamed",
            notes="Visible notes", location="Room B",
        ))

        self.assertNotIn("body", outlook_write.calls[0][2])
        self.assertNotIn("location", outlook_write.calls[0][2])

    def test_non_temporal_update_does_not_reparse_an_ambiguous_local_time(self):
        source = event(
            start="2026-11-01T01:30:00-05:00",
            end="2026-11-01T02:30:00-06:00",
            start_day="2026-11-01",
            end_day="2026-11-01",
            meeting_url="",
        )
        self.store.upsert_event(source)
        updated = dict(
            GOOGLE_RAW,
            id="event-1",
            summary="Renamed",
            etag='"etag-2"',
            start={"dateTime": source.start, "timeZone": "America/Chicago"},
            end={"dateTime": source.end, "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([updated], [updated])

        service.update(draft(
            self.keys["google"], source_uid=source.uid, title="Renamed",
            day="2026-11-01", end_day="2026-11-01",
            start="01:30", end="02:30", location="", notes="",
        ))

        self.assertNotIn("start", write.calls[0][2])
        self.assertNotIn("end", write.calls[0][2])

    def test_non_temporal_series_update_does_not_reparse_an_ambiguous_occurrence(self):
        source = event(
            start="2026-11-01T01:30:00-05:00",
            end="2026-11-01T02:30:00-06:00",
            start_day="2026-11-01",
            end_day="2026-11-01",
            series_start="2026-10-25T01:30:00-05:00",
            series_end="2026-10-25T02:30:00-05:00",
            meeting_url="",
        )
        self.store.upsert_event(source)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            summary="Original",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=SU"],
            start={"dateTime": source.series_start, "timeZone": "America/Chicago"},
            end={"dateTime": source.series_end, "timeZone": "America/Chicago"},
        )
        updated = dict(master, summary="Renamed", etag='"master-etag-2"')
        service, _, write = self.service([master], [updated])

        service.update(draft(
            self.keys["google"], source_uid=source.uid, title="Renamed",
            day="2026-11-01", end_day="2026-11-01",
            start="01:30", end="02:30", location="", notes="",
            scope="series",
            series_start=source.series_start,
            series_end=source.series_end,
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertNotIn("start", write.calls[0][2])
        self.assertNotIn("end", write.calls[0][2])

    def test_non_temporal_all_day_series_update_preserves_exact_cached_fields(self):
        source = event(
            start="2026-09-02T00:00:00.000+09:00",
            end="2026-09-03T00:00:00.000+09:00",
            all_day=True,
            start_day="2026-09-02",
            end_day="2026-09-03",
            timezone="Asia/Tokyo",
            series_start="2026-08-05T00:00:00+09:00",
            series_end="2026-08-06T00:00:00+09:00",
            meeting_url="",
        )
        self.store.upsert_event(source)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            summary="Original",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"date": "2026-08-05", "timeZone": "Asia/Tokyo"},
            end={"date": "2026-08-06", "timeZone": "Asia/Tokyo"},
        )
        updated = dict(master, summary="Renamed", etag='"master-etag-2"')
        service, _, write = self.service([master], [updated])

        result = service.update(draft(
            self.keys["google"], source_uid=source.uid, title="Renamed",
            day="2026-09-02", end_day="2026-09-03", start="", end="",
            all_day=True, scope="series", location="", notes="",
            series_start=source.series_start, series_end=source.series_end,
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertNotIn("start", write.calls[0][2])
        self.assertNotIn("end", write.calls[0][2])
        self.assertEqual(result["event"]["start"], source.start)
        self.assertEqual(result["event"]["end"], source.end)
        self.assertEqual(result["event"]["start_day"], source.start_day)
        self.assertEqual(result["event"]["end_day"], source.end_day)
        self.assertEqual(result["event"]["timezone"], source.timezone)

    def test_attendee_events_are_duplicate_only(self):
        source = event(has_attendees=True)
        self.store.upsert_event(source)
        service, read, write = self.service([])

        with self.assertRaisesRegex(PermissionError, "attendees"):
            service.update(draft(self.keys["google"], source_uid=source.uid))
        with self.assertRaisesRegex(PermissionError, "attendees"):
            service.delete_event(source.uid, scope="single", expected_revision=source.revision)

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

        copy_service, _, _ = self.service([MICROSOFT_RAW], [MICROSOFT_RAW])
        copied = copy_service.copy(draft(
            self.keys["microsoft"], source_uid=source.uid,
        ))
        self.assertFalse(copied["delete_original_available"])

    def test_nonrecurring_event_rejects_series_scope_before_network_use(self):
        source = event(
            recurrence_id="", recurrence=(), event_type="single",
            series_revision="", series_start="", series_end="", meeting_url="",
        )
        self.store.upsert_event(source)
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                service, read, write = self.service([])
                with self.assertRaisesRegex(DraftError, "Only a recurring event"):
                    if operation == "update":
                        service.update(draft(
                            self.keys["google"], source_uid=source.uid, scope="series",
                            series_revision="", series_start="", series_end="",
                        ))
                    else:
                        service.delete_event(
                            source.uid, scope="series", expected_revision=source.revision,
                        )
                self.assertEqual(read.calls, [])
                self.assertEqual(write.calls, [])

    def test_outlook_create_update_and_delete_refresh_the_local_cache(self):
        created = dict(MICROSOFT_RAW, changeKey="change-2")
        updated = dict(MICROSOFT_RAW, changeKey="change-3", subject="Renamed")
        service, _, write = self.service(
            [created, created, updated, updated],
            [created, {}, {}],
        )

        created_result = service.create(draft(self.keys["microsoft"]))
        uid = created_result["event"]["uid"]
        service.update(draft(
            self.keys["microsoft"], source_uid=uid, source_revision="change-2", title="Renamed",
        ))
        service.delete_event(uid, scope="single", expected_revision="change-3")

        self.assertEqual([call[0] for call in write.calls], ["POST", "PATCH", "DELETE"])
        self.assertIsNone(self.store.get_event(uid))

    def test_expired_outlook_edit_token_refreshes_without_losing_edit_access(self):
        self.tokens[("microsoft", "ma")] = {
            "access_token": "expired", "refresh_token": "refresh", "expires_at": 0,
            "access_mode": "edit", "scope": "Calendars.ReadWrite",
        }
        keyring = FakeKeyring(self.tokens)
        read = FakeReadHttp(
            [MICROSOFT_RAW],
            token_response={"access_token": "refreshed", "expires_in": 3600},
        )
        write = FakeWriteHttp([MICROSOFT_RAW])
        service = MutationService(
            self.store,
            keyring=keyring,
            settings=ProviderSettings(
                microsoft_client_id="11111111-2222-3333-4444-555555555555"
            ),
            read_http=read,
            write_http=write,
            now=lambda: datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        )

        service.create(draft(self.keys["microsoft"]))

        self.assertEqual(write.calls[0][3]["Authorization"], "Bearer refreshed")
        self.assertEqual(self.tokens[("microsoft", "ma")]["access_mode"], "edit")
        self.assertEqual(read.posts[0][1]["grant_type"], "refresh_token")

    def test_expired_google_edit_token_refreshes_without_losing_edit_access(self):
        self.tokens[("google", "ga")] = {
            "access_token": "expired", "refresh_token": "refresh", "expires_at": 0,
            "access_mode": "edit",
            "scope": "https://www.googleapis.com/auth/calendar.events.owned",
        }
        keyring = FakeKeyring(self.tokens)
        read = FakeReadHttp(
            [GOOGLE_RAW],
            token_response={"access_token": "refreshed", "expires_in": 3600},
        )
        write = FakeWriteHttp([GOOGLE_RAW])
        service = MutationService(
            self.store,
            keyring=keyring,
            settings=ProviderSettings(),
            read_http=read,
            write_http=write,
            now=lambda: datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
            timezone="America/Chicago",
        )

        service.create(draft(self.keys["google"]))

        self.assertEqual(write.calls[0][3]["Authorization"], "Bearer refreshed")
        self.assertEqual(self.tokens[("google", "ga")]["access_mode"], "edit")
        self.assertEqual(
            self.tokens[("google", "ga")]["scope"],
            "https://www.googleapis.com/auth/calendar.events.owned",
        )
        self.assertEqual(read.posts[0][1]["grant_type"], "refresh_token")

    def test_cross_provider_copy_verifies_destination_and_never_deletes_source(self):
        copied = dict(
            MICROSOFT_RAW,
            body={
                "contentType": "text",
                "content": f"Meeting: {self.source.meeting_url}\n\nNotes",
            },
        )
        service, _, write = self.service([copied], [copied])
        copy_draft = draft(
            self.keys["microsoft"], source_uid=self.source.uid, online_meeting="preserve",
        )

        result = service.copy(copy_draft)

        self.assertEqual(result["mode"], "copy")
        self.assertTrue(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertEqual(result["delete_original_reason"], "")
        self.assertEqual(result["original_provider"], "google")
        self.assertEqual(result["original_account_id"], "ga")
        self.assertIn(self.source.meeting_url, write.calls[0][2]["body"]["content"])
        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_transfer_never_offers_deletion_when_destination_semantics_do_not_match(self):
        mismatches = {
            "destination title": {"subject": "An unrelated existing event"},
            "all-day mode": {
                "isAllDay": True,
                "start": {"dateTime": "2026-09-02T00:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-09-03T00:00:00", "timeZone": "UTC"},
            },
            "start boundary": {
                "start": {"dateTime": "2026-09-02T15:15:00", "timeZone": "UTC"},
            },
            "recurrence": {
                "type": "seriesMaster",
                "recurrence": {
                    "pattern": {"type": "daily", "interval": 1},
                    "range": {"type": "noEnd", "startDate": "2026-09-02"},
                },
            },
            "location": {"location": {"displayName": "Wrong room"}},
            "notes": {"body": {"contentType": "text", "content": "Wrong notes"}},
            "meeting behavior": {
                "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/unrequested"},
            },
        }
        for name, changes in mismatches.items():
            with self.subTest(field=name):
                returned = {**MICROSOFT_RAW, **changes}
                service, _, write = self.service([returned], [returned])

                with self.assertRaisesRegex(DraftError, "could not be verified"):
                    service.copy(draft(
                        self.keys["microsoft"], source_uid=self.source.uid,
                        online_meeting="none",
                    ))

                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_transfer_requires_the_preserved_meeting_link_in_the_destination(self):
        service, _, write = self.service([MICROSOFT_RAW], [MICROSOFT_RAW])

        with self.assertRaisesRegex(DraftError, "could not be verified"):
            service.copy(draft(
                self.keys["microsoft"], source_uid=self.source.uid,
                online_meeting="preserve",
            ))

        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_transfer_rejects_wrong_destination_identity_and_missing_recurrence(self):
        cases = (
            (
                dict(MICROSOFT_RAW, id="another-event"),
                MICROSOFT_RAW,
                {},
            ),
            (
                MICROSOFT_RAW,
                MICROSOFT_RAW,
                {"recurrence": {"frequency": "daily", "weekdays": [], "end": "never"}},
            ),
        )
        for fetched, created, changes in cases:
            with self.subTest(changes=changes, fetched_id=fetched["id"]):
                service, _, write = self.service([fetched], [created])

                with self.assertRaisesRegex(DraftError, "could not be verified"):
                    service.copy(draft(
                        self.keys["microsoft"], source_uid=self.source.uid,
                        online_meeting="none", **changes,
                    ))

                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_same_calendar_duplicate_never_offers_deleting_the_source(self):
        service, _, _ = self.service([GOOGLE_RAW], [GOOGLE_RAW])

        result = service.copy(draft(
            self.keys["google"], source_uid=self.source.uid,
            online_meeting="preserve",
        ))

        self.assertFalse(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertEqual(result["original_uid"], "")
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_same_provider_different_calendar_transfers_verify_before_offering_delete(self):
        window = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        cases = (
            (
                "google", "ga", self.google_calendar,
                Calendar(
                    "google", "ga", "person@example.com", "gc-2", "Personal", "#9ece6a",
                    "America/Chicago", True, True, ("googleMeet",),
                ),
                event(meeting_url=""), dict(GOOGLE_RAW, location="Room B", description="Notes"),
            ),
            (
                "microsoft", "ma", self.microsoft_calendar,
                Calendar(
                    "microsoft", "ma", "person@outlook.example", "mc-2", "Personal", "#9ece6a",
                    "America/Chicago", True, True, ("teamsForBusiness",),
                ),
                event("microsoft", "ma", "mc", "event-1", meeting_url=""), MICROSOFT_RAW,
            ),
        )
        for provider, account, original, destination, source, returned in cases:
            with self.subTest(provider=provider):
                self.store.replace_window(
                    provider, account, *window, [source],
                    ProviderHealth.ok(provider, account, window[0]),
                    calendars=[original, destination],
                )
                destination_key = next(
                    item["key"] for item in self.store.view(*window)["calendars"]
                    if item["provider"] == provider
                    and self.store.get_calendar(item["key"]).calendar_id == destination.calendar_id
                )
                service, _, write = self.service([returned], [returned])

                result = service.copy(draft(
                    destination_key, source_uid=source.uid, online_meeting="none",
                ))

                self.assertTrue(result["delete_original_available"])
                self.assertIn(destination.calendar_id, write.calls[0][1])
                self.assertIsNotNone(self.store.get_event(source.uid))

    def test_verified_copy_then_failed_source_delete_keeps_both_cached_events(self):
        class DeleteFailsAfterCopy:
            def __init__(self):
                self.calls = []

            def request_json(self, method, url, body=None, headers=None):
                self.calls.append((method, url, body, headers or {}))
                if method == "DELETE":
                    raise HttpError(503, "source delete failed")
                return dict(MICROSOFT_RAW)

        write = DeleteFailsAfterCopy()
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=FakeReadHttp([MICROSOFT_RAW]),
            write_http=write,
            timezone="America/Chicago",
        )

        copied = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, online_meeting="none",
        ))
        with self.assertRaisesRegex(HttpError, "source delete failed"):
            service.delete_event(
                self.source.uid, scope="single", expected_revision=self.source.revision,
            )

        self.assertTrue(copied["delete_original_available"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))
        self.assertIsNotNone(self.store.get_event(copied["event"]["uid"]))
        self.assertEqual([call[0] for call in write.calls], ["POST", "DELETE"])

    def test_clean_google_series_transfer_offers_separately_confirmed_source_delete(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )

        class SeriesRead:
            def __init__(self):
                self.calls = []

            def get_json(self, url, headers=None):
                self.calls.append((url, headers or {}))
                if "/calendars/gc/events/series-1" in url:
                    return dict(master)
                if "/calendars/gc/events?" in url:
                    if "pageToken=next-page" in url:
                        return {"items": []}
                    return {
                        "items": [{
                            "id": "other-exception",
                            "recurringEventId": "another-series",
                        }],
                        "nextPageToken": "next-page",
                    }
                if "/calendars/mc/events/created-microsoft" in url:
                    return dict(created)
                raise AssertionError(f"unexpected read: {url}")

        read = SeriesRead()
        write = FakeWriteHttp([created])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertTrue(result["delete_original_available"])
        self.assertEqual(result["original_scope"], "series")
        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertTrue(any("showDeleted=true" in url for url, _ in read.calls))
        self.assertTrue(any("pageToken=next-page" in url for url, _ in read.calls))
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_google_series_transfer_keeps_original_when_an_occurrence_changed(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )

        for occurrence in (
            {
                "id": "modified-occurrence",
                "status": "confirmed",
                "recurringEventId": "series-1",
                "originalStartTime": {"dateTime": "2026-09-02T10:00:00-05:00"},
            },
            {
                "id": "cancelled-occurrence",
                "status": "cancelled",
                "recurringEventId": "series-1",
                "originalStartTime": {"dateTime": "2026-09-02T10:00:00-05:00"},
            },
        ):
            with self.subTest(status=occurrence["status"]):
                class SeriesRead:
                    def get_json(inner_self, url, headers=None):
                        if "/calendars/gc/events/series-1" in url:
                            return dict(master)
                        if "/calendars/gc/events?" in url:
                            return {"items": [dict(occurrence)]}
                        if "/calendars/mc/events/created-microsoft" in url:
                            return dict(created)
                        raise AssertionError(f"unexpected read: {url}")

                write = FakeWriteHttp([created])
                service = MutationService(
                    self.store,
                    keyring=FakeKeyring(self.tokens),
                    read_http=SeriesRead(),
                    write_http=write,
                    timezone="America/Chicago",
                )

                result = service.copy(draft(
                    self.keys["microsoft"], source_uid=self.source.uid, scope="series",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                ))

                self.assertFalse(result["delete_original_available"])
                self.assertFalse(result["delete_original_needs_permission"])
                self.assertEqual(
                    result["delete_original_reason"],
                    "The original series has modified or cancelled occurrences, "
                    "so it cannot be deleted after this copy.",
                )
                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_clean_outlook_series_transfer_offers_separately_confirmed_source_delete(self):
        source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
            cancelledOccurrences=[],
            exceptionOccurrences=[],
        )
        created = dict(
            GOOGLE_RAW,
            id="google-copy-series",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            location="Room B",
            description="Notes",
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )

        class SeriesRead:
            def __init__(self):
                self.calls = []

            def get_json(inner_self, url, headers=None):
                inner_self.calls.append((url, headers or {}))
                if "/me/calendars/mc/events/series-1" in url:
                    return dict(master)
                if "/calendars/gc/events/google-copy-series" in url:
                    return dict(created)
                raise AssertionError(f"unexpected read: {url}")

        read = SeriesRead()
        write = FakeWriteHttp([created])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )

        result = service.copy(draft(
            self.keys["google"], source_uid=source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertTrue(result["delete_original_available"])
        self.assertEqual(result["original_scope"], "series")
        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertTrue(any("%24select=" in url for url, _ in read.calls))
        self.assertTrue(any("%24expand=" in url for url, _ in read.calls))
        self.assertTrue(any(
            "%2CcancelledOccurrences%2CexceptionOccurrences&%24expand=" in url
            for url, _ in read.calls
        ))
        self.assertIsNotNone(self.store.get_event(source.uid))

    def test_same_provider_clean_series_transfer_is_copy_first(self):
        window = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        cases = (
            (
                "google",
                self.source,
                self.google_calendar,
                Calendar(
                    "google", "ga", "person@example.com", "gc-2", "Personal", "#9ece6a",
                    "America/Chicago", True, True, ("googleMeet",),
                ),
                dict(
                    GOOGLE_RAW,
                    id="series-1",
                    etag='"master-etag"',
                    recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                    start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
                    end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
                ),
                dict(
                    GOOGLE_RAW,
                    id="copied-google-series",
                    location="Room B",
                    description="Notes",
                    recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                    start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
                    end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
                ),
            ),
            (
                "microsoft",
                event("microsoft", "ma", "mc", "occurrence-1", revision="change-1"),
                self.microsoft_calendar,
                Calendar(
                    "microsoft", "ma", "person@outlook.example", "mc-2", "Personal", "#9ece6a",
                    "America/Chicago", True, True, ("teamsForBusiness",),
                ),
                dict(
                    MICROSOFT_RAW,
                    id="series-1",
                    changeKey="master-change",
                    type="seriesMaster",
                    recurrence={
                        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                        "range": {"type": "noEnd", "startDate": "2026-08-05"},
                    },
                    start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
                    end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
                    cancelledOccurrences=[],
                    exceptionOccurrences=[],
                ),
                dict(
                    MICROSOFT_RAW,
                    id="copied-microsoft-series",
                    type="seriesMaster",
                    recurrence={
                        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                        "range": {
                            "type": "noEnd", "startDate": "2026-08-05",
                            "recurrenceTimeZone": "America/Chicago",
                        },
                    },
                    start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
                    end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
                ),
            ),
        )

        for provider, source, original, destination, master, created in cases:
            with self.subTest(provider=provider):
                self.store.replace_window(
                    provider, source.account_id, *window, [source],
                    ProviderHealth.ok(provider, source.account_id, window[0]),
                    calendars=[original, destination],
                )
                destination_key = next(
                    item["key"] for item in self.store.view(*window)["calendars"]
                    if item["provider"] == provider
                    and self.store.get_calendar(item["key"]).calendar_id == destination.calendar_id
                )
                reads = [master, created]
                if provider == "google":
                    reads.append({"items": []})
                service, _, write = self.service(reads, [created])

                result = service.copy(draft(
                    destination_key, source_uid=source.uid, scope="series",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                ))

                self.assertTrue(result["delete_original_available"])
                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIn(f"/calendars/{destination.calendar_id}/events", write.calls[0][1])
                self.assertIsNotNone(self.store.get_event(source.uid))

    def test_outlook_series_transfer_keeps_original_for_deviations_or_unknown_shape(self):
        source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(source)
        base_master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
            cancelledOccurrences=[],
            exceptionOccurrences=[],
        )
        created = dict(
            GOOGLE_RAW,
            id="google-copy-series",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            location="Room B",
            description="Notes",
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        unknown_reason = (
            "Flight Deck could not verify every series occurrence, "
            "so the original cannot be deleted after this copy."
        )
        deviation_reason = (
            "The original series has modified or cancelled occurrences, "
            "so it cannot be deleted after this copy."
        )
        cases = (
            ({"exceptionOccurrences": [{"id": "exception-1"}]}, deviation_reason),
            ({"cancelledOccurrences": ["occurrence-1"]}, deviation_reason),
            ({"exceptionOccurrences": None}, unknown_reason),
            ({"cancelledOccurrences": None}, unknown_reason),
            ({"exceptionOccurrences@odata.nextLink": "https://graph.example/next"}, unknown_reason),
        )

        for changes, expected_reason in cases:
            with self.subTest(changes=changes):
                master = {**base_master, **changes}

                class SeriesRead:
                    def get_json(inner_self, url, headers=None):
                        if "/me/calendars/mc/events/series-1" in url:
                            return dict(master)
                        if "/calendars/gc/events/google-copy-series" in url:
                            return dict(created)
                        raise AssertionError(f"unexpected read: {url}")

                write = FakeWriteHttp([created])
                service = MutationService(
                    self.store,
                    keyring=FakeKeyring(self.tokens),
                    read_http=SeriesRead(),
                    write_http=write,
                    timezone="America/Chicago",
                )

                result = service.copy(draft(
                    self.keys["google"], source_uid=source.uid, scope="series",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                ))

                self.assertFalse(result["delete_original_available"])
                self.assertFalse(result["delete_original_needs_permission"])
                self.assertEqual(result["delete_original_reason"], expected_reason)
                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIsNotNone(self.store.get_event(source.uid))

    def test_google_series_unverifiable_scan_copies_but_never_offers_source_deletion(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )

        for scan_response in (
            HttpError(503, "series scan unavailable"),
            {"nextPageToken": "missing-items"},
            {"items": [None]},
        ):
            with self.subTest(scan_response=type(scan_response).__name__):
                class SeriesRead:
                    def get_json(inner_self, url, headers=None):
                        if "/calendars/gc/events/series-1" in url:
                            return dict(master)
                        if "/calendars/gc/events?" in url:
                            if isinstance(scan_response, Exception):
                                raise scan_response
                            return dict(scan_response)
                        if "/calendars/mc/events/created-microsoft" in url:
                            return dict(created)
                        raise AssertionError(f"unexpected read: {url}")

                write = FakeWriteHttp([created])
                service = MutationService(
                    self.store,
                    keyring=FakeKeyring(self.tokens),
                    read_http=SeriesRead(),
                    write_http=write,
                    timezone="America/Chicago",
                )

                result = service.copy(draft(
                    self.keys["microsoft"], source_uid=self.source.uid, scope="series",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                ))

                self.assertFalse(result["delete_original_available"])
                self.assertEqual(
                    result["delete_original_reason"],
                    "Flight Deck could not verify every series occurrence, "
                    "so the original cannot be deleted after this copy.",
                )
                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_google_series_incomplete_pagination_fails_closed(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )

        class SeriesRead:
            def __init__(self):
                self.scan_calls = 0

            def get_json(inner_self, url, headers=None):
                if "/calendars/gc/events/series-1" in url:
                    return dict(master)
                if "/calendars/mc/events/created-microsoft" in url:
                    return dict(created)
                if "/calendars/gc/events?" in url:
                    inner_self.scan_calls += 1
                    return {"items": [], "nextPageToken": "repeated-token"}
                raise AssertionError(f"unexpected read: {url}")

        read = SeriesRead()
        write = FakeWriteHttp([created])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertFalse(result["delete_original_available"])
        self.assertEqual(read.scan_calls, 2)
        self.assertIn("could not verify every series occurrence", result["delete_original_reason"])
        self.assertEqual([call[0] for call in write.calls], ["POST"])

    def test_google_series_copies_to_outlook_with_equivalent_recurrence(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=6"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "numbered", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago", "numberOfOccurrences": 6,
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )
        service, read, write = self.service([master, created, {"items": []}], [created])

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        recurrence = write.calls[0][2]["recurrence"]
        self.assertEqual(recurrence["pattern"]["daysOfWeek"], ["wednesday"])
        self.assertEqual(recurrence["range"]["type"], "numbered")
        self.assertEqual(recurrence["range"]["numberOfOccurrences"], 6)
        self.assertEqual(result["original_scope"], "series")
        self.assertEqual(result["original_series_revision"], '"master-etag"')
        self.assertTrue(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertEqual(result["delete_original_reason"], "")
        self.assertTrue(read.calls[0][0].endswith("/events/series-1"))
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_series_transfer_rejects_destination_recurrence_timezone_or_range_start_mismatch(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        base_range = {
            "type": "noEnd",
            "startDate": "2026-08-05",
            "recurrenceTimeZone": "America/Chicago",
        }
        for name, range_change in (
            ("timezone", {"recurrenceTimeZone": "UTC"}),
            ("range start", {"startDate": "2026-08-06"}),
        ):
            with self.subTest(field=name):
                created = dict(
                    MICROSOFT_RAW,
                    type="seriesMaster",
                    recurrence={
                        "pattern": {
                            "type": "weekly", "interval": 1,
                            "daysOfWeek": ["wednesday"],
                        },
                        "range": {**base_range, **range_change},
                    },
                    start={
                        "dateTime": "2026-08-05T10:00:00",
                        "timeZone": "America/Chicago",
                    },
                    end={
                        "dateTime": "2026-08-05T11:00:00",
                        "timeZone": "America/Chicago",
                    },
                )
                service, _, write = self.service([master, created], [created])

                with self.assertRaisesRegex(DraftError, "could not be verified"):
                    service.copy(draft(
                        self.keys["microsoft"], source_uid=self.source.uid,
                        scope="series",
                        recurrence={
                            "frequency": "preserve", "weekdays": [], "end": "never",
                        },
                    ))

                self.assertEqual([call[0] for call in write.calls], ["POST"])
                self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_series_transfer_rejects_a_google_recurrence_timezone_mismatch(self):
        source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {
                    "type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"],
                },
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        created = dict(
            GOOGLE_RAW,
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            location="Room B",
            description="Notes",
            start={"dateTime": "2026-08-05T15:00:00Z", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00Z", "timeZone": "UTC"},
        )
        service, _, write = self.service([master, created], [created])

        with self.assertRaisesRegex(DraftError, "could not be verified"):
            service.copy(draft(
                self.keys["google"], source_uid=source.uid, scope="series",
                recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
            ))

        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(source.uid))

    def test_entire_series_copy_uses_master_fields_instead_of_exception_fields(self):
        exception = event(
            title="Exception title",
            start="2026-09-02T12:00:00-05:00",
            end="2026-09-02T14:00:00-05:00",
            location="Exception room",
            description="Exception notes",
            meeting_url="https://meet.google.com/exception-room",
        )
        self.store.upsert_event(exception)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            summary="Master title",
            location="Master room",
            description="Master notes",
            hangoutLink="https://meet.google.com/master-room",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            id="copied-series",
            subject="Master title",
            type="seriesMaster",
            location={"displayName": "Master room"},
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
            body={
                "contentType": "text",
                "content": "Meeting: https://meet.google.com/master-room\n\nMaster notes",
            },
        )
        service, _, write = self.service([
            master,
            created,
            {"items": [{"id": "exception", "recurringEventId": "series-1"}]},
        ], [created])

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=exception.uid, scope="series",
            title="Exception title", day="2026-09-02", start="12:00", end="14:00",
            location="Exception room", notes="Exception notes", online_meeting="preserve",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        payload = write.calls[0][2]
        self.assertEqual(payload["subject"], "Master title")
        self.assertEqual(payload["location"]["displayName"], "Master room")
        self.assertIn("Master notes", payload["body"]["content"])
        self.assertIn("https://meet.google.com/master-room", payload["body"]["content"])
        self.assertNotIn("Exception", str(payload))
        self.assertEqual(payload["start"]["dateTime"], "2026-08-05T10:00:00")
        self.assertEqual(payload["end"]["dateTime"], "2026-08-05T11:00:00")
        self.assertEqual(result["event"]["title"], "Master title")
        self.assertEqual(result["event"]["start_day"], "2026-09-02")
        self.assertEqual(result["event"]["start"], "2026-09-02T10:00:00-05:00")

    def test_outlook_entire_series_copy_uses_master_fields_instead_of_exception_fields(self):
        exception = event(
            "microsoft", "ma", "mc", "occurrence-1",
            title="Exception title",
            start="2026-09-02T17:00:00+00:00",
            end="2026-09-02T19:00:00+00:00",
            location="Exception room",
            description="Exception notes",
            meeting_url="https://teams.microsoft.com/l/meetup-join/exception",
            revision="change-1",
        )
        self.store.upsert_event(exception)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            subject="Master title",
            body={"contentType": "text", "content": "Master notes"},
            bodyPreview="Master notes",
            location={"displayName": "Master room"},
            onlineMeeting={"joinUrl": "https://teams.microsoft.com/l/meetup-join/master"},
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
            cancelledOccurrences=[],
            exceptionOccurrences=[{"id": "occurrence-1"}],
        )
        created = dict(
            GOOGLE_RAW,
            id="copied-series",
            summary="Master title",
            location="Master room",
            description=(
                "Meeting: https://teams.microsoft.com/l/meetup-join/master\n\nMaster notes"
            ),
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master, created], [created])

        result = service.copy(draft(
            self.keys["google"], source_uid=exception.uid, scope="series",
            title="Exception title", day="2026-09-02", start="12:00", end="14:00",
            location="Exception room", notes="Exception notes", online_meeting="preserve",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        payload = write.calls[0][2]
        self.assertEqual(payload["summary"], "Master title")
        self.assertEqual(payload["location"], "Master room")
        self.assertIn("Master notes", payload["description"])
        self.assertIn("https://teams.microsoft.com/l/meetup-join/master", payload["description"])
        self.assertNotIn("Exception", str(payload))
        self.assertEqual(payload["start"]["dateTime"], "2026-08-05T10:00:00-05:00")
        self.assertEqual(payload["end"]["dateTime"], "2026-08-05T11:00:00-05:00")
        self.assertEqual(result["event"]["start_day"], "2026-09-02")

    def test_outlook_series_copies_to_google_with_equivalent_recurrence(self):
        source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "endDate", "startDate": "2026-08-05", "endDate": "2026-12-30"},
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
            cancelledOccurrences=[],
            exceptionOccurrences=[],
        )
        created = dict(
            GOOGLE_RAW,
            id="google-copy-series",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261231T055959Z"],
            location="Room B",
            description="Notes",
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master, created], [created])

        result = service.copy(draft(
            self.keys["google"], source_uid=source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(
            write.calls[0][2]["recurrence"],
            ["RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261231T055959Z"],
        )
        self.assertEqual(result["original_scope"], "series")
        self.assertEqual(result["original_series_revision"], "master-change")
        self.assertTrue(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertEqual(result["delete_original_reason"], "")

    def test_series_copy_rejects_unsupported_recurrence_without_destination_write(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=YEARLY;BYMONTH=9"],
        )
        service, _, write = self.service([master])

        with self.assertRaisesRegex(DraftError, "not supported"):
            service.copy(draft(
                self.keys["microsoft"], source_uid=self.source.uid, scope="series",
                recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
            ))

        self.assertEqual(write.calls, [])

    def test_malformed_provider_recurrence_is_rejected_as_an_unsupported_series(self):
        service, _, _ = self.service([])
        cases = (
            (
                self.store.get_calendar(self.keys["google"]),
                {
                    "recurrence": ["RRULE:FREQ=WEEKLY"],
                    "start": {"dateTime": "not-a-date"},
                },
            ),
            (
                self.store.get_calendar(self.keys["microsoft"]),
                {
                    "recurrence": {
                        "pattern": {"type": "daily", "interval": "many"},
                        "range": {"type": "noEnd"},
                    },
                },
            ),
        )
        for calendar, raw in cases:
            with self.subTest(provider=calendar.provider), self.assertRaisesRegex(
                DraftError, "not supported"
            ):
                service._source_recurrence(calendar, raw)

    def test_series_copy_rejects_a_changed_master_before_destination_write(self):
        changed = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"changed-master"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
        )
        service, _, write = self.service([changed])

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.copy(draft(
                self.keys["microsoft"], source_uid=self.source.uid, scope="series",
                recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
            ))

        self.assertEqual(write.calls, [])
        self.assertEqual(
            self.store.get_event("google:ga:gc:series-1")["revision"],
            '"changed-master"',
        )

    def test_series_copy_rejects_forged_cached_time_baselines_before_network_use(self):
        for provider in ("google", "microsoft"):
            with self.subTest(provider=provider):
                source = (
                    self.source if provider == "google"
                    else event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
                )
                self.store.upsert_event(source)
                destination = self.keys["microsoft" if provider == "google" else "google"]
                service, read, write = self.service([])

                with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
                    service.copy(draft(
                        destination,
                        source_uid=source.uid,
                        scope="series",
                        series_start="2026-01-01T10:00:00-06:00",
                        series_end="2026-01-01T11:00:00-06:00",
                        recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                    ))

                self.assertEqual(read.calls, [])
                self.assertEqual(write.calls, [])

    def test_all_day_series_baseline_compares_provider_dates_not_equivalent_instants(self):
        source = event(
            start="2026-09-02T00:00:00+14:00",
            end="2026-09-03T00:00:00+14:00",
            all_day=True,
            start_day="2026-09-02",
            end_day="2026-09-03",
            series_start="2026-08-05T00:00:00+14:00",
            series_end="2026-08-06T00:00:00+14:00",
            meeting_url="",
        )
        self.store.upsert_event(source)
        service, read, write = self.service([])

        with self.assertRaisesRegex(MutationConflict, "Draft preserved"):
            service.copy(draft(
                self.keys["microsoft"], source_uid=source.uid, scope="series",
                all_day=True, start="", end="", day="2026-09-02", end_day="2026-09-03",
                series_start="2026-08-04T10:00:00+00:00",
                series_end="2026-08-05T10:00:00+00:00",
                recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
            ))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_google_series_until_uses_the_series_timezone_when_copied_to_outlook(self):
        source = event(
            timezone="Pacific/Kiritimati",
            series_start="2026-08-05T10:00:00+14:00",
            series_end="2026-08-05T11:00:00+14:00",
        )
        self.store.upsert_event(source)
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261231T103000Z"],
            start={"dateTime": "2026-08-05T10:00:00+14:00", "timeZone": "Pacific/Kiritimati"},
            end={"dateTime": "2026-08-05T11:00:00+14:00", "timeZone": "Pacific/Kiritimati"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "endDate", "startDate": "2026-08-04",
                    "recurrenceTimeZone": "America/Chicago", "endDate": "2027-01-01",
                },
            },
            start={"dateTime": "2026-08-04T15:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-04T16:00:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master, created, {"items": []}], [created])

        service.copy(draft(
            self.keys["microsoft"], source_uid=source.uid, scope="series",
            series_start=source.series_start, series_end=source.series_end,
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2]["recurrence"]["range"]["endDate"], "2027-01-01")

    def test_series_copy_moves_the_provider_schedule_with_the_draft_day(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {
                    "type": "weekly", "interval": 1, "daysOfWeek": ["thursday"],
                },
                "range": {
                    "type": "noEnd", "startDate": "2026-08-06",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-06T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-06T11:00:00", "timeZone": "America/Chicago"},
        )
        service, _, write = self.service([master, created, {"items": []}], [created])

        service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, scope="series",
            day="2026-09-03", end_day="2026-09-03",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertEqual(write.calls[0][2]["start"]["dateTime"], "2026-08-06T10:00:00")
        self.assertEqual(
            write.calls[0][2]["recurrence"]["pattern"]["daysOfWeek"],
            ["thursday"],
        )

    def test_entire_series_copy_revalidates_master_before_offering_source_deletion(self):
        cases = (
            ("google", "attendees", "attendees"),
            ("google", "ownership", "organizer"),
            ("microsoft", "attendees", "attendees"),
            ("microsoft", "ownership", "organizer"),
        )
        for provider, restriction, reason in cases:
            with self.subTest(provider=provider, restriction=restriction):
                if provider == "google":
                    source = self.source
                    master = dict(
                        GOOGLE_RAW, id="series-1", etag='"master-etag"',
                        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                    )
                    destination = self.keys["microsoft"]
                    created = dict(
                        MICROSOFT_RAW,
                        type="seriesMaster",
                        recurrence={
                            "pattern": {
                                "type": "weekly", "interval": 1,
                                "daysOfWeek": ["wednesday"],
                            },
                            "range": {
                                "type": "noEnd", "startDate": "2026-08-05",
                                "recurrenceTimeZone": "America/Chicago",
                            },
                        },
                        start={
                            "dateTime": "2026-08-05T10:00:00",
                            "timeZone": "America/Chicago",
                        },
                        end={
                            "dateTime": "2026-08-05T11:00:00",
                            "timeZone": "America/Chicago",
                        },
                    )
                else:
                    source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
                    self.store.upsert_event(source)
                    master = dict(
                        MICROSOFT_RAW, id="series-1", changeKey="master-change", type="seriesMaster",
                        recurrence={
                            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                            "range": {"type": "noEnd", "startDate": "2026-09-02"},
                        },
                        cancelledOccurrences=[], exceptionOccurrences=[],
                    )
                    destination = self.keys["google"]
                    created = dict(
                        GOOGLE_RAW,
                        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                        location="Room B",
                        description="Notes",
                        start={
                            "dateTime": "2026-08-05T10:00:00-05:00",
                            "timeZone": "America/Chicago",
                        },
                        end={
                            "dateTime": "2026-08-05T11:00:00-05:00",
                            "timeZone": "America/Chicago",
                        },
                    )
                if restriction == "attendees":
                    master["attendees"] = (
                        [{"email": "guest@example.com"}] if provider == "google" else [{
                            "emailAddress": {"name": "Guest", "address": "guest@example.com"},
                            "status": {"response": "none", "time": "0001-01-01T00:00:00Z"},
                            "type": "required",
                        }]
                    )
                elif provider == "google":
                    master["organizer"] = {"email": "other@example.com", "self": False}
                else:
                    master["organizer"] = {"emailAddress": {"address": "other@example.com"}}
                    master["isOrganizer"] = False
                reads = [master, created]
                if provider == "google":
                    reads.append({"items": []})
                service, _, _ = self.service(reads, [created])

                result = service.copy(draft(
                    destination, source_uid=source.uid, scope="series",
                    recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
                ))

                self.assertFalse(result["delete_original_available"])
                self.assertEqual(
                    result["delete_original_reason"],
                    (
                        "The original has attendees, so Flight Deck will keep both events."
                        if restriction == "attendees"
                        else "You are not the organizer, so the original cannot be deleted."
                    ),
                )

    def test_copy_offers_source_deletion_only_for_an_owned_writable_calendar(self):
        shared = Calendar(
            "google", "ga", "person@example.com", "shared", "Shared", "#7aa2f7",
            "America/Chicago", True, False, (),
        )
        shared_source = event(calendar="shared", event_id="shared-event")
        window = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        self.store.replace_window(
            "google", "ga", *window, [shared_source],
            ProviderHealth.ok("google", "ga", window[0]),
            calendars=[self.google_calendar, shared],
        )
        service, _, _ = self.service([MICROSOFT_RAW], [MICROSOFT_RAW])

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=shared_source.uid,
        ))

        self.assertFalse(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertEqual(
            result["delete_original_reason"],
            "The source calendar is shared or read-only, so the original cannot be deleted.",
        )

    def test_copy_does_not_offer_source_deletion_without_source_write_scope(self):
        self.tokens[("google", "ga")]["scope"] = (
            "https://www.googleapis.com/auth/calendar.events.readonly"
        )
        service, _, _ = self.service([MICROSOFT_RAW], [MICROSOFT_RAW])

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid,
        ))

        self.assertFalse(result["delete_original_available"])
        self.assertTrue(result["delete_original_needs_permission"])
        self.assertEqual(
            result["delete_original_reason"],
            "Enable editing for person@example.com to delete the original.",
        )

    def test_copy_does_not_offer_source_deletion_while_source_is_offline(self):
        self.store.set_health(ProviderHealth(
            "google", "ga", False, "2026-09-02T14:00:00Z",
            "offline", "", True,
        ))
        service, _, write = self.service([MICROSOFT_RAW], [MICROSOFT_RAW])

        result = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid,
        ))

        self.assertFalse(result["delete_original_available"])
        self.assertFalse(result["delete_original_needs_permission"])
        self.assertEqual(
            result["delete_original_reason"],
            "The source account is offline, so the original cannot be deleted yet.",
        )
        self.assertEqual([call[0] for call in write.calls], ["POST"])

    def test_copy_explains_when_the_original_is_intrinsically_undeletable(self):
        service, _, _ = self.service([MICROSOFT_RAW, MICROSOFT_RAW], [MICROSOFT_RAW, MICROSOFT_RAW])

        attendee = event(event_id="attendee", has_attendees=True)
        not_organizer = event(event_id="guest", organizer_owned=False)
        self.store.upsert_event(attendee)
        self.store.upsert_event(not_organizer)

        attendee_result = service.copy(draft(
            self.keys["microsoft"], source_uid=attendee.uid,
            request_id="03e591f5-4431-41a9-bd57-3c0644602b7d",
        ))
        organizer_result = service.copy(draft(
            self.keys["microsoft"], source_uid=not_organizer.uid,
            request_id="0e355376-7af8-42bf-aa28-52183062fb8a",
        ))

        self.assertEqual(
            attendee_result["delete_original_reason"],
            "The original has attendees, so Flight Deck will keep both events.",
        )
        self.assertEqual(
            organizer_result["delete_original_reason"],
            "You are not the organizer, so the original cannot be deleted.",
        )
        self.assertFalse(attendee_result["delete_original_needs_permission"])
        self.assertFalse(organizer_result["delete_original_needs_permission"])

    def test_copy_result_series_delete_rescans_and_keeps_a_new_exception(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )

        class SeriesRead:
            def __init__(self):
                self.scan_calls = 0

            def get_json(inner_self, url, headers=None):
                if "/calendars/gc/events/series-1" in url:
                    return dict(master)
                if "/calendars/mc/events/created-microsoft" in url:
                    return dict(created)
                if "/calendars/gc/events?" in url:
                    inner_self.scan_calls += 1
                    if inner_self.scan_calls == 1:
                        return {"items": []}
                    return {"items": [{
                        "id": "new-cancelled-occurrence",
                        "status": "cancelled",
                        "recurringEventId": "series-1",
                        "originalStartTime": {"dateTime": "2026-09-09T10:00:00-05:00"},
                    }]}
                raise AssertionError(f"unexpected read: {url}")

        read = SeriesRead()
        write = FakeWriteHttp([created])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )
        copied = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        self.assertTrue(copied["delete_original_available"])
        with self.assertRaisesRegex(MutationConflict, "modified or cancelled"):
            service.delete_event(
                self.source.uid,
                scope="series",
                expected_revision=str(copied["original_revision"]),
                series_revision=str(copied["original_series_revision"]),
                series_transfer_guard=copied["original_scope"] == "series",
            )

        self.assertEqual(read.scan_calls, 2)
        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))
        self.assertIsNotNone(self.store.get_event(copied["event"]["uid"]))

    def test_outlook_copy_result_series_delete_rescans_before_delete(self):
        source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(source)
        clean_master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
            cancelledOccurrences=[],
            exceptionOccurrences=[],
        )
        changed_master = {
            **clean_master,
            "exceptionOccurrences": [{"id": "new-exception"}],
        }
        created = dict(
            GOOGLE_RAW,
            id="google-copy-series",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            location="Room B",
            description="Notes",
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )

        class SeriesRead:
            def __init__(self):
                self.master_calls = 0

            def get_json(inner_self, url, headers=None):
                if "/me/calendars/mc/events/series-1" in url:
                    inner_self.master_calls += 1
                    return dict(clean_master if inner_self.master_calls == 1 else changed_master)
                if "/calendars/gc/events/google-copy-series" in url:
                    return dict(created)
                raise AssertionError(f"unexpected read: {url}")

        read = SeriesRead()
        write = FakeWriteHttp([created])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )
        copied = service.copy(draft(
            self.keys["google"], source_uid=source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        with self.assertRaisesRegex(MutationConflict, "modified or cancelled"):
            service.delete_event(
                source.uid,
                scope="series",
                expected_revision=str(copied["original_revision"]),
                series_revision=str(copied["original_series_revision"]),
                series_transfer_guard=copied["original_scope"] == "series",
            )

        self.assertEqual(read.master_calls, 2)
        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(source.uid))
        self.assertIsNotNone(self.store.get_event(copied["event"]["uid"]))

    def test_clean_copy_result_series_delete_rescans_then_deletes(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        created = dict(
            MICROSOFT_RAW,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {
                    "type": "noEnd", "startDate": "2026-08-05",
                    "recurrenceTimeZone": "America/Chicago",
                },
            },
            start={"dateTime": "2026-08-05T10:00:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00", "timeZone": "America/Chicago"},
        )

        class SeriesRead:
            def __init__(self):
                self.master_calls = 0
                self.scan_calls = 0

            def get_json(inner_self, url, headers=None):
                if "/calendars/gc/events/series-1" in url:
                    inner_self.master_calls += 1
                    return dict(master)
                if "/calendars/mc/events/created-microsoft" in url:
                    return dict(created)
                if "/calendars/gc/events?" in url:
                    inner_self.scan_calls += 1
                    return {"items": []}
                raise AssertionError(f"unexpected read: {url}")

        read = SeriesRead()
        write = FakeWriteHttp([created, {}])
        service = MutationService(
            self.store,
            keyring=FakeKeyring(self.tokens),
            read_http=read,
            write_http=write,
            timezone="America/Chicago",
        )
        copied = service.copy(draft(
            self.keys["microsoft"], source_uid=self.source.uid, scope="series",
            recurrence={"frequency": "preserve", "weekdays": [], "end": "never"},
        ))

        deleted = service.delete_event(
            self.source.uid,
            scope="series",
            expected_revision=str(copied["original_revision"]),
            series_revision=str(copied["original_series_revision"]),
            series_transfer_guard=copied["original_scope"] == "series",
        )

        self.assertTrue(deleted["deleted"])
        self.assertEqual(read.scan_calls, 2)
        self.assertEqual(read.master_calls, 3)
        self.assertEqual([call[0] for call in write.calls], ["POST", "DELETE"])
        self.assertIsNone(self.store.get_event(self.source.uid))
        self.assertIsNotNone(self.store.get_event(copied["event"]["uid"]))

    def test_series_delete_uses_master_id_and_removes_only_after_success(self):
        master = dict(GOOGLE_RAW, id="series-1", etag='"master-etag"')
        service, _, write = self.service([master])

        result = service.delete_event(
            self.source.uid, scope="series", expected_revision='"etag-1"',
            series_revision='"master-etag"',
        )

        self.assertEqual(result, {
            "deleted": True, "uid": self.source.uid, "scope": "series", "refresh": True,
        })
        self.assertTrue(write.calls[0][1].endswith("/events/series-1"))
        self.assertEqual(write.calls[0][3]["If-Match"], '"master-etag"')
        self.assertIsNone(self.store.get_event(self.source.uid))

    def test_series_delete_rejects_a_changed_master_before_writing(self):
        master = dict(
            GOOGLE_RAW, id="series-1", etag='"changed-master"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
        )
        service, _, write = self.service([master])

        with self.assertRaisesRegex(MutationConflict, "Delete cancelled"):
            service.delete_event(
                self.source.uid, scope="series", expected_revision='"etag-1"',
                series_revision='"master-etag"',
            )

        self.assertEqual(write.calls, [])
        self.assertEqual(
            self.store.get_event("google:ga:gc:series-1")["revision"],
            '"changed-master"',
        )

    def test_google_series_delete_412_caches_the_latest_master(self):
        current = dict(
            GOOGLE_RAW, id="series-1", etag='"master-etag"',
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
        )
        latest = dict(current, etag='"latest-master"', summary="Changed elsewhere")
        service, read, write = self.service(
            [current, latest], error=HttpError(412, "precondition failed"),
        )

        with self.assertRaisesRegex(MutationConflict, "Delete cancelled"):
            service.delete_event(
                self.source.uid, scope="series", expected_revision='"etag-1"',
                series_revision='"master-etag"',
            )

        self.assertEqual(len(read.calls), 2)
        self.assertEqual(write.calls[0][0], "DELETE")
        cached = self.store.get_event("google:ga:gc:series-1")
        self.assertEqual(cached["revision"], '"latest-master"')
        self.assertEqual(cached["title"], "Changed elsewhere")

    def test_outlook_series_delete_conflict_caches_the_latest_master(self):
        source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(source)
        latest = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="latest-master",
            type="seriesMaster",
            subject="Changed elsewhere",
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                "range": {"type": "noEnd", "startDate": "2026-08-05"},
            },
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        service, _, write = self.service([latest])

        with self.assertRaisesRegex(MutationConflict, "Delete cancelled"):
            service.delete_event(
                source.uid, scope="series", expected_revision=source.revision,
                series_revision=source.series_revision,
            )

        self.assertEqual(write.calls, [])
        cached = self.store.get_event("microsoft:ma:mc:series-1")
        self.assertEqual(cached["revision"], "latest-master")
        self.assertEqual(cached["title"], "Changed elsewhere")

    def test_series_delete_revalidates_master_attendees_and_ownership(self):
        cases = (
            ("google", "attendees", "attendees"),
            ("google", "ownership", "organize"),
            ("microsoft", "attendees", "attendees"),
            ("microsoft", "ownership", "organize"),
        )
        for provider, restriction, message in cases:
            with self.subTest(provider=provider, restriction=restriction):
                if provider == "google":
                    source = self.source
                    master = dict(
                        GOOGLE_RAW, id="series-1", etag='"master-etag"',
                        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                    )
                else:
                    source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
                    self.store.upsert_event(source)
                    master = dict(
                        MICROSOFT_RAW, id="series-1", changeKey="master-change", type="seriesMaster",
                        recurrence={
                            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
                            "range": {"type": "noEnd", "startDate": "2026-09-02"},
                        },
                    )
                if restriction == "attendees":
                    master["attendees"] = (
                        [{"email": "guest@example.com"}] if provider == "google" else [{
                            "emailAddress": {"name": "Guest", "address": "guest@example.com"},
                            "status": {"response": "none", "time": "0001-01-01T00:00:00Z"},
                            "type": "required",
                        }]
                    )
                elif provider == "google":
                    master["organizer"] = {"email": "other@example.com", "self": False}
                else:
                    master["organizer"] = {"emailAddress": {"address": "other@example.com"}}
                    master["isOrganizer"] = False
                service, _, write = self.service([master])

                with self.assertRaisesRegex(PermissionError, message):
                    service.delete_event(
                        source.uid, scope="series", expected_revision=source.revision,
                        series_revision=source.series_revision,
                    )

                self.assertEqual(write.calls, [])
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_outlook_series_delete_fetches_and_deletes_the_master(self):
        outlook_source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        master = dict(MICROSOFT_RAW, id="series-1", changeKey="master-change", type="seriesMaster")
        service, read, write = self.service([master], [{}])

        result = service.delete_event(
            outlook_source.uid, scope="series", expected_revision="change-1",
            series_revision="master-change",
        )

        self.assertTrue(read.calls[0][0].endswith("/events/series-1"))
        self.assertTrue(write.calls[0][1].endswith("/events/series-1"))
        self.assertEqual(result["scope"], "series")
        self.assertIsNone(self.store.get_event(outlook_source.uid))

    def test_retry_after_ambiguous_delete_converges_when_provider_event_is_gone(self):
        class AmbiguousDeleteRead:
            def __init__(self, state, raw):
                self.state = state
                self.raw = raw
                self.calls = []

            def get_json(self, url, headers=None):
                self.calls.append((url, headers or {}))
                if self.state["deleted"]:
                    raise HttpError(404, "event not found")
                return dict(self.raw)

        class AmbiguousDeleteWrite:
            def __init__(self, state):
                self.state = state
                self.calls = []

            def request_json(self, method, url, body=None, headers=None):
                self.calls.append((method, url, body, headers or {}))
                if not self.state["deleted"]:
                    self.state["deleted"] = True
                    raise HttpError(0, "response lost after provider deleted the event")
                raise HttpError(404, "event not found")

        for provider in ("google", "microsoft"):
            for scope in ("single", "series"):
                with self.subTest(provider=provider, scope=scope):
                    source = (
                        event()
                        if provider == "google"
                        else event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
                    )
                    self.store.upsert_event(source)
                    state = {"deleted": False}
                    raw = (
                        dict(
                            GOOGLE_RAW,
                            id="series-1" if scope == "series" else source.provider_event_id,
                            etag=source.series_revision if scope == "series" else source.revision,
                            **({"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=WE"]} if scope == "series" else {}),
                        )
                        if provider == "google"
                        else dict(
                            MICROSOFT_RAW,
                            id="series-1" if scope == "series" else source.provider_event_id,
                            changeKey=source.series_revision if scope == "series" else source.revision,
                            type="seriesMaster" if scope == "series" else "singleInstance",
                        )
                    )
                    read = AmbiguousDeleteRead(state, raw)
                    write = AmbiguousDeleteWrite(state)
                    service = MutationService(
                        self.store,
                        keyring=FakeKeyring(self.tokens),
                        read_http=read,
                        write_http=write,
                        timezone="America/Chicago",
                    )
                    arguments = {
                        "scope": scope,
                        "expected_revision": source.revision,
                        "series_revision": source.series_revision if scope == "series" else "",
                    }

                    with self.assertRaisesRegex(HttpError, "response lost"):
                        service.delete_event(source.uid, **arguments)
                    self.assertIsNotNone(self.store.get_event(source.uid))

                    result = service.delete_event(source.uid, **arguments)

                    self.assertTrue(result["deleted"])
                    self.assertIsNone(self.store.get_event(source.uid))

    def test_google_duplicate_request_fetches_the_existing_id_after_conflict(self):
        service, _, write = self.service(
            [GOOGLE_RAW], error=HttpError(409, "already exists"),
        )

        result = service.create(draft(self.keys["google"]))

        self.assertEqual(result["event"]["provider_event_id"], "created-google")
        self.assertEqual(len(write.calls), 1)

    def test_retry_after_ambiguous_create_reuses_provider_idempotency_key(self):
        class AmbiguousCreateHttp:
            def __init__(self, provider):
                self.provider = provider
                self.calls = []
                self.logical_events = {}

            def request_json(self, method, url, body=None, headers=None):
                payload = dict(body or {})
                self.calls.append((method, url, payload, headers or {}))
                key = payload["id"] if self.provider == "google" else payload["transactionId"]
                if key not in self.logical_events:
                    event_id = key if self.provider == "google" else "created-microsoft"
                    self.logical_events[key] = event_id
                    raise HttpError(0, "response lost after provider created the event")
                if self.provider == "google":
                    raise HttpError(409, "event already exists")
                return {"id": self.logical_events[key]}

        for provider in ("google", "microsoft"):
            with self.subTest(provider=provider):
                event_draft = draft(self.keys[provider])
                event_id = (
                    google_event_id(event_draft.request_id)
                    if provider == "google" else "created-microsoft"
                )
                raw = (
                    dict(GOOGLE_RAW, id=event_id)
                    if provider == "google" else MICROSOFT_RAW
                )
                read = FakeReadHttp([raw])
                write = AmbiguousCreateHttp(provider)
                service = MutationService(
                    self.store,
                    keyring=FakeKeyring(self.tokens),
                    read_http=read,
                    write_http=write,
                    timezone="America/Chicago",
                )

                with self.assertRaisesRegex(HttpError, "response lost"):
                    service.create(event_draft)
                result = service.create(event_draft)

                field = "id" if provider == "google" else "transactionId"
                expected_key = (
                    google_event_id(event_draft.request_id)
                    if provider == "google" else event_draft.request_id
                )
                self.assertEqual(
                    [call[2][field] for call in write.calls],
                    [expected_key, expected_key],
                )
                self.assertEqual(len(write.logical_events), 1)
                self.assertEqual(result["event"]["provider_event_id"], event_id)

    def test_copy_verification_failure_leaves_the_source_unchanged(self):
        service, _, write = self.service([], [MICROSOFT_RAW])

        with self.assertRaises(AssertionError):
            service.copy(draft(self.keys["microsoft"], source_uid=self.source.uid))

        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_offline_write_preserves_cached_event(self):
        service, _, _ = self.service([], error=HttpError(0, "offline"))

        with self.assertRaisesRegex(HttpError, "offline"):
            service.delete_event(
                self.source.uid, scope="single", expected_revision=self.source.revision,
            )

        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_cached_offline_health_blocks_writes_before_provider_access(self):
        for connected, stale in ((False, False), (True, True)):
            with self.subTest(connected=connected, stale=stale):
                self.store.set_health(ProviderHealth(
                    "google", "ga", connected, "2026-09-02T14:01:00Z",
                    "offline", "", stale,
                ))
                service, read, write = self.service([], [GOOGLE_RAW])

                with self.assertRaisesRegex(PermissionError, "offline"):
                    service.update(draft(
                        self.keys["google"], source_uid=self.source.uid,
                    ))

                self.assertEqual(read.calls, [])
                self.assertEqual(write.calls, [])
                self.assertEqual(self.store.get_event(self.source.uid)["title"], "Original")

    def test_unsynced_calendar_is_not_a_write_destination(self):
        hidden = Calendar(
            "google", "ga", "person@example.com", "gc", "Google Work", "#7aa2f7",
            "America/Chicago", True, True, ("googleMeet",), False,
        )
        self.store.replace_window(
            "google", "ga", "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
            [self.source], ProviderHealth.ok("google", "ga", "2026-09-02T14:01:00Z"),
            calendars=[hidden],
        )
        service, read, write = self.service([], [GOOGLE_RAW])

        with self.assertRaisesRegex(PermissionError, "enabled for synchronization"):
            service.create(draft(self.keys["google"]))

        self.assertEqual(read.calls, [])
        self.assertEqual(write.calls, [])

    def test_revoked_write_token_does_not_mutate_the_cached_event(self):
        service, _, _ = self.service([], error=HttpError(401, "revoked"))

        with self.assertRaisesRegex(HttpError, "revoked"):
            service.update(draft(self.keys["google"], source_uid=self.source.uid))

        self.assertEqual(self.store.get_event(self.source.uid)["title"], "Original")


if __name__ == "__main__":
    unittest.main()
