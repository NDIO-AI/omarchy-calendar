# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from omarchy_calendar.cache import CalendarStore
from omarchy_calendar.http import HttpError
from omarchy_calendar.models import Calendar, Event, ProviderHealth
from omarchy_calendar.mutations import DraftError, EventDraft, MutationConflict, MutationService
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
    }
    values.update(changes)
    return Event(**values)


def draft(calendar_key, **changes):
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
            ("google", "ga"): {"access_token": "google-edit", "expires_at": 9999999999, "access_mode": "edit"},
            ("microsoft", "ma"): {"access_token": "microsoft-edit", "expires_at": 9999999999, "access_mode": "edit"},
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

        self.tokens[("google", "ga")]["access_mode"] = "read"
        blocked, _, blocked_write = self.service([])
        with self.assertRaisesRegex(PermissionError, "Read and edit"):
            blocked.create(draft(self.keys["google"]))
        self.assertEqual(blocked_write.calls, [])

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

    def test_google_series_update_fetches_the_master_etag_before_writing(self):
        master = dict(
            GOOGLE_RAW,
            id="series-1",
            etag='"master-etag"',
            start={"dateTime": "2026-08-05T10:00:00-05:00", "timeZone": "America/Chicago"},
            end={"dateTime": "2026-08-05T11:00:00-05:00", "timeZone": "America/Chicago"},
        )
        updated = dict(master, summary="Updated series", etag='"updated-etag"')
        service, read, write = self.service([master, updated], [updated])

        service.update(draft(
            self.keys["google"], source_uid=self.source.uid, scope="series",
        ))

        self.assertEqual(write.calls[0][3]["If-Match"], '"master-etag"')
        self.assertEqual(write.calls[0][2]["start"]["dateTime"], "2026-08-05T10:00:00-05:00")
        self.assertEqual(write.calls[0][2]["recurrence"], [])
        self.assertTrue(read.calls[0][0].endswith("/events/series-1"))

    def test_outlook_series_move_applies_the_occurrence_delta_to_the_master(self):
        outlook_source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        master = dict(
            MICROSOFT_RAW,
            id="series-1",
            changeKey="master-change",
            type="seriesMaster",
            start={"dateTime": "2026-08-05T15:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-05T16:00:00", "timeZone": "UTC"},
        )
        updated = dict(
            master,
            changeKey="updated-change",
            start={"dateTime": "2026-08-06T15:30:00", "timeZone": "UTC"},
            end={"dateTime": "2026-08-06T16:30:00", "timeZone": "UTC"},
        )
        service, _, write = self.service([master, updated], [updated])

        service.update(draft(
            self.keys["microsoft"],
            source_uid=outlook_source.uid,
            scope="series",
            day="2026-09-03",
            start="10:30",
            end="11:30",
        ))

        self.assertEqual(write.calls[0][2]["start"], {
            "dateTime": "2026-08-06T10:30:00", "timeZone": "America/Chicago",
        })
        self.assertEqual(write.calls[0][2]["end"], {
            "dateTime": "2026-08-06T11:30:00", "timeZone": "America/Chicago",
        })
        self.assertIsNone(write.calls[0][2]["recurrence"])

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

    def test_outlook_create_update_and_delete_refresh_the_local_cache(self):
        created = dict(MICROSOFT_RAW, changeKey="change-2")
        updated = dict(MICROSOFT_RAW, changeKey="change-3", subject="Renamed")
        service, _, write = self.service(
            [created, created, updated, updated],
            [created, {}, {}],
        )

        created_result = service.create(draft(self.keys["microsoft"]))
        uid = created_result["event"]["uid"]
        service.update(draft(self.keys["microsoft"], source_uid=uid, title="Renamed"))
        service.delete_event(uid, scope="single")

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

    def test_cross_provider_copy_verifies_destination_and_never_deletes_source(self):
        service, _, write = self.service([MICROSOFT_RAW], [MICROSOFT_RAW])
        copy_draft = draft(
            self.keys["microsoft"], source_uid=self.source.uid, online_meeting="preserve",
        )

        result = service.copy(copy_draft)

        self.assertEqual(result["mode"], "copy")
        self.assertTrue(result["delete_original_available"])
        self.assertIn(self.source.meeting_url, write.calls[0][2]["body"]["content"])
        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))

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

    def test_series_delete_uses_master_id_and_removes_only_after_success(self):
        master = dict(GOOGLE_RAW, id="series-1", etag='"master-etag"')
        service, _, write = self.service([master])

        result = service.delete_event(self.source.uid, scope="series")

        self.assertEqual(result, {
            "deleted": True, "uid": self.source.uid, "scope": "series", "refresh": True,
        })
        self.assertTrue(write.calls[0][1].endswith("/events/series-1"))
        self.assertEqual(write.calls[0][3]["If-Match"], '"master-etag"')
        self.assertIsNone(self.store.get_event(self.source.uid))

    def test_outlook_series_delete_fetches_and_deletes_the_master(self):
        outlook_source = event("microsoft", "ma", "mc", "occurrence-1", revision="change-1")
        self.store.upsert_event(outlook_source)
        master = dict(MICROSOFT_RAW, id="series-1", changeKey="master-change", type="seriesMaster")
        service, read, write = self.service([master], [{}])

        result = service.delete_event(outlook_source.uid, scope="series")

        self.assertTrue(read.calls[0][0].endswith("/events/series-1"))
        self.assertTrue(write.calls[0][1].endswith("/events/series-1"))
        self.assertEqual(result["scope"], "series")
        self.assertIsNone(self.store.get_event(outlook_source.uid))

    def test_google_duplicate_request_fetches_the_existing_id_after_conflict(self):
        service, _, write = self.service(
            [GOOGLE_RAW], error=HttpError(409, "already exists"),
        )

        result = service.create(draft(self.keys["google"]))

        self.assertEqual(result["event"]["provider_event_id"], "created-google")
        self.assertEqual(len(write.calls), 1)

    def test_copy_verification_failure_leaves_the_source_unchanged(self):
        service, _, write = self.service([], [MICROSOFT_RAW])

        with self.assertRaises(AssertionError):
            service.copy(draft(self.keys["microsoft"], source_uid=self.source.uid))

        self.assertEqual([call[0] for call in write.calls], ["POST"])
        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_offline_write_preserves_cached_event(self):
        service, _, _ = self.service([], error=HttpError(0, "offline"))

        with self.assertRaisesRegex(HttpError, "offline"):
            service.delete_event(self.source.uid, scope="single")

        self.assertIsNotNone(self.store.get_event(self.source.uid))

    def test_revoked_write_token_does_not_mutate_the_cached_event(self):
        service, _, _ = self.service([], error=HttpError(401, "revoked"))

        with self.assertRaisesRegex(HttpError, "revoked"):
            service.update(draft(self.keys["google"], source_uid=self.source.uid))

        self.assertEqual(self.store.get_event(self.source.uid)["title"], "Original")


if __name__ == "__main__":
    unittest.main()
